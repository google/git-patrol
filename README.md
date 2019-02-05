---
This is not an officially supported Google product.

---

# Git Patrol Service

A service to monitor git repositories and trigger Google Cloud Build workflows
when changes are noticed. The Google Cloud Build service is capable of
monitoring and triggering workflows from a limited selection of public hosting
services (ex: GitHub). The Git Patrol service is written to fill in the gaps
for users of other hosting platforms.

# Setup

The Git Patrol service is delivered via Docker container image. The container
currently assumes that credentials are secured and delivered by the Google Cloud
KMS and Google Cloud Storage services. Long term persistent state is stored in a
PostgreSQL database such as those available in Google Cloud SQL.

## Add secrets

Follow the Google Cloud KMS directions on [Storing
secrets](https://cloud.google.com/kms/docs/store-secrets). This involves
creating a separate Google Cloud project to host the key management service.
These Cloud KMS settings are passed to the `run.sh` script in the Docker
container's `KMS_PROJECT`, `KMS_KEYRING` and `KMS_KEY` environment variables.

The `run.sh` script assumes that credentials are stored encrypted in a `.tar.gz`
archive stored in a Cloud Storage bucket. The URL to this file is passed in the
Docker container's `SECRET_URL` environment variable.

## Build the container

To create a new container image follow the instructions below.

1. [Install Docker CE](https://www.docker.com/products/docker-engine).
2. Build the Git Patrol container.
  ```shell
  $ docker build . --tag git-patrol \
      --build-arg SECRET_URL=<cloud_storage_secret_url> \
      --build-arg KMS_PROJECT=<cloud_kms_project> \
      --build-arg KMS_KEYRING=<cloud_kms_keyring> \
      --build-arg KMS_KEY=<cloud_kms_key>
  ```

## Configure the cloud

Configure a Google Cloud service account with `Reader` access to the secret
archive stored at `SECRET_URL` and `Decrypter` access to the encryption key at
`KMS_KEY`.

## Configure PostgreSQL database

Run the SQL script in `git_patrol_db.sql` to create the database tables
required by the Git Patrol service. Database connection parameters are passed to
the service via the following command line flags.
   * `--db_host`: Hostname or IP address of the database server
   * `--db_port`: Destination port on the database server
   * `--db_user`: The name of the database role used for authentication
   * `--db_password`: The password used for authentication
   * `--db_name`: Name of the database to access on the database server

# Run

Most deployments to Google Cloud just need to use the Git Patrol container's
default entrypoint. This will automatically download, decrypt and extract
credentials before starting the service. The container can be manually started
as follows.

```shell
$ docker run git-patrol
```

# Test

The Git Patrol service has a (growing) unit test suite. Run it with the following
commands.

```shell
$ python3 git_patrol_test.py
$ python3 git_patrol_db_test.py
```

## Configure Kubernetes

A hermetic environment can be created by running a PostgreSQL database instance
in a sidecar container on the same pod as the Git Patrol service. The
recommended container is launcher.gcr.io/google/postgresql9. At startup this
container will run any `.sql` scripts mounted into the
`/docker-entrypoint-initdb.d` folder. Kubernetes can be configured to mount the
Git Patrol's database setup script to that folder with a
[ConfigMap](https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/)
with the following command.

```shell
$ kubectl create configmap gp-db-init-scripts --from-file=scripts/git_patrol_db.sql
```

## Local testing

The Kubernetes test deployment can be manually replicated on the local
workstation by starting the PostgreSQL and Git Patrol containers individually.
Database connection options will need to be passed to the container through
environment variables and the Cloud Build workflow volume will need to be
manually mounted.

It is recommended that a strong password be used to secure the database at all
times, even for test deployments. The following command will create a random
password and store it in the `POSTGRES_PASSWORD` environment variable so it can
be passed into local containers.

```shell
$ POSTGRES_PASSWORD=$(dd if=/dev/urandom bs=1 count=9 status=none | base64)
```

Run the `launcher.gcr.io/google/postgresql9` container locally.

```shell
$ docker run \
    --detach \
    --volume $PWD/scripts:/docker-entrypoint-initdb.d \
    --env POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    --publish 5432:5432 \
    launcher.gcr.io/google/postgresql9
```

Pass local workstation credentials for Google Cloud into the Git Patrol
container instance by mounting `$HOME/.config/gcloud` as a volume. Taken
from StackOverflow at https://stackoverflow.com/questions/42307210.

```shell
$ docker run \
    --env DB_HOST=<database_host_or_ip_address>
    --env DB_NAME=<database_name>
    --env DB_USER=<database_user>
    --env DB_PASSWORD="$POSTGRES_PASSWORD"
    --volume $HOME/.config/gcloud:/root/.config/gcloud
    --volume <cloud_build_config_path>:/cloud-build-config.d
    git-patrol
```
# License

Apache 2.0; see LICENSE for details.
