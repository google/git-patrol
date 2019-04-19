-- Copyright 2018 Google LLC
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     https://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

BEGIN;
  CREATE TABLE git_poll_journal (
    -- Primary key. Uniquely identifies the repository poll attempt. Can be
    -- used by other tables to identify the specific poll attempt that
    -- triggered subsequent actions.
    git_poll_uuid uuid,
    -- Time that the journal entry was generated. Always in UTC.
    update_time timestamp,
    -- URL for the git repository.
    url text,
    -- Human consumable alias for the repository. There must be a 1:1
    -- correspondence between "url" and "alias".
    alias text,
    -- Identifies the most recent poll attempt for the repository identified by
    -- "alias" that returned a new set of git refs. Otherwise NULL if this poll
    -- attempt returned git refs seen in the previous attempt. This field can
    -- be used to quickly identify the poll attempt that discovered new git
    -- refs and thus triggered Cloud Build workflows.
    -- Note: This field will be NULL if this poll attempt returned fewer git
    -- refs than the previous attempt, as long as they are all present in that
    -- previous attempt.
    previous_uuid uuid,
    -- Git refs and their hashes that were present when the journal entry was
    -- generated. This will include the following items...
    --   - HEADs (branch names)
    --   - Tags
    --   - Gerrit changes (ex: refs/changes/NNN/MMM)
    --   - GitHub pull requests (ex: refs/pull/NNN)
    --   - Anything else returned by "git ls-remote --refs"
    --
    -- The above list will be pruned by any ref filter patterns passed to the
    -- "git ls-remote" pattern.
    --
    -- Elements are laid out as follows...
    --   - refs[i][0]: ref name
    --   - refs[i][1]: ref hash
    --
    -- Note: Fixed array dimensions are not enforced by Postgres. Provided
    -- purely for documentation-as-code purposes.
    refs text[][2],
    -- Filter patterns (if any) used to filter the git refs returned for this
    -- journal entry. There isn't a canonical name for this term in the git
    -- literature (see docs for "git ls-remote" for description) so I'm going
    -- with this name. If no filter patterns are applied then this array will
    -- be empty. The "git check-ref-format --allow-onelevel --refspec-pattern"
    -- command must be used to validate all entries.
    ref_filters text[],
    PRIMARY KEY(git_poll_uuid));

  CREATE TABLE cloud_build_journal (
    -- Primary key. Uniquely identifies the journal update. Per the PostgreSQL
    -- documentation this corresponds to an "integer" column.
    -- https://www.postgresql.org/docs/9.5/datatype-numeric.html#DATATYPE-SERIAL
    journal_id serial,

    -- Identifies the preceding journal entry. This column is zero when this is
    -- the first journal entry for a workflow execution. This column is intended
    -- to be used in a recursive query to trace the status of all workflows that
    -- were executed in response to a particular git poll.
    -- Note: Since this is an append-only table the parent_id can refer to an
    -- instance of the same Cloud Build workflow.
    parent_id integer,

    -- Identifies the git poll that triggered this execution.
    git_poll_uuid uuid references git_poll_journal(git_poll_uuid),

    -- Time that the journal entry was generated. Always in UTC.
    update_time timestamp,

    -- Human consumable alias for the repository. Must match the "alias" field
    -- in the corresponding git_poll_journal entry.
    alias text,

    -- The git ref label and commit hash assigned to this sequence of Cloud
    -- Build workflows. A git poll can yield multiple new or updated git refs
    -- so this field tracks the specific git ref assigned to the workflows.
    ref text[2],

    -- Dump of the JSON status returned by "gcloud builds describe" command. If
    -- the parent_id field is non-zero then this entry *must* have a different
    -- status field than the previous entry.
    cloud_build_status jsonb,
    PRIMARY KEY(journal_id));
END;
