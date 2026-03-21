# validation test cases

this document lists test cases for validating the repo-sync implementation, organized by component.  these cover both expected behavior and edge cases identified during the design process.

## stripping tool

### private directory removal

- directories named exactly `private` are removed at any depth (e.g., `private/`, `crates/private/`, `src/a/b/private/`)
- directories with `private` as a substring are **not** removed (e.g., `private-utils/`, `my_private/`)
- nested `private` directories are handled correctly (e.g., `private/sub/private/` -- the outer removal should handle everything)
- files inside removed `private/` directories do not appear in the clean snapshot
- directories named `private` that contain only binary files are still removed

### marker stripping

- a single `private-start`/`private-end` region is stripped correctly, including the marker lines themselves
- multiple non-overlapping regions in the same file are all stripped
- stripping leaves no blank lines where the region was removed
- markers work regardless of comment syntax (`//`, `#`, `/*`, `--`, etc.)
- markers work with leading whitespace (indented markers)
- markers work with trailing content after the marker string on the same line
- a file where all content is inside a single marker region becomes an empty file (not deleted)
- a file with multiple marker regions where stripping leaves some content -- only the marked regions are removed

### marker error cases

- `private-start` without a matching `private-end` in the same file -- error
- `private-end` without a preceding `private-start` -- error
- nested markers (`private-start` inside an open region) -- error
- markers split across files (start in one file, end in another) -- each file errors independently

### symlink handling

- a symlink anywhere in the repo tree raises an error
- a symlink inside a `private/` directory (which gets removed before symlink check) does **not** raise an error -- it's removed with the directory
- a symlink pointing to a file outside the repo raises an error
- a symlink pointing to a `private/` directory raises an error

### text vs. binary detection

- a file with a null byte in the first 8192 bytes is classified as binary and left in the snapshot as-is
- a file with no null bytes is classified as text and processed for markers
- a PNG/JPEG/other binary file is left in the snapshot unchanged
- a binary file inside a `private/` directory is removed (directory removal happens before binary detection)

### UTF-8 handling

- a valid UTF-8 file with markers is processed correctly
- a file that fails UTF-8 decoding (e.g., Latin-1 encoded) raises an error
- a UTF-16 file (contains null bytes) is classified as binary and left as-is -- but if it somehow passes binary detection and fails UTF-8 decoding, it raises an error
- a file with a BOM (byte order mark) is handled correctly

### full tree replacement

- files added in the private repo appear in the clean snapshot
- files deleted in the private repo are absent from the clean snapshot
- file renames are handled correctly (old path gone, new path present)
- file permission changes are preserved
- binary files (images, fonts, etc.) are included in the snapshot
- an empty directory in the private repo is handled (git doesn't track empty dirs, so this may be a no-op)

## CI validation action

- detects unpaired `private-start` (no matching `private-end`)
- detects unpaired `private-end` (no matching `private-start`)
- detects nested markers
- detects symlinks anywhere in the repo
- passes on a repo with no markers and no symlinks
- passes on a repo with correctly paired, non-nested markers
- respects the `paths` input filter (only validates specified files/globs)

## reviewer assignment logic

the following assignment tests apply to all contexts where a reviewer is requested: conflict resolution, agent failure, CI failure after clean rebase, and escalation cron CI failure detection.

- when the source commit came from a PR, the person who clicked merge is requested as reviewer
- when the source commit is a direct push (no PR), the commit author is requested as reviewer
- when neither the merger nor the commit author can be determined, `@oncall-client-primary` is requested
- a `Repo-Sync-Assigned` trailer with the reviewer's username and current timestamp is appended to the PR description

## sync workflow -- private-to-public

### basic sync

- a commit that modifies only public code produces a sync PR with the correct diff
- a commit that modifies only private code (inside `private/` dirs or `!repo-sync` markers) produces no sync PR (empty diff, skipped)
- a commit that modifies both public and private code produces a sync PR containing only the public changes
- the sync PR's commit message is generic (does not contain the source commit's message)
- the `Repo-Sync-Origin` trailer is present in the PR description
- the PR description contains a reference back to the source commit

### multiple unsynced commits

- when multiple commits are unsynced, the workflow creates one stacked sync PR per commit
- the stack ordering matches the chronological order of source commits
- if some commits in a batch are internal-only (empty diff), they are skipped and no PR is created for them, but subsequent commits are still processed
- the watermark advances correctly after each sync PR merges

### idempotency

- if the workflow crashes and restarts mid-run, it does not create duplicate PRs for commits that already have sync branches
- if a sync branch already exists for a given SHA, the workflow skips that commit

### auto-merge

- the bottom PR in the stack (base = default branch) has auto-merge enabled
- PRs deeper in the stack do **not** have auto-merge enabled
- after restacking, the new bottom PR gets auto-merge enabled

## sync workflow -- public-to-private

### basic sync

- a commit on the public repo produces a sync PR to the private repo
- the sync PR preserves the original commit's author and message
- the PR title and description are copied from the source public PR, with the "Synced from" header
- for direct pushes (no source PR), the commit message is used as the PR description, with a "Synced from" header linking to the source commit
- the `Repo-Sync-Origin` trailer is present in the PR description

### multiple unsynced commits

- when multiple public commits are unsynced, the workflow creates one stacked sync PR per commit
- the stack ordering matches the chronological order of source commits
- the watermark advances correctly after each sync PR merges

### idempotency

- if the workflow crashes and restarts mid-run, it does not create duplicate public-to-private PRs
- if a sync branch already exists for a given SHA, the workflow skips that commit

