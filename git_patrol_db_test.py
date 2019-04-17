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

"""Tests for Git Patrol database library."""

import asyncio
import unittest
from unittest import mock
import uuid

import git_patrol_db


def AsyncioMock(*args, **kwargs):
  """Create a mock object to replace an 'async def' function.

  Args:
    *args: Positional arguments to the inner mock function.
    **kwargs: Keyword arguments to the inner mock function.
  Returns:
    An async function which passes the call onto the inner mock function.
  """
  inner_mock = mock.MagicMock(*args, **kwargs)

  async def _CallMockObject(*args, **kwargs):
    return inner_mock(*args, **kwargs)

  _CallMockObject.inner_mock = inner_mock
  return _CallMockObject


class MockAsyncpgConnection:
  """Mock object to use instead of asyncpg.Connection.

  Use this mock object in place of an asyncpg.Connection object in tests.
  Currently works in the scenario where it is obtained by calling
  `asyncpg.Pool.acquire` inside an `async with` statement.
  """

  def __init__(self, fetchrow=None, execute=None):
    self.fetchrow = fetchrow
    self.execute = execute

  async def __aenter__(self):
    return self

  async def __aexit__(self, exc_type, exc, tb):
    pass


class MockAsyncpgPool:
  """Mock object to use instead of asyncpg.Pool.

  Use this mock object in place of an asyncpg.Pool object. Currently supports
  `asyncpg.Pool.acquire` inside an `async with` statement.
  """

  def __init__(self, connection=None):
    self._connection = connection
    self.acquire = mock.MagicMock(return_value=self._connection)


class GitPatrolDbTest(unittest.TestCase):

  def testFetchGitRefsSuccess(self):
    expected_uuid = uuid.uuid4()
    expected_refs = [['refs/tags/r0000', 'abcd'], ['refs/tags/r0001', 'fghi']]

    mock_fetchrow = AsyncioMock(return_value=(
        {'git_poll_uuid': expected_uuid, 'refs': expected_refs}))

    mock_connection = MockAsyncpgConnection(fetchrow=mock_fetchrow)
    mock_pool = MockAsyncpgPool(connection=mock_connection)

    db = git_patrol_db.GitPatrolDb(mock_pool)
    actual_uuid, actual_refs = asyncio.get_event_loop().run_until_complete(
        db.fetch_latest_refs_by_alias('sdm845'))
    self.assertEqual(actual_uuid, expected_uuid)
    self.assertEqual(actual_refs, {ref[0]: ref[1] for ref in expected_refs})

    mock_fetchrow.inner_mock.assert_called_with(unittest.mock.ANY, 'sdm845')

  def testRecordGitPollSuccess(self):
    mock_execute = AsyncioMock(return_value='INSERT 0 1')
    mock_connection = MockAsyncpgConnection(execute=mock_execute)
    mock_pool = MockAsyncpgPool(connection=mock_connection)

    prev_uuid = uuid.uuid4()
    refs = {
        'refs/heads/master': 'abcde', 'refs/tags/r0001': 'abcde',
        'refs/tags/r0002': 'defgh'}
    ref_filters = []

    db = git_patrol_db.GitPatrolDb(mock_pool)
    poll_journal_uuid = asyncio.get_event_loop().run_until_complete(
        db.record_git_poll(None, None, None, prev_uuid, refs, ref_filters))
    self.assertTrue(poll_journal_uuid)

    mock_execute.inner_mock.assert_called_with(
        unittest.mock.ANY, poll_journal_uuid, unittest.mock.ANY,
        unittest.mock.ANY, unittest.mock.ANY, prev_uuid,
        [[item[0], item[1]] for item in refs.items()], ref_filters)


if __name__ == '__main__':
  unittest.main()
