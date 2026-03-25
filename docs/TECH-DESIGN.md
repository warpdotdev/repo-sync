# technical design

this is a working document capturing technical implementation details.  it will be fleshed out into a comprehensive design after the README (PRD) is reviewed.

## sync PR stack management

### serialization

sync workflow runs are serialized using a github actions [concurrency group](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/control-the-concurrency-of-workflows-and-jobs) with `cancel-in-progress: false`.  this ensures only one sync workflow runs at a time per direction per repo pair, with at most one pending.

### handling cancelled pending runs

because github actions only keeps one pending run per concurrency group, a workflow run may be cancelled before it executes if a newer trigger replaces it.  to avoid losing commits, **each workflow run processes all unsynced commits, not just the triggering one.**

### tracking synced state

sync branches use a naming convention that includes the source commit SHA:
* private-to-public: `repo-sync/private-to-public/<short-sha>`
* public-to-private: `repo-sync/public-to-private/<short-sha>`

the last-synced source commit is tracked via a **watermark tag** in the target repo:
* `repo-sync/watermark/private-to-public`
* `repo-sync/watermark/public-to-private`

the watermark tag points to the target repo's merge commit for the most recently merged sync PR.  the source commit SHA is recovered by reading the `Repo-Sync-Origin` trailer from that commit.  this allows merged sync branches to be safely deleted (including via GitHub's auto-delete-on-merge setting) without losing track of what's been synced.

when a workflow run starts, it reads the watermark tag to determine the last-synced source commit, then processes all source commits after it in order, creating one stacked sync PR per commit.

### per-commit sync PR creation

**private-to-public:**
1. identify unsynced commits on the private repo's default branch (all commits after the watermark's source SHA)
2. for each unsynced commit (in chronological order):
   a. generate clean snapshots of the private repo at both the current commit and its parent (stripping `private/` dirs and `!repo-sync: private-start`/`!repo-sync: private-end` regions from both)
   b. compute the diff between the two clean snapshots by committing both into a temporary git repo.  if the diff is empty, skip (all changes in this commit were internal-only)
   c. check if a PR with head branch `repo-sync/private-to-public/<short-sha>` was previously created (idempotency guard -- prevents duplicates if the workflow crashed and restarted mid-run).  if a MERGED PR exists, skip without updating the stack base.  if an OPEN PR exists, use its branch as the stack base and skip
   d. create a branch `repo-sync/private-to-public/<short-sha>` based on the top of the current stack (or `main` if no stack)
   e. apply the delta to the public repo by cherry-picking the diff commit from the temporary repo.  cherry-pick uses three-way merge internally, so it handles context mismatches from un-synced public changes gracefully.  if the cherry-pick produces no changes (delta already present in the public repo), skip.  if it conflicts, fail loudly
   f. amend the commit with a generic message (e.g., `"repo-sync: sync from private"`) and the `Repo-Sync-Origin` trailer.  **do not** use the source commit's message, as it could leak private information
   g. push the branch.  if the branch already exists on the remote (from a previous interrupted run where push succeeded but PR creation failed), verify the content matches and skip the push.  hard-fail if the content differs
   h. create a PR with the base set to the previous sync branch (or `main`)

the sync workflow does **not** approve or enable auto-merge on any PRs.  approval is handled by the separate approve workflow when a PR reaches the bottom of the stack.