### auto-merge

- the bottom public-to-private PR in the stack (base = default branch) has auto-merge enabled
- PRs deeper in the stack do **not** have auto-merge enabled
- after restacking, the new bottom PR gets auto-merge enabled

### cherry-pick conflicts

- if a cherry-pick conflicts (e.g., the same file was modified in the private repo), the conflict resolution agent is invoked
- if the agent resolves the conflict, a resolution commit is added to the PR and a reviewer is requested (human sign-off required)
- auto-merge is **not** enabled on conflict-resolved PRs
- if the agent fails, the PR is still created and a reviewer is requested

## infinite loop prevention

- a sync commit that merges into the target repo does **not** trigger a reverse sync
- the check requires both the `Repo-Sync-Origin` trailer **and** the PR branch verification to pass
- a commit with a manually-added `Repo-Sync-Origin` trailer (not from a `repo-sync/` branch) is **not** skipped -- it syncs normally
- a commit from a `repo-sync/` branch without the trailer is **not** skipped (though this shouldn't happen in practice)

## trailer parsing

- when a PR description contains multiple `Repo-Sync-Origin` trailers (e.g., from a copied source description plus the workflow-appended one), the **last** occurrence is used
- when a PR description contains a spoofed `Repo-Sync-Assigned` trailer in the copied source body, the **last** occurrence (appended by the workflow) is used
- a PR description with no trailers returns no match (not an error)

## stacked PR management

### stack creation

- the first sync PR in a stack is based on the default branch
- subsequent sync PRs are based on the previous sync PR's branch
- each PR's diff shows only the changes for its corresponding commit

### restacking after merge

- after the bottom PR merges, the next PR is rebased onto the updated default branch using `git rebase --onto`
- the rebased PR's base branch is updated to the default branch
- if the rebase succeeds and CI passes, the PR auto-merges
- if the rebase succeeds but CI fails, the PR is assigned to a human reviewer (no agent invocation)
- if the rebase has conflicts, the conflict resolution agent is invoked
- after agent conflict resolution during restacking, a reviewer is **always** requested (human sign-off required)
- auto-merge is **not** enabled on conflict-resolved PRs during restacking

### squash merge interaction

- after a squash merge, the restack correctly uses `--onto` to avoid duplicate-change conflicts
- the watermark tag is updated to point to the squash merge commit
- the `Repo-Sync-Origin` trailer is present in the squash merge commit message (preserved from the PR description)

## escalation cron workflow

### timeout escalation

- a sync PR with a `Repo-Sync-Assigned` trailer older than `escalate_after` triggers a review request from `escalate_to`
- a sync PR with a `Repo-Sync-Assigned` trailer newer than `escalate_after` is not escalated
- a sync PR with no `Repo-Sync-Assigned` trailer is not escalated

### CI failure detection

- a sync PR with auto-merge enabled and failed CI is detected
- auto-merge is disabled and a reviewer is requested
- a `Repo-Sync-Assigned` trailer is appended to begin the escalation clock
- a sync PR with auto-merge enabled and passing CI is not flagged

### stuck stack recovery

- a sync PR whose base branch no longer exists (merged and deleted) is detected
- the restack logic is triggered for that PR
- a sync PR whose base branch still exists is not flagged

## bootstrap

- the bootstrap script generates a clean snapshot and pushes it as the initial commit
- the initial commit includes the `Repo-Sync-Origin` trailer
- the `repo-sync/watermark/private-to-public` tag is set in the public repo
- the `repo-sync/watermark/public-to-private` tag is set in the private repo (sentinel value)
- the bootstrap commit is recognized as sync-originated by the public-to-private workflow and skipped
- after bootstrap, a new commit on the private repo triggers a normal sync
- if the sync workflow runs before bootstrap (no watermark tag exists), it fails gracefully with a clear error (does not attempt to sync all historical commits)

## watermark recovery

- after a sync PR is merged and its branch is auto-deleted, the next workflow run correctly recovers the last-synced source SHA from the watermark tag's commit trailer
- the recovered SHA is used to identify unsynced commits, and no already-synced commits are reprocessed

## agent isolation (PR description)

- the Docker container has access to the clean snapshot (no private code)
- the Docker container has access to the public diff
- the Docker container does **not** have access to the private repo's git history
- the Docker container does **not** have access to private files, commit messages, or PR metadata
- the skill file is mounted read-only from the host, not fetched over the network
- the agent's output (title + description) does not contain private information (this is a property of the isolation boundary, not something we can deterministically test -- but we can verify the boundary is in place)

## error handling

### stripping tool errors

- a stripping error blocks the sync and does not update the watermark
- the next workflow run retries from the same commit
- a Slack notification is sent on failure
- subsequent commits are blocked until the error is resolved

### workflow crash recovery

- if the sync creation workflow crashes mid-run, the next run picks up where it left off (idempotency guards prevent duplicates)
- if the restack workflow crashes after updating the watermark but before rebasing, the escalation cron's stuck stack recovery detects and handles it

### agent failures

- if the conflict resolution agent errors, the PR is assigned to a human without an agent-proposed resolution
- if the conflict resolution agent produces code that doesn't compile, the workflow treats it as a failure
- if the PR description agent fails, the workflow creates the PR with a generic fallback description that includes a reference to the source commit (e.g., "repo-sync: sync from private (source: `<short-sha>`)").  the `Repo-Sync-Origin` trailer is still appended.  the sync is not blocked
