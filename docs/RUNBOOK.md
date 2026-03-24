# runbook

operational procedures for repo-sync failure scenarios.

## cherry-pick failure during public-to-private sync

### symptoms

the sync workflow fails with a cherry-pick conflict error.  the Slack notification reads something like: `repo-sync: cherry-pick failed for <sha> in <repo>`.  the watermark does not advance, so all subsequent commits are blocked.

### cause

a public commit's diff context overlaps with private-only code in the same file (e.g., `!repo-sync` marker regions near the modified lines).  this causes `git cherry-pick` to fail because the surrounding lines in the private repo don't match what the public commit expects.

this is rare -- it can only happen when a public commit modifies lines adjacent to inline `!repo-sync` marker regions.  sequential public commits cannot conflict with each other (they were merged in order on public `main`), so the conflict is always between the public commit and private-repo-specific code.

### remediation

#### option 1: fix the root cause (preferred)

adjust the private repo so the cherry-pick context matches:

1. identify the conflicting file and lines from the workflow logs
2. in the private repo, move the nearby `!repo-sync` marker regions so they don't overlap with the public commit's diff context.  options:
   - move the private-only code to a `private/` directory instead of using inline markers
   - rearrange the code so the marker region is further from the modified lines
3. merge the fix into the private repo's default branch
4. the next sync workflow run retries the cherry-pick against the updated `main`.  if the context now matches, the cherry-pick succeeds and the pipeline unblocks automatically

#### option 2: manually create the sync PR (escape hatch)

if fixing the root cause isn't practical or is too slow:

1. determine the source commit SHA and the sync branch name from the workflow logs (format: `repo-sync/public-to-private/<short-sha>`)
2. check out the private repo locally
3. create the sync branch from the current stack top (or `main` if no stack):
   ```sh
   git checkout -b repo-sync/public-to-private/<short-sha> origin/main
   ```
4. manually apply the public commit's changes, resolving any context mismatches:
   ```sh
   git cherry-pick <source-sha>
   # resolve conflicts, then:
   git cherry-pick --continue
   ```
5. ensure the commit message includes the `Repo-Sync-Origin` trailer:
   ```sh
   git commit --amend -m "$(git log -1 --format='%B')" -m "Repo-Sync-Origin: <source-repo>@<source-sha>"
   ```
6. push the branch and create the PR with the correct `Repo-Sync-Origin` trailer in the description
7. the sync workflow's idempotency guard will see the branch exists and skip this commit on the next run, unblocking the pipeline

### key properties

- the watermark does not advance past the failing commit, so no commits are lost
- the idempotency guard ensures manual intervention does not conflict with automation
- once the failing commit is handled (by either path), the sync workflow processes all remaining unsynced commits automatically

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