**public-to-private:**
1. identify unsynced commits on the public repo's default branch
2. for each unsynced commit (in chronological order):
   a. check if a branch `repo-sync/public-to-private/<short-sha>` already exists or a PR with that head branch was previously created (idempotency guard)
   b. create a branch `repo-sync/public-to-private/<short-sha>` based on the top of the current stack (or `main` if no stack)
   c. cherry-pick the commit, preserving author and message.  if the cherry-pick fails (rare -- caused by private-only code overlapping with the public commit's diff context), the workflow **fails loudly** and notifies oncall.  see [RUNBOOK.md](RUNBOOK.md) for remediation steps
   d. create a PR with the base set to the previous sync branch (or `main`)

### merge strategy

sync PRs are merged using **squash and merge**.  this ensures each source commit produces exactly one commit in the target repo.  the squash commit message must include the full PR title and description body, so that the `Repo-Sync-Origin` trailer (included in the PR description) survives into the merged commit on the default branch.  consuming repos should configure their squash merge settings to preserve the PR body in the commit message.

this has an important interaction with restacking: when a sync PR is squash-merged, the resulting commit on `main` is a new commit object -- it is not the same as the commits on the PR's branch.  a naive `git rebase main` on the next PR in the stack would fail to recognize that the squashed changes are already in `main`, causing duplicate-change conflicts.

the fix is to use `git rebase --onto` when restacking:
```
git rebase --onto main <merged-pr-branch-tip> <next-pr-branch>
```
this tells git: "drop everything before `<merged-pr-branch-tip>` (i.e., the commits from the merged PR) and replay only the next PR's commits onto `main`."  since each sync PR has exactly one commit, this is clean.

### restacking after merge

the restack workflow has two trigger modes:

**post-merge mode** (triggered by `pull_request` `closed`+`merged` on sync branches):
1. updates the watermark tag to point to the merge commit (so the source SHA is recoverable from its `Repo-Sync-Origin` trailer)
2. identifies the next PR in the stack by searching for an open PR whose base is the merged PR's branch.  if not found (because GitHub auto-retargeted the PR to the default branch after auto-deleting the merged branch), falls back to finding the oldest open `repo-sync/` PR targeting the default branch that has more than one commit and no `repo-sync:conflict` label
3. rebases and pushes the PR (see rebase logic below)

**needs-restack mode** (triggered by `pull_request` `labeled` with `repo-sync:needs-restack`):
1. the approve workflow adds this label when it detects a PR with more than one commit (e.g., GitHub auto-retargeted it)
2. rebases and pushes the labeled PR (see rebase logic below).  no watermark update

**rebase logic (both modes):**
1. find the sync commit by searching backwards from HEAD for the most recent commit with a `Repo-Sync-Origin` trailer.  use its parent as the old base for `git rebase --onto main`.  this correctly handles both the normal case (sync commit at the tip) and the resolution case (sync commit followed by resolution commits) -- it drops old stack commits while preserving the sync commit and anything after it
2. after rebase, verify the branch SHA actually changed.  if the rebase was a no-op (SHA unchanged), do **not** push -- log an error and leave the `repo-sync:needs-restack` label for human investigation
3. if the branch changed, force-push the rebased branch
4. remove the `repo-sync:needs-restack` label (if present)
5. update the PR's base branch to `main` (if not already)

the restack workflow does **not** invoke the conflict resolution agent, assign reviewers, or enable auto-merge.  all of that is the approve workflow's responsibility.

the restack workflow is serialized per repo via a concurrency group (`cancel-in-progress: false`) to prevent concurrent rebases.

### approve workflow

the approve workflow is a separate reusable workflow that runs when a `repo-sync/` PR targets the default branch (i.e., it is at the bottom of the stack).  it is triggered by `pull_request` events (`opened`, `synchronize`, `edited`, `labeled`) and uses a **second GitHub App** (the "approver bot") for all operations.  a separate identity is required because GitHub does not allow a PR's author to approve it.

the workflow is serialized per PR via a concurrency group (`cancel-in-progress: false`).  multiple events can fire near-simultaneously for the same PR; the concurrency group ensures only one run proceeds at a time, with queued runs exiting early via the skip checks.

**decision logic (in order):**
1. **already handled?** if the PR has an existing approval OR a `Repo-Sync-Assigned` trailer → skip
2. **commit count?** if the PR has ≠ 1 commit:
   - if `repo-sync:needs-restack` label is already present → skip (restack already knows)
   - if label is absent → add `repo-sync:needs-restack` label → skip.  the label fires a `labeled` event that triggers the restack workflow
3. **mergeable?** check via GitHub API (with retries for `UNKNOWN`):
   - `MERGEABLE` → approve + enable auto-merge (API-only, no git operations)
   - `CONFLICTING` → conflict path (checkout, rebase for conflict markers, invoke agent, assign reviewer, add `Repo-Sync-Assigned` trailer, add `repo-sync:conflict` label).  do NOT approve
   - `UNKNOWN` after retries → skip (next event will re-trigger)

**loop prevention:** the approve workflow only adds the `repo-sync:needs-restack` label if it is **absent**.  this prevents re-triggering the restack workflow if it has already been notified.  GitHub also does not fire a `labeled` event for a label that is already present, providing a second layer of protection.

**key invariant:** the approve workflow NEVER force-pushes or rebases clean PRs.  git operations only happen in the conflict resolution path.

this provides a structural safety guarantee: the approver bot only approves PRs that have no conflicts and exactly one commit.  any PR that required conflict resolution (whether by an agent or a human) will not have a bot approval and cannot merge until a human explicitly approves it.  the safety property comes from GitHub's permission model rather than from the bot's code being correct.

consuming repos must have their default branch configured to **require PR approvals** for this mechanism to work.  the approver bot's approval satisfies this requirement for clean PRs.

the merged sync branch can be safely deleted after the watermark is updated (GitHub's auto-delete-on-merge is compatible with this approach).

### sync PR descriptions

**private-to-public:** an oz agent generates the PR description based solely on the public diff.  the agent does not have access to the private repo's PR description or commit messages, to avoid leaking internal information.

**public-to-private:** the PR title and description are copied from the source public PR.  a header is prepended to the description: "Synced from \<public repo name\>: \<URL to public repo PR\>".  the squash commit title matches the original public PR title.

### trailer parsing

PR descriptions may contain multiple trailers (e.g., `Repo-Sync-Origin`, `Repo-Sync-Assigned`).  since public-to-private sync copies the source PR description verbatim (which is untrusted input), the source description could contain spoofed `Repo-Sync-*` trailers.

to handle this, all trailer parsing uses the **last occurrence** of each trailer type.  the workflow always appends its trailers to the end of the description, so the last occurrence is always the one the workflow wrote.  this avoids needing to sanitize or mutate the source description.

### conflict resolution assignment

in both directions, the sync PR has a **reviewer requested** from the person who clicked merge on the source PR.  for public-to-private, this works because only people with private repo access have merge privileges on the public repo.  for direct pushes (no source PR), the commit author is requested as reviewer.  if neither can be determined, the fallback is `@oncall-client-primary`.

if the reviewer doesn't respond within a configurable timeout, the PR is reassigned to the configured `escalate_to` team (defaults to `@oncall-client-primary`).

note: GitHub PR "reviewer requests" support both individual users and teams, so `@oncall-client-primary` works as a team reviewer request.

### escalation mechanism

when a sync PR has a reviewer requested (either after agent conflict resolution or after agent failure), the workflow appends a `Repo-Sync-Assigned` trailer to the PR description:
```
Repo-Sync-Assigned: <github-username>@<ISO-8601-timestamp>
```

a separate **escalation cron workflow** (see reusable workflow interfaces) runs periodically (e.g., every 15 minutes) and checks all open sync PRs for this trailer.  if the elapsed time since the timestamp exceeds the configured `escalate_after` duration, a review is requested from the `escalate_to` team.

the cron resolution means actual escalation may be up to one cron interval late (e.g., up to 15 minutes if the cron runs every 15 minutes).  this is acceptable for the initial implementation.

### infinite loop prevention

when a sync commit merges into the target repo's default branch, it would normally trigger a reverse sync.  to prevent this infinite loop, a three-layer check is used:

1. **commit trailer:** sync commits include a `Repo-Sync-Origin: <repo>@<sha>` git trailer identifying the source commit.
2. **PR branch verification:** when the workflow sees a commit with this trailer, it verifies via the GitHub API that the commit was merged from a PR whose head branch matches the `repo-sync/` prefix.
3. **branch protection:** branch protection rules on `repo-sync/*` branches ensure only the sync workflow's GitHub token can create or push to them.

a commit is only recognized as sync-originated (and skipped) if **both** the trailer is present **and** the PR branch check passes.  this prevents spoofing -- a manually-added trailer without a corresponding `repo-sync/` branch will not suppress syncing.

## stripping tool

the stripping tool is a **python** CLI that generates a clean snapshot of the private repo at a given commit.  correctness is critical -- the tool must never allow private code to leak into the public repo -- so it will have thorough test coverage.

### algorithm

1. check out the target commit to a temporary working directory
2. walk the directory tree and remove all directories named `private/` (exact basename match at any depth).  **do not follow symlinks** during the walk
3. for each remaining file:
   a. if the file is a symlink, **raise an error** (symlinks are not allowed in synced repos -- they could bypass `private/` directory exclusion.  the CI validation action also checks for this)
   b. if the file is binary (see text vs. binary detection below), leave it in the snapshot as-is (no marker stripping attempted, but the file is still included in the clean snapshot)
   c. attempt to read the file as UTF-8.  if decoding fails, **raise an error** (fail-closed -- a file that can't be decoded might contain markers that would be silently skipped)
   d. strip all `!repo-sync: private-start` / `!repo-sync: private-end` regions:
      - scan lines for any line containing the string `!repo-sync: private-start` -- this begins a private region
      - strip all lines from the start marker (inclusive) through the corresponding `!repo-sync: private-end` line (inclusive), leaving no blank line in their place
      - if a `private-start` is encountered while already inside a private region, raise an error (nesting is not allowed)
      - if the file ends while inside a private region (no matching `private-end`), raise an error
   e. if stripping leaves the file with zero remaining lines, keep it as an empty file (do not delete it)
4. the resulting directory tree is the clean snapshot

### error handling

if the stripping tool encounters any error (nesting, unmatched markers, UTF-8 decode failure, symlinks), the sync workflow **fails** and does **not** update the watermark.  the next run will retry from the same commit.  this is correct fail-closed behavior -- a stripping error might indicate a condition that could cause private code to leak.

on failure, the workflow posts a notification to a configured Slack channel to alert oncall.  all commits after the failing commit are blocked until the issue is resolved.

### marker matching

markers are matched via simple substring search: any line containing `!repo-sync: private-start` or `!repo-sync: private-end` is treated as a marker, regardless of surrounding content (comment syntax, whitespace, etc.).  developers are trusted to use markers sensibly.

### text vs. binary detection

the stripping tool scans all text files in the repo for marker stripping.  binary files are **not stripped** but remain in the snapshot (they are included in the clean output as-is).

a file is classified as binary if its first 8192 bytes contain a null byte (`\x00`).  this is the standard heuristic used by git itself.

note: files that pass the binary check but fail UTF-8 decoding (e.g., UTF-16 encoded files) are treated as errors, not silently skipped.  this prevents a scenario where a file contains `!repo-sync` markers but is misclassified and left unstripped in the public snapshot.

### shared library

the marker parsing and validation logic (pairing checks, nesting detection, region stripping) is implemented as a shared python library used by both the stripping tool and the CI validation action.  the CI validation action invokes the library in a "validate only" mode -- it checks for errors without producing a stripped output.  this avoids having two implementations of the same logic that could drift apart.

## authentication

cross-repo operations (pushing branches, creating PRs in the counterpart repo) are authenticated via a **GitHub App** installed on both repos.

the consuming repo is responsible for generating a short-lived installation token and passing it as the `auth_token` secret to the reusable workflows.  this is typically done using the [`actions/create-github-app-token`](https://github.com/actions/create-github-app-token) action in the consuming repo's wrapper workflow, before calling the reusable workflow.

this is preferred over a PAT because:
* permissions are scoped to the specific repos the app is installed on
* tokens are short-lived (1 hour) and auto-rotate
* no dependency on a personal user account

the primary GitHub App needs the following permissions:
* `contents: write` -- push branches
* `pull_requests: write` -- create and manage PRs
* `workflows: write` -- push `.github/workflows/` files during sync
* `metadata: read` -- required for API access

a **second GitHub App** ("approver bot") is required for the approve workflow.  it needs `contents: write`, `pull_requests: write`, and `metadata: read`.  the approve workflow uses this single token for all of its operations (approval, conflict resolution pushes, PR edits).  a separate identity is necessary because GitHub does not allow a PR's author to approve it -- since the primary app creates the sync PRs, a different app must approve them.

## reusable workflow interfaces

### sync workflow

there are four separate reusable workflows: sync creation (triggered on push to default branch), restack (triggered on PR close), approve (triggered on PR events for sync PRs at the bottom of the stack), and escalation (cron).  separating them avoids unnecessary complexity from multiplexing trigger conditions and provides a clean audit boundary between PR creation and the merge decision.

**sync creation workflow** -- triggered by consuming repo on push to default branch:

inputs:
* `peer_repo` (required) -- the counterpart repo, e.g., `warpdotdev/warp-public`
* `peer_default_branch` (required) -- e.g., `main`
* `source_is_private` (required, boolean) -- if `true`, the workflow strips private code before syncing
* `escalate_to` (optional) -- GitHub team or user to escalate to on timeout.  defaults to `@oncall-client-primary`
* `slack_webhook_url` (optional) -- Slack webhook for stripping error notifications

secrets:
* `auth_token` -- GitHub App installation token (or a token with cross-repo push + PR permissions)

**restack workflow** -- triggered by consuming repo on `pull_request` closed+merged for `repo-sync/` branches, or dispatched by the escalation cron for stuck stack recovery.  uses a concurrency group to serialize all restack operations:

inputs:
* `peer_repo` (required)
* `peer_default_branch` (required)
* `source_is_private` (required, boolean)
* `escalate_to` (optional)

secrets:
* `auth_token`

**escalation workflow** -- triggered by consuming repo on a cron schedule (e.g., every 15 minutes):

inputs:
* `escalate_to` (optional) -- defaults to `@oncall-client-primary`
* `escalate_after` (required) -- duration before escalation, e.g., `5m`, `1h`

secrets:
* `auth_token`

the escalation workflow checks all open sync PRs (identified by `repo-sync/` head branches) and performs three checks:

1. **timeout escalation:** if the PR has a `Repo-Sync-Assigned` trailer and the elapsed time since the timestamp exceeds `escalate_after`, a review is requested from `escalate_to`.
2. **CI failure detection:** if the PR has auto-merge enabled but CI has failed (required status checks are not passing), the workflow disables auto-merge, requests a review from the appropriate person (using the same assignment logic as conflict resolution), and appends a `Repo-Sync-Assigned` trailer to begin the escalation clock.
3. **stuck stack recovery:** if a sync PR's base branch no longer exists (the PR below it was merged and the branch deleted) but the PR has not been restacked, the escalation cron adds the `repo-sync:needs-restack` label to the PR.  this triggers the restack workflow via the consuming repo's `labeled` event handler, using the same mechanism as the approve workflow.  the actual restack logic lives only in the restack workflow, keeping a single codepath for all restacking.

### CI validation action

a reusable composite action that validates `!repo-sync` marker correctness.  intended to be added to CI workflows in private repos.

inputs:
* `paths` (optional) -- restrict validation to specific file paths/globs.  defaults to all files in the repo

validation checks:
* every `private-start` has a matching `private-end` in the same file
* no nested markers (a `private-start` inside an open region)
* no symlinks present in the repository (symlinks could bypass `private/` directory exclusion)

### consuming repo integration

a consuming repo's workflow file would look roughly like:

```yaml
# .github/workflows/repo-sync.yml
name: repo-sync
on:
  push:
    branches: [main]
jobs:
  sync:
    uses: warpdotdev/repo-sync/.github/workflows/sync.yml@v1
    with:
      peer_repo: warpdotdev/warp-public
      peer_default_branch: main
      source_is_private: true
    secrets:
      auth_token: ${{ secrets.REPO_SYNC_TOKEN }}
```

consuming repos pin to a **major version** (e.g., `@v1`) of the repo-sync workflows.  this ensures changes to repo-sync don't break consumers unexpectedly, while allowing minor/patch updates.

## oz agent skills

skills live in `.agents/skills/` in this repository.  they are invoked from the sync workflows via `oz agent run --skill warpdotdev/repo-sync:<skill-name>`, or programmatically via the [Oz Python SDK](https://github.com/warpdotdev/oz-sdk-python).

### conflict resolution skill

location: `.agents/skills/conflict-resolution/SKILL.md`

this is a generic merge conflict resolution agent -- it does not need to know about sync direction or repo-sync internals.  it just resolves merge conflicts.

**context the agent receives:**
* the repo, checked out to the sync branch with conflict markers present
* the list of conflicting files (from `git diff --name-only --diff-filter=U`)

**what the agent does:**
1. reads the conflicting files and resolves the merge conflict markers
2. ensures the code compiles and is properly formatted
3. runs any tests it believes may be affected by the resolution
4. commits the resolution
5. pushes to the sync branch

**failure handling:** if the agent errors, fails to produce a clean resolution, or produces code that doesn't compile, the workflow treats it as a failure and proceeds to assign the PR to a human without an agent-proposed resolution.

### PR description skill

location: `.agents/skills/pr-description/SKILL.md`

used only for private-to-public sync PRs, where the description must be generated from the public diff without access to private commit messages or PR descriptions.

### isolation

the PR description agent is run inside a **Docker container** with the **clean snapshot** mounted as the working directory.  this provides a hard isolation boundary -- the agent has access to the full (stripped) codebase for context, but cannot access the private repo's git history, private files, commit messages, or PR metadata.

the workflow:
1. generates the clean snapshot (as part of the normal stripping process)
2. builds/pulls a Docker image with `oz` and necessary tooling
3. mounts the following as read-only volumes into the container:
   - the clean snapshot (as the working directory)
   - the public diff
   - the skill file (`.agents/skills/pr-description/SKILL.md` from the `repo-sync` checkout)
4. runs the agent inside the container with the mounted skill (no network fetch required for skill resolution)
5. captures the agent's output (title + description) from the container

**context the agent receives:**
* the clean snapshot mounted as the working directory (full codebase context, no private code)
* the public diff being synced

**what the agent produces:**
* a PR title
* a human-readable PR description summarizing what changed

the agent is **not** responsible for adding the `Repo-Sync-Origin` trailer -- that is appended by deterministic code in the workflow after the agent produces its description.  this ensures the trailer is always present and correctly formatted, regardless of agent behavior.

**failure handling:** if the PR description agent fails, the workflow falls back to a generic description that includes a reference to the source commit in the private repo (e.g., "repo-sync: sync from private (source: `<short-sha>`)").  the `Repo-Sync-Origin` trailer is still appended by deterministic code.  the sync is not blocked by a description agent failure.

### public-to-private PR descriptions

public-to-private sync PRs do not use an agent.  the PR title and description are constructed deterministically by the workflow:
* title: copied from the source public PR (or commit message for direct pushes)
* description: copied from the source PR description, with a header prepended: "Synced from \<public repo name\>: \<URL to source PR or commit\>"
* the `Repo-Sync-Origin` trailer is appended to the description

## bootstrap

the first sync requires a one-time bootstrap to create the public repo from the private repo.  this is a separate script/workflow (not the regular sync workflow).

bootstrap steps:
1. generate a clean snapshot of the private repo at `HEAD`
2. push the snapshot as the initial commit to the new (or existing empty) public repo, with a `Repo-Sync-Origin: <private-repo>@<HEAD-sha>` trailer in the commit message
3. set the watermark tag `repo-sync/watermark/private-to-public` in the public repo pointing to this initial commit
4. set the watermark tag `repo-sync/watermark/public-to-private` in the private repo pointing to a sentinel value (e.g., the empty tree SHA or a special "bootstrap" tag) so the public→private workflow knows there are no public commits to sync yet

the bootstrap commit in the public repo includes the `Repo-Sync-Origin` trailer, so the public→private workflow will recognize it as sync-originated and skip it (preventing it from being replayed back into the private repo).

after bootstrap, the regular sync workflows take over.
