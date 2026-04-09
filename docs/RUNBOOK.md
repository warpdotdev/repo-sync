# runbook

operational procedures for repo-sync failure scenarios.

## cherry-pick conflict during public-to-private sync

### symptoms

the sync workflow creates a conflict PR with the `repo-sync:conflict` label and `Repo-Sync-Conflict` trailer.  the Slack notification reads something like: `repo-sync: cherry-pick conflict for <sha> in <repo>. Conflict PR #<n> created.`  a reviewer is automatically assigned.

### cause

a public commit's diff context overlaps with private-only code in the same file (e.g., `!repo-sync` marker regions near the modified lines).  this causes `git cherry-pick` to fail because the surrounding lines in the private repo don't match what the public commit expects.

this is rare -- it can only happen when a public commit modifies lines adjacent to inline `!repo-sync` marker regions.  sequential public commits cannot conflict with each other (they were merged in order on public `main`), so the conflict is always between the public commit and private-repo-specific code.

### remediation

#### option 1: resolve the conflict PR (preferred)

the conflict PR contains the raw cherry-pick conflict (with conflict markers or modify/delete artifacts) and possibly an agent-proposed resolution commit on top.

1. check out the conflict PR's branch locally
2. resolve the conflicts (edit files to remove conflict markers, or handle modify/delete cases)
3. push the resolution as a new commit
4. approve the PR and merge it
5. subsequent sync PRs in the stack will be restacked automatically

the approve bot will **never** auto-approve a conflict PR (the `Repo-Sync-Conflict` trailer prevents this).  human approval is always required.

#### option 2: fix the root cause

adjust the private repo so the cherry-pick context matches:

1. identify the conflicting file and lines from the conflict PR
2. in the private repo, move the nearby `!repo-sync` marker regions so they don't overlap with the public commit's diff context.  options:
   - move the private-only code to a `private/` directory instead of using inline markers
   - rearrange the code so the marker region is further from the modified lines
3. merge the fix into the private repo's default branch
4. close the conflict PR.  the next sync workflow run retries the cherry-pick against the updated `main`.  if the context now matches, the cherry-pick succeeds and creates a clean PR

#### option 3: manually create the sync PR (escape hatch)

if the conflict PR is unusable for some reason:

1. close the conflict PR
2. determine the source commit SHA and the sync branch name from the workflow logs (format: `repo-sync/public-to-private/<short-sha>`)
3. check out the private repo locally
4. create the sync branch from the current stack top (or `main` if no stack):
   ```sh
   git checkout -b repo-sync/public-to-private/<short-sha> origin/main
   ```
5. manually apply the public commit's changes, resolving any context mismatches:
   ```sh
   git cherry-pick <source-sha>
   # resolve conflicts, then:
   git cherry-pick --continue
   ```
6. ensure the commit message includes the `Repo-Sync-Origin` trailer:
   ```sh
   git commit --amend -m "$(git log -1 --format='%B')" -m "Repo-Sync-Origin: <source-repo>@<source-sha>"
   ```
7. push the branch and create the PR with the correct `Repo-Sync-Origin` trailer in the description
8. the sync workflow's idempotency guard will see the branch exists and skip this commit on the next run, unblocking the pipeline

### key properties

- the watermark does not advance past the conflicting commit, so no commits are lost
- subsequent commits are stacked on top of the conflict PR and will be processed once it merges
- the idempotency guard ensures manual intervention does not conflict with automation
- once the conflict PR is resolved and merged, the sync workflow processes all remaining unsynced commits automatically

## cherry-pick conflict during private-to-public sync

### symptoms

the sync workflow creates a conflict PR with the `repo-sync:conflict` label and `Repo-Sync-Conflict` trailer.  the Slack notification reads something like: `repo-sync: cherry-pick conflict for <sha> in <repo>. Conflict PR #<n> created.`  a reviewer is automatically assigned.

### cause

a private commit modifies lines in a file that also has un-synced public changes nearby.  the sync workflow generates a clean delta (diff between consecutive stripped snapshots of the private repo) and cherry-picks it onto the public repo.  if the public repo's state in the affected area differs from the clean snapshot's context (because of un-synced public changes), the cherry-pick fails.

this happens when both repos independently modify the same area of a file before the changes have been synced in both directions.

### remediation

#### option 1: resolve the conflict PR (preferred)

the conflict PR contains the raw cherry-pick conflict (with conflict markers or modify/delete artifacts) and possibly an agent-proposed resolution commit on top.

1. check out the conflict PR's branch locally
2. resolve the conflicts (edit files to remove conflict markers, or handle modify/delete cases)
3. push the resolution as a new commit
4. approve the PR and merge it
5. subsequent sync PRs in the stack will be restacked automatically

