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
# Defines a container for the Git Patrol service.
# Usage:
#   $ docker build . -t git-patrol
#   $ docker run git-patrol

FROM ubuntu:18.04

# Install dependencies from the package manager.
#   - git: Access remote git repositories
#   - python: Dependency for Google Cloud command line client
#   - python3: Dependency for Git Patrol service
#   - python3-pip: Install required python library dependencies
#   - wget: Fetch Google Cloud command line client
RUN apt update && \
    apt install -y \
    git \
    python \
    python3 \
    python3-pip \
    wget && \
    rm -rf /var/lib/apt/lists/*

# Google Cloud SDK installation command borrowed from...
# https://github.com/pivotal-cf/bosh-concourse-deployments/blob/master/docker/Dockerfile
ENV GCLOUD_SDK_VERSION=225.0.0
RUN wget -q -O /usr/gcloud.tar.gz https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-$GCLOUD_SDK_VERSION-linux-x86_64.tar.gz && \
    ( \
      echo '406e5b3a281a11fbd0b457319e138fc6a1b4cd8c7628c7186cf7b4add240411d' /usr/gcloud.tar.gz | \
      sha256sum -c - \
    ) && \
    tar -C /usr/ -xzf /usr/gcloud.tar.gz && \
    /usr/google-cloud-sdk/install.sh --usage-reporting false --path-update false --command-completion false -q && \
    rm /usr/gcloud.tar.gz

# Python dependencies installed from PIP.
#   - PyYAML: YAML parsing library
#   - asyncpg: Client library for PostgreSQL
#   - google-api-python-client: Client library for Google Cloud
#   - google-cloud-logging: Client library for logging to StackDriver
RUN pip3 install \
    PyYAML \
    asyncpg \
    google-api-python-client \
    google-cloud-logging

# Git Patrol service scripts.
COPY git_patrol_gce.py /usr/sbin/git_patrol_gce.py
COPY git_patrol_db.py /usr/sbin/git_patrol_db.py
COPY git_patrol.py /usr/sbin/git_patrol.py
COPY run.sh /usr/sbin/run.sh

# Create the folder used to mount Cloud Build configuration.
RUN mkdir /cloud-build-config.d

# Parameters for fetching and decrypting secrets when the container is run.
# There should be no secret information contained in these arguments per se,
# rather they are passed as arguments to gcloud commands that fetch and decrypt
# the actual secrets.
ENV SECRET_URL=""
ENV KMS_PROJECT=""
ENV KMS_KEYRING=""
ENV KMS_KEY=""

# Parameters for connecting to the persistent state database. These should be
# provided as environment variables by the Kubernetes deployment YAML config
# file. The DB_PASSWORD should be stored as a Kubernetes secret and not
# copy/pasted into the YAML config file in plaintext.
ENV DB_HOST=""
ENV DB_PORT=""
ENV DB_USER=""
ENV DB_NAME=""
ENV DB_PASSWORD=""

# Run the Git Patrol service by default.
ENTRYPOINT ["bash", "/usr/sbin/run.sh"]
