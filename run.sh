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
#  $ run.sh <secret_url> <kms_project> <kms_keyring> <kms_key>

# Add gsutil command to PATH.
source /usr/google-cloud-sdk/path.bash.inc

if [[ $# != 4 ]]; then
  echo "Usage"
  echo "  $ run.sh <secret_url> <kms_project> <kms_keyring> <kms_key>"
  exit 1
fi

readonly SECRET_URL=$1
readonly KMS_PROJECT=$2
readonly KMS_KEYRING=$3
readonly KMS_KEY=$4

# Verify DB entpoint and credentials are provided to the container.
if [[ -z "$DB_HOST" || -z "$DB_USER" || -z "$DB_PASSWORD" || -z "$DB_NAME" ]]; then
  echo "Missing database environment variables"
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

# Run long-lived service.
#   - poll_interval: 7200 seconds chosen arbitrarily as a compomise betwen
#     responsiveness and low overhead to the remote server.
echo "Start Git Patrol"
exec python3 /usr/sbin/git_patrol_gce.py \
    --poll_interval=7200 \
    --config_path=/cloud-build-config.d \
    --db_host="$DB_HOST" \
    --db_name="$DB_NAME" \
    --db_user="$DB_USER" \
    --db_password="$DB_PASSWORD"
