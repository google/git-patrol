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

"""Entry point for the Git Patrol service.

Defines the entry point for the Git Patrol service. Provides a platform
dependent customization wrapper around the git_patrol library. Useful for
encapsulating dependencies (ex: google.cloud.logging) that shouldn't
necessarily live in the git_patrol library itself.
"""

import argparse
import asyncio
import logging
import os
import time
import yaml

import asyncpg
import git_patrol
import git_patrol_db


DB_CONNECT_ATTEMPTS = 3
DB_CONNECT_WAIT_SECS = 10


# Route logs to StackDriver. The Google Cloud logging library enables logs
# for INFO level by default.
# Taken from the "Setting up StackDriver Logging for Python" page at
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


def main():
  # Parse command line flags.
  parser = argparse.ArgumentParser()
  parser.add_argument(
      '--poll_interval',
      type=int,
      default=7200,
      help='Time between repository poll attempts in seconds.')
  parser.add_argument(
      '--config_path',
      required=True,
      help='Path to configuration file and Cloud Build source archives.')
  parser.add_argument(
      '--config',
      default='gitpatrol.yaml',
      help='Name of configuration file within the --config_path folder.')
  parser.add_argument(
      '--db_host',
      default='localhost',
      help='Hostname or IP address of the database server.')
  parser.add_argument(
      '--db_port',
      type=int,
      default=5432,
      help='Destination port on the database server.')
  parser.add_argument(
      '--db_user',
      help='The name of the database role used for authentication.')
  parser.add_argument(
      '--db_password',
      help='The password used for authentication.')
  parser.add_argument(
      '--db_name',
      help='Name of the database to access on the database server.')
  args = parser.parse_args()

  # Use actual subprocess commands in production.
  commands = git_patrol.GitPatrolCommands()

  # Read and parse the configuration file.
  # TODO(brianorr): Parse the YAML into a well defined Python object to easily
  # handle parse errors etc.
  with open(os.path.join(args.config_path, args.config), 'r') as f:
    raw_config = f.read()
  git_patrol_config = yaml.safe_load(raw_config)
  git_patrol_targets = git_patrol_config['targets']

  # Connect to the persistent state database.
  loop = asyncio.get_event_loop()
  db_pool = None
  for i in range(DB_CONNECT_ATTEMPTS):
    try:
      db_pool = loop.run_until_complete(
          asyncpg.create_pool(
              host=args.db_host, port=args.db_port, user=args.db_user,
              password=args.db_password, database=args.db_name))
      if db_pool:
        break
    except asyncpg.exceptions.InvalidPasswordError as e:
      logging.error('Bad database login: %s', e)
      break
    except asyncpg.exceptions.InvalidCatalogNameError as e:
      logging.error('Unknown database: %s', e)
      break
    except OSError as e:
      logging.warning('OSError while connecting: %s', e)

    # Retry non-fatal errors after a brief timeout.
    if i < (DB_CONNECT_ATTEMPTS - 1):
      logging.warning(
          'Connect error. Retry in %d seconds...', DB_CONNECT_WAIT_SECS)
      time.sleep(DB_CONNECT_WAIT_SECS)

  if not db_pool:
    return
  db = git_patrol_db.GitPatrolDb(db_pool)

  # Create a polling loop coroutine for each target repository. Provide an
  # initial time offset for each coroutine so they don't all hammer the remote
  # server(s) at once.
  target_loops = [
      git_patrol.target_loop(
          commands=commands,
          loop=loop,
          db=db,
          config_path=args.config_path,
          target_config=target_config,
          offset=idx * args.poll_interval / len(git_patrol_targets),
          interval=args.poll_interval)
      for idx, target_config in enumerate(git_patrol_targets)]

  # Use asyncio.gather() to submit all coroutines to the event loop as
  # recommended by @gvanrossum in the GitHub issue comments at
  # https://github.com/python/asyncio/issues/477#issuecomment-269038238
  try:
    loop.run_until_complete(asyncio.gather(*target_loops))
  except KeyboardInterrupt:
    logger.warning('Received interrupt: shutting down')
  finally:
    loop.close()


if __name__ == '__main__':
  main()
