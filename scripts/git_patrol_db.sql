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
  CREATE TABLE tag_history (
    -- Primary key. Uniquely identifies the repository poll attempt. Can be
    -- used by other tables to identify the specific poll attempt that
    -- triggered subsequent actions.
    tag_history_uuid uuid,
    -- Time that the tag_history entry was generated. Always in UTC.
    update_time timestamp,
    -- URL for the git repository.
    url text,
    -- Human consumable alias for the repository. There must be a 1:1
    -- correspondence between "url" and "alias".
    alias text,
    -- Git tags that were present when the tag_history entry was generated.
    tags text[],
    PRIMARY KEY(tag_history_uuid));

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
END;
