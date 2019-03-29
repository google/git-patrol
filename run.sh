#!/bin/bash
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
#
# Usage:
#  $ run.sh [<git_patrol_args>]

# Add gsutil command to PATH.
source /usr/google-cloud-sdk/path.bash.inc

# Hang onto arguments that should be passed to Git Patrol.
readonly GIT_PATROL_ARGS="$@"

# Verify DB entpoint and credentials are provided to the container.
if [[ -z "$DB_HOST" || -z "$DB_USER" || -z "$DB_PASSWORD" || -z "$DB_NAME" ]]; then
  echo "Missing database environment variables"
  exit 1
fi

# Optionally fetch, decrypt and install secrets to the $HOME folder.
if [[ ! -z "$SECRET_URL" ]]; then
  if [[ -z "$KMS_PROJECT" || -z "$KMS_KEYRING" || -z "$KMS_KEY" ]]; then
    echo "Missing Cloud KMS environment variables"
    exit 1
  fi

  # Retrieve and install secrets.
  echo "Copy encrypted credentials from Cloud Storage"
  gsutil cp "$SECRET_URL" "$HOME/secrets.tar.gz.enc" || { exit 1; }

  echo "Decrypt credentials to $HOME"
  gcloud kms decrypt \
      --location=global \
      --project="$KMS_PROJECT" \
      --keyring="$KMS_KEYRING" \
      --key="$KMS_KEY" \
      --ciphertext-file="$HOME/secrets.tar.gz.enc" \
      --plaintext-file="$HOME/secrets.tar.gz" || { exit 1; }

  echo "Extract credentials to $HOME"
  tar -C "$HOME" -xzf "$HOME/secrets.tar.gz" || { exit 1; }
fi

# Run long-lived service.
#   - poll_interval: 7200 seconds chosen arbitrarily as a compomise betwen
#     responsiveness and low overhead to the remote server.
echo "Start Git Patrol"
exec python3 /usr/sbin/git_patrol_gce.py \
    --config_path=/cloud-build-config.d \
    --db_host="$DB_HOST" \
    --db_name="$DB_NAME" \
    --db_user="$DB_USER" \
    --db_password="$DB_PASSWORD" \
    $GIT_PATROL_ARGS
