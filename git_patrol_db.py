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

"""Database abstraction library for the Git Patrol service.

Provides a high level API to the database of persistent state.
"""

import uuid


class GitPatrolDb:
  """Database abstraction class for commonly used operations.

  Rather than have client code depend on the database library directly, this
  class provides some insulation between the two. It also wraps the commonly
  used operations behind simple method calls to provide a clean layer between
  the callers and potentially complex database acrobatics.
  """

  def __init__(self, asyncpg_pool):
    self.db_pool = asyncpg_pool

  async def fetch_latest_refs_by_alias(self, alias):
    """Retrieve the most recent git refs for a given alias.

    Args:
      alias: The git alias to use when looking up git tags.
    Returns:
      A dictionary of git refs and commit hashes. An empty dictionary otherwise.
    """
    async with self.db_pool.acquire() as conn:
      row = await conn.fetchrow(
          '''SELECT refs
          FROM git_poll_journal
          WHERE alias = $1
          ORDER BY update_time DESC LIMIT 1;
          ''', alias)
      if row:
        return { ref[0]: ref[1] for ref in row['refs'] }

    return {}

  async def record_git_poll(self, utc_datetime, url, alias, refs, ref_filters):
    """Update the git poll journal with results from the latest poll.

    Args:
      utc_datetime: Timestamp of the poll operation in UTC time zone.
      url: Git URL of the polled repository.
      alias: Human readable alias for the repository.
      refs: Dictionary of git reference names and commit hashes retrieved from
        the repository.
      ref_filters: Git ref filters used to prune the returned references.
    Returns:
      The unique identifier assigned to this entry if successful. None
      otherwise.
    """
    poll_journal_uuid = uuid.uuid4()

    async with self.db_pool.acquire() as conn:
      insert_status = await conn.execute(
          '''INSERT INTO git_poll_journal (
            git_poll_uuid, update_time, url, alias, refs, ref_filters)
          VALUES ($1, $2, $3, $4, $5, $6);
          ''', poll_journal_uuid, utc_datetime, url, alias,
          [[refname, commit] for (refname, commit) in refs.items()],
          ref_filters)
      if insert_status == 'INSERT 0 1':
        return poll_journal_uuid

    return None
