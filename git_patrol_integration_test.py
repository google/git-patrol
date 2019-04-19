# Copyright 2019 Google LLC
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

"""Integration tests for git_patrol."""

import asyncio
import logging
import os
import shutil
import tempfile
import time
import unittest

import asyncpg
import git_patrol_db


DB_CONNECT_ATTEMPTS = 3
DB_CONNECT_WAIT_SECS = 10
FETCH_REFS_ATTEMPTS = 3
FETCH_REFS_WAIT_SECS = 20


class GitPatrolIntegrationTest(unittest.TestCase):

  async def _init_git_repos(bare_dir, clone_dir):
    """Initialize integration test repositories.

    Set up two repositories for the integration test. One repository is created
    bare to be served via the git HTTP container. The other repository is a
    clone for making local changes needed by the test.

    Args:
      bare_dir: Path to the bare repository.
      clone_dir: Path to the cloned repository.
    """
    proc = await asyncio.create_subprocess_exec(
        'git', 'init', '--quiet', '--bare', cwd=bare_dir)
    returncode = await proc.wait()
    assert returncode == 0

    proc = await asyncio.create_subprocess_exec(
        'git', 'clone', '--quiet', bare_dir, clone_dir)
    returncode = await proc.wait()
    assert returncode == 0

    proc = await asyncio.create_subprocess_exec(
        'git', 'config', 'user.name', '"The Author"', cwd=clone_dir)
    returncode = await proc.wait()
    assert returncode == 0

    proc = await asyncio.create_subprocess_exec(
        'git', 'config', 'user.email', 'the@author.com', cwd=clone_dir)
    returncode = await proc.wait()
    assert returncode == 0

  @classmethod
  def setUpClass(cls):
    git_http_dir = os.environ['GIT_HTTP_DIR'] or ''
    db_host = os.environ['DB_HOST'] or ''
    db_port = os.environ['DB_PORT'] or ''
    db_user = os.environ['DB_USER'] or ''
    db_name = os.environ['DB_NAME'] or ''
    db_password = os.environ['DB_PASSWORD'] or ''

    cls._bare_dir = os.path.join(git_http_dir, 'test.git')
    os.makedirs(cls._bare_dir)

    cls._clone_dir = tempfile.mkdtemp()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(cls._init_git_repos(cls._bare_dir, cls._clone_dir))

    db_pool = None
    for i in range(DB_CONNECT_ATTEMPTS):
      try:
        db_pool = loop.run_until_complete(
            asyncpg.create_pool(
                host=db_host, port=db_port, user=db_user, password=db_password,
                database=db_name))
      except:
        logging.warning('Failed to connect on attempt {}'.format(i + 1))
        if i < (DB_CONNECT_ATTEMPTS - 1):
          logging.warning('Retry in {} seconds...'.format(DB_CONNECT_WAIT_SECS))
          time.sleep(DB_CONNECT_WAIT_SECS)
        else:
          raise

    cls._db = git_patrol_db.GitPatrolDb(db_pool)

  @classmethod
  def tearDownClass(cls):
    shutil.rmtree(cls._bare_dir, ignore_errors=True)
    shutil.rmtree(cls._clone_dir, ignore_errors=True)

  async def run_command(self, *args, **kwargs):
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    return await proc.wait()

  def testTagAndBranchSuccess(self):
    loop = asyncio.get_event_loop()
    returncode = loop.run_until_complete(
        self.run_command(
            'git', 'commit', '--quiet', '--allow-empty', '--message=Abc',
            cwd=self._clone_dir))
    self.assertEqual(returncode, 0)

    returncode = loop.run_until_complete(
        self.run_command(
            'git', 'tag', '--annotate', '--message="Tag abc"', 'abc',
            cwd=self._clone_dir))
    self.assertEqual(returncode, 0)

    returncode = loop.run_until_complete(
        self.run_command(
            'git', 'push', '--quiet', '--mirror', cwd=self._clone_dir))
    self.assertEqual(returncode, 0)

    # Wait for Git Patrol to poll the new commits and record them.
    for i in range(FETCH_REFS_ATTEMPTS):
      uuid, refs = loop.run_until_complete(
          self._db.fetch_latest_refs_by_alias('test'))
      if 'refs/tags/abc' in refs and 'refs/heads/master' in refs:
        break

      logging.warning('Expected refs not found yet...')
      time.sleep(FETCH_REFS_WAIT_SECS)

    self.assertTrue('refs/tags/abc' in refs)
    self.assertTrue('refs/heads/master' in refs)

if __name__ == '__main__':
  unittest.main()
