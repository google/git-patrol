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

"""Tests for git_patrol."""

import asyncio
import datetime
import logging
import json
import os
import re
import shutil
import tempfile
import unittest
import unittest.mock
import uuid

import git_patrol
import yaml


class _FakeProcess():
  """Fake version of asyncio.subprocess.Process class.

  Provides a fake implementation of the parts of the asyncio.subprocess.Process
  class used by Git Patrol. The wait() and communicate() class methods are
  `async def` defined so we can't just swap in a MagicMock class.
  """

  def __init__(self, returncode, stdout, stderr):
    self._returncode = returncode
    self._stdout = stdout
    self._stderr = stderr

  async def wait(self):
    return self._returncode

  async def communicate(self):
    return self._stdout, self._stderr


def _MakeFakeCommand(returncode_fn=None, stdout_fn=None, stderr_fn=None):
  """Construct a coroutine to return a FakeProcess.

  Parameters are provided as lookup functions that are called with the args
  provided to the subprocess' command line. This allows mock commands to behave
  differently depending on the subcommand issued. Very useful for mocking
  commands such as 'git' where behavior is determined by the second command
  line argument (ex: 'git describe', 'git ls-remote', 'git clone').

  Args:
    returncode_fn: Lookup function that provides a return code based on the
      subprocess args. If not provided, the return code defaults to zero.
    stdout_fn: Lookup function that can provide a byte array for stdout based on
      the subprocess args. If not provided, stdout defaults to an empty byte
      array.
    stderr_fn: Lookup function that can provide a byte array for stderr based on
      the subprocess args. If not provided, stderr defaults to an empty byte
      array.
  Returns:
    A coroutine that creates a FakeProcess instance.
  """

  class FakeCommand:
    """Stateful fake command execution.

    Keeps track of the number of times a specific command (with arguments) has
    been run to permit responsive fake behavior. Useful for generating
    different exit code/stdout/stderr based on command arguments and the number
    of times a command has been run.
    TODO(brian): Perhaps replace this design with layered AsyncioMock objects.
    """

    def __init__(self, returncode_fn, stdout_fn, stderr_fn):
      self._call_counts = {}
      self._returncode_fn = returncode_fn
      self._stdout_fn = stdout_fn
      self._stderr_fn = stderr_fn

    def __call__(self, *args):
      call_str = ' '.join(['{!r}'.format(arg) for arg in args])
      call_count = self._call_counts.get(call_str, 0)
      self._call_counts[call_str] = call_count + 1

      returncode = 0
      stdout = ''.encode()
      stderr = ''.encode()

      if returncode_fn:
        returncode = returncode_fn(*args, count=call_count)
      if stdout_fn:
        stdout = stdout_fn(*args, count=call_count)
      if stderr_fn:
        stderr = stderr_fn(*args, count=call_count)

      return _FakeProcess(returncode, stdout, stderr)

  fake_command = FakeCommand(returncode_fn, stdout_fn, stderr_fn)
  async def _GetFakeProcess(*args):
    return fake_command(*args)

  return _GetFakeProcess


def AsyncioMock(*args, **kwargs):
  """Create a mock object to replace an 'async def' function.

  Args:
    *args: Positional arguments to the mock function.
    **kwargs: Keyword arguments to the mock function.

  Returns:
    A coroutine that will call the MagicMock object.
  """
  inner_mock = unittest.mock.MagicMock(*args, **kwargs)

  async def _CallMockObject(*args, **kwargs):
    return inner_mock(*args, **kwargs)

  _CallMockObject.inner_mock = inner_mock
  return _CallMockObject


class MockGitPatrolDb():

  def __init__(self, record_git_poll=None, record_cloud_build=None):
    self.record_git_poll = record_git_poll
    self.record_cloud_build = record_cloud_build


