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
   a. generate a clean snapshot of the private repo at that commit (strip `private/` dirs and `!repo-sync: private-start`/`!repo-sync: private-end` regions)
   b. diff the clean snapshot against the previous clean snapshot (or the public repo's current `main` if this is the first unsynced commit)
   c. if the diff is empty, skip (all changes in this commit were internal-only)
   d. create a branch `repo-sync/private-to-public/<short-sha>` based on the top of the current stack (or `main` if no stack)
   e. apply the diff and commit
   f. create a PR with the base set to the previous sync branch (or `main`)

**public-to-private:**
1. identify unsynced commits on the public repo's default branch
2. for each unsynced commit (in chronological order):
   a. create a branch `repo-sync/public-to-private/<short-sha>` based on the top of the current stack (or `main` if no stack)
   b. cherry-pick the commit, preserving author and message
   c. create a PR with the base set to the previous sync branch (or `main`)

### merge strategy

sync PRs are merged using **squash and merge**.  this ensures each source commit produces exactly one commit in the target repo.  the squash commit message must include the full PR title and description body, so that the `Repo-Sync-Origin` trailer (included in the PR description) survives into the merged commit on the default branch.  consuming repos should configure their squash merge settings to preserve the PR body in the commit message.

this has an important interaction with restacking: when a sync PR is squash-merged, the resulting commit on `main` is a new commit object — it is not the same as the commits on the PR's branch.  a naive `git rebase main` on the next PR in the stack would fail to recognize that the squashed changes are already in `main`, causing duplicate-change conflicts.

the fix is to use `git rebase --onto` when restacking:
```
git rebase --onto main <merged-pr-branch-tip> <next-pr-branch>
```
this tells git: "drop everything before `<merged-pr-branch-tip>` (i.e., the commits from the merged PR) and replay only the next PR's commits onto `main`."  since each sync PR has exactly one commit, this is clean.

### restacking after merge

when a sync PR is merged (detected via a `pull_request` `closed`+`merged` event on sync branches), the workflow:
1. updates the watermark tag to point to the merge commit (so the source SHA is recoverable from its `Repo-Sync-Origin` trailer)
2. identifies the next PR in the stack
3. rebases its branch onto the updated `main` using `git rebase --onto` (see merge strategy above)
4. updates the PR's base branch to `main` (or the new bottom of the stack)
5. if the rebase succeeds cleanly, attempts to merge automatically
6. if the rebase has conflicts, invokes the conflict resolution agent

the merged sync branch can be safely deleted after the watermark is updated (GitHub's auto-delete-on-merge is compatible with this approach).

### sync PR descriptions

**private-to-public:** an oz agent generates the PR description based solely on the public diff.  the agent does not have access to the private repo's PR description or commit messages, to avoid leaking internal information.

**public-to-private:** the PR title and description are copied from the source public PR.  a header is prepended to the description: "Synced from \<public repo name\>: \<URL to public repo PR\>".  the squash commit title matches the original public PR title.

### conflict resolution assignment

in both directions, the sync PR is assigned to the person who clicked merge on the source PR.  for public-to-private, this works because only people with private repo access have merge privileges on the public repo.  for direct pushes (no source PR), the commit author is assigned.  if neither can be determined, the fallback is `@oncall-client-primary`.

if the assignee doesn't respond within a configurable timeout, the PR is reassigned to `@oncall-client-primary`.  escalation implementation details TBD.

### infinite loop prevention

when a sync commit merges into the target repo's default branch, it would normally trigger a reverse sync.  to prevent this infinite loop, a three-layer check is used:

1. **commit trailer:** sync commits include a `Repo-Sync-Origin: <repo>@<sha>` git trailer identifying the source commit.
2. **PR branch verification:** when the workflow sees a commit with this trailer, it verifies via the GitHub API that the commit was merged from a PR whose head branch matches the `repo-sync/` prefix.
3. **branch protection:** branch protection rules on `repo-sync/*` branches ensure only the sync workflow's GitHub token can create or push to them.

a commit is only recognized as sync-originated (and skipped) if **both** the trailer is present **and** the PR branch check passes.  this prevents spoofing — a manually-added trailer without a corresponding `repo-sync/` branch will not suppress syncing.
