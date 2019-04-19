# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Monitors git repositories and Cloud Builds.

Periodically polls git repositories to detect changes and kick off Cloud Build
workflows.
"""

import asyncio
import datetime
import logging
import json
import os
import re
import uuid


# Extract the commit hash and the reference name from the output of
# 'git ls-remote --refs'. The exact regex for a reference name is tricky as seen
# on StackOverflow (https://stackoverflow.com/questions/12093748). Since this
# regex is parsing the output of the git command, we will assume it is well
# formatted and just limit the length.
GIT_HASH_REFNAME_REGEX = r'^([0-9a-f]{40})\s+(refs/[^\s]{1,64})$'

# Extract the Cloud Build UUID from the text sent to stdout when a build is
# started with "gcloud builds submit ... --async".
# Example: 16fd2706-8baf-433b-82eb-8c7fada847da
# See https://docs.python.org/3/library/uuid.html for more UUID info.
GCB_ASYNC_BUILD_ID_REGEX = (
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

# Limit on the total number of ref filters.
MAX_REF_FILTERS = 5

# Route logs to StackDriver when running in the Cloud. The Google Cloud logging
# library enables logs for INFO level by default.
# Adapted from the "Setting up StackDriver Logging for Python" page at
# https://cloud.google.com/logging/docs/setup/python
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
try:
  import google.auth.exceptions
  import google.cloud.logging
  client = google.cloud.logging.Client()
  logger.addHandler(client.get_default_handler())
except (ImportError, google.auth.exceptions.GoogleAuthError) as ex:
  logger.addHandler(logging.StreamHandler())


def make_subprocess_cmd(cmd):
  """Creates a function that returns an async subprocess.

  Args:
    cmd: Command to run in the subprocess. Arguments should be provided when
      calling the returned function.
  Returns:
    A function that creates an asyncio.subprocess.Process instance.
  """
  def subprocess_cmd(*args, cwd=None):
    logger.info('Running "%s %s"', cmd, ' '.join(args))
    return asyncio.create_subprocess_exec(
        cmd, *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=cwd)
  return subprocess_cmd


class GitPatrolCommands:

  def __init__(self):
    self.git = make_subprocess_cmd('git')
    self.gcloud = make_subprocess_cmd('gcloud')


async def git_check_ref_filter(commands, ref_filter):
  """Use the git command to validate a ref filter.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    ref_filter: The git ref filter to validate.
  Returns:
    True for a valid ref filter. False otherwise.
  """
  git_subproc = await commands.git(
      'check-ref-format', '--allow-onelevel', '--refspec-pattern', ref_filter)
  await git_subproc.communicate()
  returncode = await git_subproc.wait()
  return returncode == 0


def log_command_error(command, returncode, stdout_bytes, stderr_bytes):
  """Helper function to log command errors.

  Args:
    command: The command that was run.
    returncode: Command's numeric return code.
    stdout_bytes: Command's raw standard output.
    stderr_bytes: Command's raw standard error output.
  """
  logger.warning('%s returned %d', command, returncode)
  logger.warning(
      '%s stdout:\n%s', command, stdout_bytes.decode('utf-8', 'ignore'))
  logger.warning(
      '%s stderr:\n%s', command, stderr_bytes.decode('utf-8', 'ignore'))


async def fetch_git_refs(commands, url, ref_filters):
  """Fetch tags and HEADs from the provided git repository URL.

  Use 'git ls-remote --refs' to fetch the current list of references from the
  repository. If successful the information is returned as a dictionary. The
  dictionary keys will be the full reference names and the values will be the
  commit hash associated with that reference.

  Example:
    {
      'refs/heads/master': '039de508998f3676871ed8cc00e3b33f0f95f7cb',
      'refs/heads/branch0': 'c589a4d44889afa2e6f811852b4575df7287abcd'
      'refs/tags/tag0': 'aaa2aa362047ec750359ccf42eee159db5f62726',
      'refs/tags/tag1': 'bbb7626c1d6b48d5509db048e290b1642a6766c4'
    }

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    url: URL of git repo to retrieve refs from.
    ref_filters: A (possibly empty) list of ref filters to pass to the
      'git ls-remote' command to filter the returned refs.
  Returns:
    Returns a dictionary of git references and commit hashes retrieved from the
    repository if successful. Returns None when the underlying git command
    fails.
  """
  git_subproc = await commands.git('ls-remote', '--refs', url, *ref_filters)
  stdout_bytes, stderr_bytes = await git_subproc.communicate()
  returncode = await git_subproc.wait()
  if returncode:
    log_command_error('git ls-remote', returncode, stdout_bytes, stderr_bytes)
    return None

  # Note that re.findall() returns group matches as tuples, so a conversion to
  # lists is necessary.
  raw_refs = stdout_bytes.decode('utf-8', 'ignore')
  refs = re.findall(GIT_HASH_REFNAME_REGEX, raw_refs, re.MULTILINE)
  return {refname: commit for (commit, refname) in refs}


async def cloud_build_start(commands, config_path, config, git_ref):
  """Submit a new workflow to Google Cloud Build.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    config_path: Path to the Cloud Build configuration sources.
    config: Configuration object to read Cloud Build config from.
    git_ref: The git ref (ex: refs/heads/master, refs/tags/v0.0.1) that
      triggered this workflow execution.
  Returns:
    The in-progress Cloud Build workflow state as a JSON string if successful.
    See Cloud Build documentation for the schema at...
    https://cloud.google.com/cloud-build/docs/api/reference/rest/v1/operations#Operation
    Otherwise returns None.
  """
  arg_config = '--config={}'.format(os.path.join(config_path, config['config']))

  # Provide a few default substitutions that Google Cloud Build would fill in
  # if it was launching a triggered workflow. See link for details...
  # https://cloud.google.com/cloud-build/docs/configuring-builds/substitute-variable-values
  substitutions_list = []
  if git_ref.startswith('refs/tags/'):
    substitutions_list.append(
        'TAG_NAME={}'.format(git_ref.replace('refs/tags/', '')))
  elif git_ref.startswith('refs/heads/'):
    substitutions_list.append(
        'BRANCH_NAME={}'.format(git_ref.replace('refs/heads/', '')))

  # Generate substitution strings from the target config.
  if 'substitutions' in config:
    substitutions_list += [
        '{!s}={!s}'.format(k, v) for (k, v) in config['substitutions'].items()]

  # Populate the substitutions argument if needed.
  arg_substitutions = ''
  if substitutions_list:
    arg_substitutions = '--substitutions=' + ','.join(substitutions_list)

  # Support an optional source archive passed to the workflow.
  arg_sources = '--no-source'
  if 'sources' in config:
    arg_sources = os.path.join(config_path, config['sources'])

  gcloud_subproc = await commands.gcloud(
      'builds', 'submit', '--async', arg_config, arg_substitutions, arg_sources)
  stdout_bytes, stderr_bytes = await gcloud_subproc.communicate()
  returncode = await gcloud_subproc.wait()
  if returncode:
    log_command_error(
        'gcloud builds submit', returncode, stdout_bytes, stderr_bytes)
    return None

  stdout_lines = stdout_bytes.decode('utf-8', 'ignore').splitlines()
  if not stdout_lines:
    logger.warning('gcloud builds submit produced no output')
    return None

  build_info_line = stdout_lines[-1]
  build_id_list = re.findall(GCB_ASYNC_BUILD_ID_REGEX, build_info_line)
  if not build_id_list or not build_id_list[0]:
    logger.fatal('gcloud builds submit output format has changed')
    return None

  cloud_build_uuid = build_id_list[0]
  logger.info('Cloud Build started [ID=%s]', cloud_build_uuid)

  gcb_describe_subproc = await commands.gcloud(
      'builds', 'describe', '--format=json', str(cloud_build_uuid))
  stdout_bytes, stderr_bytes = await gcb_describe_subproc.communicate()
  returncode = await gcb_describe_subproc.wait()
  if returncode:
    log_command_error(
        'gcloud builds describe', returncode, stdout_bytes, stderr_bytes)
    return None

  return stdout_bytes.decode('utf-8', 'ignore')


async def cloud_build_wait(commands, cloud_build_uuid):
  """Wait for a Google Cloud Build workflow to complete.

  Args:
    commands: MetaMonitorCommands object used to execute external commands.
    cloud_build_uuid: UUID of the Cloud Build workflow to wait for.
  Returns:
    The final Cloud Build workflow state as a JSON string if successful.
    Otherwise returns None.
  """
  # "Stream" the logs with stdout/stderr disabled because we just care about
  # waiting for the workflow to complete. It might also generate a bunch of
  # text, so disabling output avoids blowing up the Python heap collecting
  # stdout.
  logger.info('Waiting for Cloud Build [ID=%s]', cloud_build_uuid)
  gcb_log_subproc = await commands.gcloud(
      'builds', 'log', '--stream', '--no-user-output-enabled',
      str(cloud_build_uuid))
  stdout_bytes, stderr_bytes = await gcb_log_subproc.communicate()
  returncode = await gcb_log_subproc.wait()
  if returncode:
    log_command_error(
        'gcloud builds log', returncode, stdout_bytes, stderr_bytes)
    return None

  logger.info('Cloud Build finished [ID=%s]', cloud_build_uuid)

  gcb_describe_subproc = await commands.gcloud(
      'builds', 'describe', '--format=json', str(cloud_build_uuid))
  stdout_bytes, stderr_bytes = await gcb_describe_subproc.communicate()
  returncode = await gcb_describe_subproc.wait()
  if returncode:
    log_command_error(
        'gcloud builds describe', returncode, stdout_bytes, stderr_bytes)
    return None

  return stdout_bytes.decode('utf-8', 'ignore')


def git_refs_find_deltas(previous_refs, current_refs):
  """Finds new or updated git refs.

  Identifies the new git refs and the git refs whose commit hashes are
  different in current refs. Git refs present in previous_refs but missing
  from current_refs are ignored.

  Args:
    previous_refs: Dictionary of git refs to compare against.
    current_refs: Dictionary of git refs possibly containing new entries or
      updates.

  Returns:
    A dictionary of the new and updated git refs found in current_refs,
    otherwise an empty dictionary.
  """
  new_refs = {}
  for k, v in current_refs.items():
    if k not in previous_refs:
      new_refs[k] = v
    elif previous_refs[k] != v:
      new_refs[k] = v
  return new_refs


async def run_workflow_triggers(
    commands, db, alias, url, ref_filters, utc_datetime, previous_uuid,
    previous_refs):
  """Evaluates workflow trigger conditions.

  Poll the remote repository for a list of its git refs. The workflow trigger
  will be satisfied if any git refs were added or changed since the last time
  the repository was polled.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    db: A GitPatrolDb object used for database operations.
    alias: Human friendly alias of the configuration for this repository.
    url: URL of the repository to patrol.
    ref_filters: A (possibly empty) list of ref filters to pass to the
      'git ls-remote' command to filter the returned refs.
    utc_datetime: Timestamp of the poll operation in UTC time zone.
    previous_uuid: UUID of previous git poll attempt.
    previous_refs: List of git refs to expect in the cloned repository. Any refs
      that are now in the repository and not in this list, or any altered refs
      will satisfy the workflow trigger.
  Returns:
    Returns a (uuid, dict, dict) tuple. The first item is the persistent UUID
    for the poll attempt. The second item contains a dictionary of the current
    git refs in the remote repository. The third item contains a dictionary of
    git refs that should trigger a workflow execution.
    """
  # Retrieve current refs from the remote repo.
  current_refs = await fetch_git_refs(commands, url, ref_filters)
  if not current_refs:
    return previous_uuid, previous_refs, {}

  # See if the repository was updated since the last check. Only record the
  # previous poll attempt's UUID if there was a change.
  new_refs = git_refs_find_deltas(previous_refs, current_refs)
  if new_refs:
    logger.info('%s: new refs: %s', alias, new_refs)
    previous_uuid_to_record = previous_uuid
  else:
    logger.info('%s: no new refs', alias)
    previous_uuid_to_record = None

  # Add a new journal entry with these git refs.
  current_uuid = await db.record_git_poll(
      utc_datetime, url, alias, previous_uuid_to_record, current_refs,
      ref_filters)
  if not current_uuid:
    logger.warning('%s: failed to record git refs', alias)
    return previous_uuid, previous_refs, {}

  return current_uuid, current_refs, new_refs


async def run_workflow_body(
    commands, db, config_path, config, git_poll_uuid, git_ref):
  """Runs the actual workflow logic.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    db: A GitPatrolDb object used for database operations.
    config_path: Path to the Cloud Build configuration sources.
    config: Target configuration object.
    git_ref: The git ref dictionary item (ex: ('refs/heads/master', '<hash>'))
      that triggered this workflow execution.
  Returns:
    True when the workflow completes successfully. False otherwise.
  """
  alias = config['alias']

  parent_id = 0
  for workflow in config['workflows']:
    utc_datetime = datetime.datetime.utcnow()
    status_json = await cloud_build_start(
        commands, config_path, workflow, git_ref[0])
    if not status_json:
      return False

    try:
      status = json.loads(status_json)
    except JSONDecodeError as e:
      logger.error('Failed to decode Cloud Build JSON: %s', e)
      return False

    if not 'id' in status:
      return False
    build_id = status['id']

    journal_id = await db.record_cloud_build(
        parent_id, git_poll_uuid, utc_datetime, alias, git_ref, status)
    if not journal_id:
      return False
    parent_id = journal_id

    status_json = await cloud_build_wait(commands, build_id)
    if not status_json:
      return False

    utc_datetime = datetime.datetime.utcnow()
    try:
      status = json.loads(status_json)
    except JSONDecodeError as e:
      logger.error('Failed to decode Cloud Build JSON: %s', e)
      return False

    journal_id = await db.record_cloud_build(
        parent_id, git_poll_uuid, utc_datetime, alias, git_ref, status)
    if not journal_id:
      return False
    parent_id = journal_id

    if not 'status' in status:
      return False

    if status['status'] != 'SUCCESS':
      return False

  return True


async def target_loop(
    commands, loop, db, config_path, target_config, offset, interval):
  """Main loop to manage periodic workflow execution.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    loop: A reference to the asyncio event loop in use.
    db: A GitPatrolDb object used for database operations.
    config_path: Path to files referenced by the target configuration.
    target_config: Git Patrol config target information.
    offset: Starting offset time in seconds.
    interval: Time in seconds to wait between poll attempts.
  Returns:
    Nothing. Loops forever.
  """
  alias = target_config['alias']
  url = target_config['url']
  ref_filters = []
  if 'ref_filters' in target_config:
    ref_filters = target_config['ref_filters']

  # Validate target configuration.
  if len(ref_filters) > MAX_REF_FILTERS:
    logger.error('%s: too many ref filters provided', alias)
    return
  validate_tasks = [git_check_ref_filter(commands, f) for f in ref_filters]
  ref_filters_ok = await asyncio.gather(*validate_tasks)
  if not all(ref_filters_ok):
    logger.error('%s: error in ref filter', alias)
    return

  # Fetch latest git tags from the database.
  current_uuid, current_refs = await db.fetch_latest_refs_by_alias(alias)
  logger.info('%s: current refs %s', alias, current_refs)

  # Stagger the wakeup time of the target loops to avoid hammering the remote
  # server with requests all at once.
  next_wakeup_time = loop.time() + offset + 1

  while True:
    # Calculate the polling loop's next wake-up time. To stay on schedule we
    # keep incrementing next_wakeup_time by the polling interval until we
    # arrive at a time in the future.
    while next_wakeup_time < loop.time():
      next_wakeup_time += interval
    sleep_time = max(0, next_wakeup_time - loop.time())
    logger.info('%s: sleeping for %f', alias, sleep_time)
    await asyncio.sleep(sleep_time)

    # Get the current time for this round.
    utc_datetime = datetime.datetime.utcnow()

    # Evaluate workflow triggers to see if the workflow needs to run again.
    current_uuid, current_refs, new_refs = await run_workflow_triggers(
        commands, db, alias, url, ref_filters, utc_datetime, current_uuid,
        current_refs)

    # Launch a workflow for each new/updated git ref.
    workflow_tasks = [
        run_workflow_body(
            commands, db, config_path, target_config, current_uuid, ref)
        for ref in new_refs.items()]
    await asyncio.gather(*workflow_tasks)