class GitPatrolTest(unittest.TestCase):

  async def _init_git_repo(self, git_dir):
    proc = await asyncio.create_subprocess_exec(
        'git', 'init', '--quiet', cwd=git_dir)
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    proc = await asyncio.create_subprocess_exec(
        'git', 'config', 'user.name', '"The Author"', cwd=git_dir)
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    proc = await asyncio.create_subprocess_exec(
        'git', 'config', 'user.email', 'the@author.com', cwd=git_dir)
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    proc = await asyncio.create_subprocess_exec(
        'git', 'commit', '--quiet', '--allow-empty', '--message="First"',
        cwd=git_dir)
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    proc = await asyncio.create_subprocess_exec(
        'git', 'tag', '-a', 'r0001', '-m', 'Tag r0001', cwd=git_dir)
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    proc = await asyncio.create_subprocess_exec(
        'git', 'commit', '--quiet', '--allow-empty', '--message="Second"',
        cwd=git_dir)
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    proc = await asyncio.create_subprocess_exec(
        'git', 'tag', '-a', 'r0002', '-m', 'Tag r0002', cwd=git_dir)
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    proc = await asyncio.create_subprocess_exec(
        'git', 'show-ref', stdout=asyncio.subprocess.PIPE, cwd=git_dir)
    stdout, _ = await proc.communicate()
    returncode = await proc.wait()
    self.assertEqual(returncode, 0)

    raw_refs = stdout.decode('utf-8', 'ignore')
    refs = re.findall(git_patrol.GIT_HASH_REFNAME_REGEX, raw_refs, re.MULTILINE)
    self.assertEqual(len(refs), 3)
    return {refname: commit for (commit, refname) in refs}

  def setUp(self):
    super(GitPatrolTest, self).setUp()
    logging.disable(logging.CRITICAL)

    self._temp_dir = tempfile.mkdtemp()
    self._upstream_dir = os.path.join(self._temp_dir, 'upstream')
    os.makedirs(self._upstream_dir)
    self._refs = asyncio.get_event_loop().run_until_complete(
        self._init_git_repo(self._upstream_dir))

  def tearDown(self):
    shutil.rmtree(self._temp_dir, ignore_errors=True)
    super(GitPatrolTest, self).tearDown()

  def testFetchGitRefsSuccess(self):
    commands = git_patrol.GitPatrolCommands()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = []
    refs = asyncio.get_event_loop().run_until_complete(
        git_patrol.fetch_git_refs(commands, upstream_url, ref_filters))
    self.assertDictEqual(refs, self._refs)

  def testFetchGitRefsFilteredSuccess(self):
    commands = git_patrol.GitPatrolCommands()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = ['refs/tags/*']
    refs = asyncio.get_event_loop().run_until_complete(
        git_patrol.fetch_git_refs(commands, upstream_url, ref_filters))
    self.assertDictEqual(
        refs,
        {k: v for k, v in self._refs.items() if k.startswith('refs/tags/')})

  def testWorkflowNotTriggered(self):
    commands = git_patrol.GitPatrolCommands()

    previous_uuid = uuid.uuid4()
    current_uuid = uuid.uuid4()
    mock_record_git_poll = AsyncioMock(return_value=current_uuid)
    mock_db = MockGitPatrolDb(record_git_poll=mock_record_git_poll)

    loop = asyncio.get_event_loop()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = []
    utc_datetime = datetime.datetime.utcnow()
    current_uuid, current_refs, new_refs = loop.run_until_complete(
        git_patrol.run_workflow_triggers(
            commands, mock_db, 'upstream', upstream_url, ref_filters,
            utc_datetime, previous_uuid, self._refs))

    # Ensure previous UUID is None since there is no change in the repository's
    # git refs.
    mock_record_git_poll.inner_mock.assert_called_with(
        utc_datetime, upstream_url, 'upstream', None, self._refs, ref_filters)

    # The git commit hashes are always unique across test runs, thus the
    # acrobatics here to extract the HEAD and tag names only.
    record_git_poll_args, _ = mock_record_git_poll.inner_mock.call_args
    self.assertCountEqual(
        ['refs/heads/master', 'refs/tags/r0001', 'refs/tags/r0002'],
        list(record_git_poll_args[4].keys()))

    self.assertEqual(current_refs, self._refs)
    self.assertFalse(new_refs)

  def testWorkflowIsTriggered(self):
    commands = git_patrol.GitPatrolCommands()

    previous_uuid = uuid.uuid4()
    current_uuid = uuid.uuid4()
    mock_record_git_poll = AsyncioMock(return_value=current_uuid)
    mock_db = MockGitPatrolDb(record_git_poll=mock_record_git_poll)

    loop = asyncio.get_event_loop()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = []
    utc_datetime = datetime.datetime.utcnow()
    current_uuid, current_refs, new_refs = loop.run_until_complete(
        git_patrol.run_workflow_triggers(
            commands, mock_db, 'upstream', upstream_url, ref_filters,
            utc_datetime, previous_uuid, {'refs/heads/master': 'none'}))

    mock_record_git_poll.inner_mock.assert_called_with(
        utc_datetime, upstream_url, 'upstream', previous_uuid,
        self._refs, ref_filters)

    # The git commit hashes are always unique across test runs, thus the
    # acrobatics here to extract the HEADs and tag names only.
    record_git_poll_args, _ = mock_record_git_poll.inner_mock.call_args
    self.assertCountEqual(
        ['refs/heads/master', 'refs/tags/r0001', 'refs/tags/r0002'],
        list(record_git_poll_args[4].keys()))

    self.assertDictEqual(current_refs, self._refs)
    self.assertDictEqual(new_refs, self._refs)

  def testRunOneWorkflowSuccess(self):
    cloud_build_uuid = '7d1bb5a7-545f-4c30-b640-f5461036e2e7'

    cloud_build_json = [
        ('{ "createTime": "2018-11-01T20:49:31.802340417Z", '
         '"id": "7d1bb5a7-545f-4c30-b640-f5461036e2e7", '
         '"startTime": "2018-11-01T20:50:24.132599935Z", '
         '"status": "QUEUED" }').encode(),
        ('{ "createTime": "2018-11-01T20:49:31.802340417Z", '
         '"finishTime": "2018-11-01T22:44:36.303015Z", '
         '"id": "7d1bb5a7-545f-4c30-b640-f5461036e2e7", '
         '"startTime": "2018-11-01T20:50:24.132599935Z", '
         '"status": "SUCCESS" }').encode()]

    # Queue up three different stdout strings for the gcloud mock to return,
    # one for each of the different commands we expect the client to call.
    def gcloud_builds_stdout(*args, count):
      if args[1] == 'submit':
        return (
            '7d1bb5a7-545f-4c30-b640-f5461036e2e7 '
            '2018-11-01T20:49:31+00:00 '
            '1H54M12S '
            '- '
            '- '
            'QUEUED').encode()
      if args[1] == 'log':
        return ''.encode()
      if args[1] == 'describe':
        return cloud_build_json[count]
      raise ValueError('Unexpected gcloud command: {}'.format(args[1]))

    commands = git_patrol.GitPatrolCommands()
    commands.gcloud = unittest.mock.MagicMock()
    commands.gcloud.side_effect = _MakeFakeCommand(
        stdout_fn=gcloud_builds_stdout)

    # The "record_cloud_build()" method returns the journal_id of the created
    # entry. This must be the value of parent_id for the next entry.
    journal_ids = [1, 2]
    mock_record_cloud_build = AsyncioMock(side_effect=journal_ids)
    mock_db = MockGitPatrolDb(record_cloud_build=mock_record_cloud_build)

    target_config = yaml.safe_load(
        """
        alias: upstream
        workflows:
        - alias: first
          config: first.yaml
          sources: first.tar.gz
          substitutions:
            _VAR0: val0
            _VAR1: val1
        """)
    workflow = target_config['workflows'][0]
    substitutions = workflow['substitutions']
    substitution_list = (
        ','.join('{!s}={!s}'.format(k, v) for (k, v) in substitutions.items()))

    config_path = '/some/path'
    git_poll_uuid = uuid.uuid4()
    git_ref = ('refs/tags/r0002', 'deadbeef')

    workflow_success = asyncio.get_event_loop().run_until_complete(
        git_patrol.run_workflow_body(
            commands, mock_db, config_path, target_config, git_poll_uuid,
            git_ref))
    self.assertTrue(workflow_success)

    commands.gcloud.assert_any_call(
        'builds', 'submit', '--async',
        '--config={}'.format(os.path.join(config_path, workflow['config'])),
        '--substitutions=TAG_NAME={},{}'.format(
            git_ref[0].replace('refs/tags/', ''), substitution_list),
        os.path.join(config_path, workflow['sources']))

    commands.gcloud.assert_any_call(
        'builds', 'log', '--stream', '--no-user-output-enabled',
        cloud_build_uuid)

    commands.gcloud.assert_any_call(
        'builds', 'describe', '--format=json', cloud_build_uuid)

    # We know the method will be called with only positional arguments so we
    # can unpack call_args_list to discard the unused kwargs.
    record_cloud_build_args = [
        args for (args, _) in mock_record_cloud_build.inner_mock.call_args_list]

    # There should be two calls to "record_cloud_build()".
    self.assertEqual(len(record_cloud_build_args), 2)

    # The first call should have parent_id set to "0", indicating this is the
    # first entry. The second call should have parent_id set to "1", indicating
    # this entry has a parent.
    self.assertEqual(record_cloud_build_args[0][0], 0)
    self.assertEqual(record_cloud_build_args[1][0], 1)

    # The recorded Cloud Build JSON status should reflect what we passed via the
    # fake gcloud commands.
    self.assertEqual(
        record_cloud_build_args[0][5].items(),
        json.loads(cloud_build_json[0].decode('utf-8', 'ignore')).items())
    self.assertEqual(
        record_cloud_build_args[1][5].items(),
        json.loads(cloud_build_json[1].decode('utf-8', 'ignore')).items())

if __name__ == '__main__':
  unittest.main()
