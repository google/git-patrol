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
import logging
import os
import shutil
import tempfile
import unittest
import unittest.mock
import uuid

import git_patrol
import yaml


class _MockProcess():

  def __init__(self, returncode, stdout, stderr):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr

  async def wait(self):
    return self.returncode

  async def communicate(self):
    return self.stdout, self.stderr


def _MakeMockCommand(returncode_fn=None, stdout_fn=None, stderr_fn=None):
  """Construct a coroutine to return a MockProcess.

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
    A coroutine that creates a MockProcess instance.
  """
  async def _GetMockProcess(*args):
    returncode = 0
    stdout = ''.encode()
    stderr = ''.encode()

    if args:
      if returncode_fn:
        returncode = returncode_fn(*args)
      if stdout_fn:
        stdout = stdout_fn(*args)
      if stderr_fn:
        stderr = stderr_fn(*args)

    return _MockProcess(returncode, stdout, stderr)
  return _GetMockProcess


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

  def __init__(self, record_git_tags=None, record_git_poll=None):
    self.record_git_tags = record_git_tags
    self.record_git_poll = record_git_poll


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

  def setUp(self):
    logging.disable(logging.CRITICAL)

    self._temp_dir = tempfile.mkdtemp()
    self._upstream_dir = os.path.join(self._temp_dir, 'upstream')
    os.makedirs(self._upstream_dir)
    asyncio.get_event_loop().run_until_complete(
        self._init_git_repo(self._upstream_dir))

  def tearDown(self):
    shutil.rmtree(self._temp_dir, ignore_errors=True)

  def testFetchGitTagsSuccess(self):
    commands = git_patrol.GitPatrolCommands()

    upstream_url = 'file://' + self._upstream_dir
    tags = asyncio.get_event_loop().run_until_complete(
        git_patrol.fetch_git_tags(commands, upstream_url))
    self.assertEqual(tags, ['r0001', 'r0002'])

  def testFetchGitRefsSuccess(self):
    commands = git_patrol.GitPatrolCommands()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = []
    refs = asyncio.get_event_loop().run_until_complete(
        git_patrol.fetch_git_refs(commands, upstream_url, ref_filters))
    self.assertListEqual(
        ['refs/heads/master', 'refs/tags/r0001', 'refs/tags/r0002'],
        sorted(list(refs.keys())))

  def testFetchGitRefsFilteredSuccess(self):
    commands = git_patrol.GitPatrolCommands()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = ['refs/tags/*']
    refs = asyncio.get_event_loop().run_until_complete(
        git_patrol.fetch_git_refs(commands, upstream_url, ref_filters))
    self.assertListEqual(
        ['refs/tags/r0001', 'refs/tags/r0002'], sorted(list(refs.keys())))

  def testWorkflowNotTriggered(self):
    commands = git_patrol.GitPatrolCommands()

    tag_history_uuid = uuid.uuid4()
    git_poll_uuid = uuid.uuid4()
    mock_record_git_tags = AsyncioMock(return_value=tag_history_uuid)
    mock_record_git_poll = AsyncioMock(return_value=git_poll_uuid)
    mock_db = MockGitPatrolDb(
        record_git_tags=mock_record_git_tags,
        record_git_poll=mock_record_git_poll)

    loop = asyncio.get_event_loop()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = []
    workflow_trigger, current_tags = loop.run_until_complete(
        git_patrol.run_workflow_triggers(
            commands, mock_db, 'upstream', upstream_url, ref_filters,
            ['r0001', 'r0002']))

    mock_record_git_tags.inner_mock.assert_called_with(
        unittest.mock.ANY, upstream_url, 'upstream', ['r0001', 'r0002'])
    mock_record_git_poll.inner_mock.assert_called_with(
        unittest.mock.ANY, upstream_url, 'upstream', unittest.mock.ANY,
        unittest.mock.ANY)

    # The git commit hashes are always unique across test runs, thus the
    # acrobatics here to extract the HEAD and tag names only.
    record_git_poll_args, _ = mock_record_git_poll.inner_mock.call_args
    self.assertListEqual(
        ['refs/heads/master', 'refs/tags/r0001', 'refs/tags/r0002'],
        sorted(list(record_git_poll_args[3].keys())))

    self.assertEqual(workflow_trigger, False)
    self.assertEqual(current_tags, ['r0001', 'r0002'])

  def testWorkflowIsTriggered(self):
    commands = git_patrol.GitPatrolCommands()

    tag_history_uuid = uuid.uuid4()
    git_poll_uuid = uuid.uuid4()
    mock_record_git_tags = AsyncioMock(return_value=tag_history_uuid)
    mock_record_git_poll = AsyncioMock(return_value=git_poll_uuid)
    mock_db = MockGitPatrolDb(
        record_git_tags=mock_record_git_tags,
        record_git_poll=mock_record_git_poll)

    loop = asyncio.get_event_loop()

    upstream_url = 'file://' + self._upstream_dir
    ref_filters = []
    workflow_trigger, current_tags = loop.run_until_complete(
        git_patrol.run_workflow_triggers(
            commands, mock_db, 'upstream', upstream_url, ref_filters,
            ['r0001']))

    mock_record_git_tags.inner_mock.assert_called_with(
        unittest.mock.ANY, upstream_url, 'upstream', ['r0001', 'r0002'])
    mock_record_git_poll.inner_mock.assert_called_with(
        unittest.mock.ANY, upstream_url, 'upstream', unittest.mock.ANY,
        unittest.mock.ANY)

    # The git commit hashes are always unique across test runs, thus the
    # acrobatics here to extract the HEADs and tag names only.
    record_git_poll_args, _ = mock_record_git_poll.inner_mock.call_args
    self.assertListEqual(
        ['refs/heads/master', 'refs/tags/r0001', 'refs/tags/r0002'],
        sorted(list(record_git_poll_args[3].keys())))

    self.assertEqual(workflow_trigger, True)
    self.assertEqual(current_tags, ['r0001', 'r0002'])

  def testRunWorkflowSuccess(self):
    cloud_build_uuid = '7d1bb5a7-545f-4c30-b640-f5461036e2e7'

    # Queue up three different stdout strings for the gcloud mock to return,
    # one for each of the different commands we expect the client to call.
    def gcloud_builds_stdout(*args):
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
        return (
            '{ "createTime": "2018-11-01T20:49:31.802340417Z", '
            '"finishTime": "2018-11-01T22:44:36.303015Z", '
            '"id": "7d1bb5a7-545f-4c30-b640-f5461036e2e7", '
            '"startTime": "2018-11-01T20:50:24.132599935Z", '
            '"status": "SUCCESS" }').encode()
      raise ValueError('Unexpected gcloud command: {}'.format(args[1]))

    commands = git_patrol.GitPatrolCommands()
    commands.gcloud = unittest.mock.MagicMock()
    commands.gcloud.side_effect = _MakeMockCommand(
        stdout_fn=gcloud_builds_stdout)

    target_config = yaml.load(
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
    git_tag = 'r0002'

    workflow_success = asyncio.get_event_loop().run_until_complete(
        git_patrol.run_workflow_body(
            commands, config_path, target_config, git_tag))
    self.assertTrue(workflow_success)

    commands.gcloud.assert_any_call(
        'builds', 'submit', '--async',
        '--config={}'.format(os.path.join(config_path, workflow['config'])),
        '--substitutions=TAG_NAME={},{}'.format(git_tag, substitution_list),
        os.path.join(config_path, workflow['sources']))

    commands.gcloud.assert_any_call(
        'builds', 'log', '--stream', '--no-user-output-enabled',
        cloud_build_uuid)

    commands.gcloud.assert_any_call(
        'builds', 'describe', '--format=json', cloud_build_uuid)


if __name__ == '__main__':
  unittest.main()
