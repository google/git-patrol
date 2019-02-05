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
import os
import re
import uuid


# Extract just the git tag from the output of 'git ls-remote --refs --tags'.
GIT_TAG_REGEX = r'refs/tags/(r[a-z0-9_\.]+)'

# Extract the commit hash and the reference name from the output of
# 'git ls-remote --refs'. The exact regex for a reference name is tricky as seen
# on StackOverflow (https://stackoverflow.com/questions/12093748). Since this
# regex is parsing the output of the git command, we will assume it is well
# formatted and just limit the length.
GIT_HASH_REFNAME_REGEX = r'^([0-9a-f]{40})\t(refs/[^\s]{1,64})$'

# Extract the Cloud Build UUID from the text sent to stdout when a build is
# started with "gcloud builds submit ... --async".
# Example: 16fd2706-8baf-433b-82eb-8c7fada847da
# See https://docs.python.org/3/library/uuid.html for more UUID info.
GCB_ASYNC_BUILD_ID_REGEX = (
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

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


async def fetch_git_tags(commands, url):
  """Fetch tags from the provided git repository URL.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    url: URL of git repo to retrieve tags from.
  Returns:
    A list of git tags in the repo. Returns an empty list when the undelying
    git command fails.
  """
  git_subproc = await commands.git('ls-remote', '--refs', '--tags', url)
  stdout, _ = await git_subproc.communicate()
  returncode = await git_subproc.wait()
  if returncode:
    logger.warning('git ls-remote returned %d', returncode)
    return []

  raw_tags = stdout.decode('utf-8', 'ignore')
  tags = re.findall(GIT_TAG_REGEX, raw_tags)
  return tags


async def fetch_git_refs(commands, url):
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
  Returns:
    Returns a dictionary of git references and commit hashes retrieved from the
    repository if successful. Returns None when the underlying git command
    fails.
  """
  git_subproc = await commands.git('ls-remote', '--refs', url)
  stdout, _ = await git_subproc.communicate()
  returncode = await git_subproc.wait()
  if returncode:
    logger.warning('git ls-remote returned %d', returncode)
    return None

  # Note that re.findall() returns group matches as tuples, so a conversion to
  # lists is necessary.
  raw_refs = stdout.decode('utf-8', 'ignore')
  refs = re.findall(GIT_HASH_REFNAME_REGEX, raw_refs, re.MULTILINE)
  return {refname: commit for (commit, refname) in refs}


async def cloud_build_start(commands, config_path, config, git_tag):
  """Submit a new workflow to Google Cloud Build.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    config_path: Path to the Cloud Build configuration sources.
    config: Configuration object to read Cloud Build config from.
    git_tag: Name of the git tag to pass to the Cloud Build workflow.
  Returns:
    The UUID of the newly created Cloud Build workflow if successful. Otherwise
    returns None.
  """
  arg_config = '--config={}'.format(os.path.join(config_path, config['config']))

  substitutions = (
      ','.join(
          '{!s}={!s}'.format(
              k, v) for (k, v) in config['substitutions'].items()))
  arg_substitutions = '--substitutions=TAG_NAME={},{}'.format(
      git_tag, substitutions)

  arg_sources = os.path.join(config_path, config['sources'])

  gcloud_subproc = await commands.gcloud(
      'builds', 'submit', '--async', arg_config, arg_substitutions, arg_sources)
  stdout_bytes, _ = await gcloud_subproc.communicate()
  returncode = await gcloud_subproc.wait()
  if returncode:
    logger.warning('gcloud builds submit returned %d', returncode)
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

  return uuid.UUID(hex=cloud_build_uuid)


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
  await gcb_log_subproc.communicate()
  returncode = await gcb_log_subproc.wait()
  if returncode:
    logger.warning('gcloud builds log returned %d', returncode)
    return None

  logger.info('Cloud Build finished [ID=%s]', cloud_build_uuid)

  gcb_describe_subproc = await commands.gcloud(
      'builds', 'describe', '--format=json', str(cloud_build_uuid))
  stdout_bytes, _ = await gcb_describe_subproc.communicate()
  returncode = await gcb_describe_subproc.wait()
  if returncode:
    logger.warning('gcloud builds describe returned %d', returncode)
    return None

  return stdout_bytes.decode('utf-8', 'ignore')


async def run_workflow_triggers(commands, db, alias, url, current_tags):
  """Evaluates workflow trigger conditions.

  Poll the remote repository for a list of its git tags. The workflow trigger
  will be satisfied if new tags were added since the last time the repository
  was polled.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    db: A GitPatrolDb object used for database operations.
    alias: Human friendly alias of the configuration for this repository.
    url: URL of the repository to patrol.
    current_tags: List of git tags to expect in the cloned repository. Any tags
      that are now in the repository and not in this list will satisfy the
      workflow trigger.
  Returns:
    Returns a (boolean, string[]) tuple. The first item is True when the
    workflow trigger has been satisfied and False otherwise. The second item
    contains a list of the current git tags in the remote repository.
  """
  # Retrieve current tags from the remote repo.
  new_tags = await fetch_git_tags(commands, url)
  if not new_tags:
    return False, current_tags
  logger.info('%s: fetched tags: %s', alias, ' '.join(new_tags))

  # Add a new journal entry with these git tags.
  update_time = datetime.datetime.utcnow()
  git_tags_uuid = await db.record_git_tags(update_time, url, alias, new_tags)
  if not git_tags_uuid:
    logger.warning('%s: failed to record git tags', alias)
    return False, current_tags

  # Retreive current refs from the remote repo. This is non-fatal until we
  # migrate away from the pure tag-based design.
  new_refs = await fetch_git_refs(commands, url)
  if new_refs:
    # Add a new journal entry with these git refs.
    git_refs_uuid = await db.record_git_poll(update_time, url, alias, new_refs)
    if not git_refs_uuid:
      logger.warning('%s: failed to record git refs', alias)

  # See if new git tags were added since the last check.
  tags_delta = set(new_tags) - set(current_tags)
  if not tags_delta:
    logger.info('%s: no new tags', alias)
    return False, current_tags

  logger.info('%s: new tags: %s', alias, ' '.join(tags_delta))
  return True, new_tags


async def run_workflow_body(commands, config_path, config, git_tag):
  """Runs the actual workflow logic.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    config_path: Path to the Cloud Build configuration sources.
    config: Target configuration object.
    git_tag: Expected git tag at HEAD in the cloned repository.
  Returns:
    True when the workflow completes successfully. False otherwise.
  """
  for workflow in config['workflows']:
    build_id = await cloud_build_start(commands, config_path, workflow, git_tag)
    if not build_id:
      return False

    status_json = await cloud_build_wait(commands, build_id)
    if not status_json:
      return False

  return True


async def target_loop(
    commands, loop, db, config_path, target_config, offset, interval):
  """Main loop to manage periodic workflow execution.

  Args:
    commands: GitPatrolCommands object used to execute external commands.
    loop: A reference to the asyncio event loop in use.
    db: A GitPatrolDb object used for database operations.
    target: Git Patrol config target information.
    offset: Starting offset time in seconds.
    interval: Time in seconds to wait between poll attempts.
  Returns:
    Nothing. Loops forever.
  """
  alias = target_config['alias']
  url = target_config['url']

  # Fetch latest git tags from the database.
  current_tags = await db.fetch_latest_tags_by_alias(alias)
  logger.info('%s: current tags %s', alias, ' '.join(current_tags))

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

    # Evaluate workflow triggers to see if the workflow needs to run again.
    workflow_trigger, current_tags = await run_workflow_triggers(
        commands, db, alias, url, current_tags)
    if workflow_trigger:
      await run_workflow_body(
          commands, config_path, target_config, current_tags[-1])