the approve bot will **never** auto-approve a conflict PR (the `Repo-Sync-Conflict` trailer prevents this).  human approval is always required.

#### option 2: merge pending public-to-private sync PRs

the most common root cause is pending public-to-private sync PRs that haven't merged yet.  once they merge, the private repo includes the public changes, and a subsequent retry would generate a patch with the correct context.

1. close the conflict PR
2. check for open public-to-private sync PRs in the private repo
3. merge them (or wait for them to auto-merge)
4. the next private-to-public sync run retries with updated context and should succeed

#### option 3: manually create the sync PR (escape hatch)

if the conflict PR is unusable for some reason:

1. close the conflict PR
2. determine the source commit SHA and sync branch name from the workflow logs (format: `repo-sync/private-to-public/<short-sha>`)
3. check out the public repo locally
4. create the sync branch from the current stack top (or `main` if no stack):
   ```sh
   git checkout -b repo-sync/private-to-public/<short-sha> origin/main
   ```
5. manually apply the changes with conflict resolution
6. commit with the `Repo-Sync-Origin` trailer:
   ```sh
   git commit -m "repo-sync: sync from private" -m "Repo-Sync-Origin: <source-repo>@<source-sha>"
   ```
7. push the branch and create the PR with the `Repo-Sync-Origin` trailer in the description
8. the sync workflow's idempotency guard will see the branch exists and skip this commit on the next run

### key properties

- the watermark does not advance past the conflicting commit, so no commits are lost
- subsequent commits are stacked on top of the conflict PR and will be processed once it merges
- the idempotency guard ensures manual intervention does not conflict with automation
- merging pending public-to-private sync PRs is usually sufficient to prevent these conflicts

## missing Repo-Sync-Origin trailer on merge commit

### symptoms

the restack workflow fails after a sync PR is merged.  the error reads: `Merge commit <sha> has no Repo-Sync-Origin trailer. Cannot update watermark.`  the watermark does not advance, so subsequent syncs in that direction are blocked.

### cause

the target repo's squash merge settings did not preserve the PR description in the commit message.  the `Repo-Sync-Origin` trailer is in the PR body, but the squash commit only includes the title (and possibly co-author lines).  this can also happen if someone manually edits the commit message during merge and removes the trailer.

### remediation

1. find the merged PR and confirm the trailer is in the PR body:
   ```sh
   gh api repos/<owner>/<repo>/pulls/<pr-number> --jq '.body'
   ```
2. extract the source repo and SHA from the trailer (format: `Repo-Sync-Origin: <source-repo>@<source-sha>`).
3. get the tree SHA from the merge commit:
   ```sh
   TREE_SHA=$(gh api repos/<owner>/<repo>/commits/<merge-sha> --jq '.commit.tree.sha')
   ```
4. create a new commit object with the correct trailer via the GitHub API.  note: the message must be defined separately to avoid zsh `cmdsubst>` issues with literal newlines inside `$()`:
   ```sh
   MSG=$'repo-sync: watermark recovery for PR #<pr-number>\n\nRepo-Sync-Origin: <source-repo>@<source-sha>'
   NEW_COMMIT=$(gh api repos/<owner>/<repo>/git/commits \
     -f "message=$MSG" \
     -f "tree=$TREE_SHA" \
     -f "parents[]=<merge-sha>" \
     --jq '.sha')
   ```
5. update the watermark tag to point to the new commit:
   ```sh
   gh api -X PATCH repos/<owner>/<repo>/git/refs/tags/repo-sync/watermark/<direction> \
     -f "sha=$NEW_COMMIT" \
     -F "force=true"
   ```
6. verify the watermark reads correctly:
   ```sh
   gh api repos/<owner>/<repo>/git/ref/tags/repo-sync/watermark/<direction> --jq '.object.sha'
   ```

the new commit is a dangling object (not on any branch) whose sole purpose is to carry the trailer for the watermark.  it does not affect git history.

### prevention

ensure the target repo's merge settings preserve the PR description in the squash commit message.  in GitHub: Settings → Pull Requests → verify that the default squash merge commit message includes the PR body.

## stripping tool failure

### symptoms

the sync workflow fails during snapshot generation.  the Slack notification reads: `repo-sync: stripping failed for <sha> in <repo>`.

### cause

the stripping tool encountered one of: unpaired `!repo-sync` markers, nested markers, a symlink, or a UTF-8 decode failure.

### remediation

1. check the workflow logs for the specific error
2. fix the issue in the private repo (e.g., add a missing `private-end` marker, remove a symlink)
3. merge the fix.  the next sync run retries from the same commit

to prevent this in CI, ensure the `validate-markers` action is added to the private repo's PR checks (see [README.md](../README.md#step-2-add-the-ci-validation-action-private-repo)).
