# repo-sync

this repository provides reusable github workflows, actions, and tooling to synchronize code between a public repo and a private repo.  each repository integrates the reusable workflows defined here and configures them to sync with the other repository.

initial consumers: `warp-internal` (private) and its public counterpart, with `warp-proto-apis` as a possible future consumer.

see [DECISIONS.md](DECISIONS.md) for a log of design decisions, alternatives considered, and justifications.

# sync trigger

syncing is triggered **per-commit**: each merge to the default branch of either repo creates a sync PR to the other repo.  this applies in both directions (private-to-public and public-to-private).

since github actions concurrency groups may cancel pending runs if a newer trigger arrives, a single workflow run may process **multiple unsynced commits**, creating a stacked sync PR for each one.

to prevent infinite loops (a sync commit triggering a reverse sync), sync commits include a `Repo-Sync-Origin: <repo>@<sha>` git trailer.  the workflow skips commits that have this trailer **and** were merged from a `repo-sync/` branch (verified via the GitHub API).  branch protection rules on `repo-sync/*` ensure only the sync workflow can create these branches.  neither the trailer nor the branch check alone is sufficient -- both must match for a commit to be recognized as sync-originated.

# sync directions

## private-to-public

this is the more complex direction, because internal-only code must be stripped before syncing.

the workflow:
1. on each merge to the private repo's default branch, generate a **clean snapshot** of the private repo at that commit (stripping all internal code -- see [marking internal-only code](#marking-internal-only-code) below)
2. diff the clean snapshot against the previous clean snapshot (or the public repo's current `main` if this is the first unsynced commit)
3. if the diff is empty (i.e., all changes were internal-only), skip -- no PR is created
4. otherwise, create a sync PR to the public repo (see [stacked sync PRs](#stacked-sync-prs) below)
5. an agent writes a PR description based on the concrete public diff being merged.  individual commit messages from the private repo are **not** included, to avoid leaking private information

## public-to-private

this is a straightforward merge -- no code stripping is needed.

the workflow:
1. on each merge to the public repo's default branch, create a sync PR to the private repo
2. if the source commit came from a PR, the sync PR's title and description are copied from the source PR, with a header linking back to the original.  for direct pushes (no source PR), the commit message is used instead, with a header linking to the commit
3. original commit author and message are preserved on the sync PR's branch; when the sync PR is merged using squash-and-merge, the final squash commit uses the sync PR's title

# stacked sync PRs

sync PRs are managed as a **stack**: each new sync PR is based on the branch of the previous (unmerged) sync PR, not directly on `main`.

this provides natural queuing semantics:
* if a sync PR requires conflict resolution, subsequent sync PRs queue up behind it in the stack
* each PR in the stack shows only the diff for the single commit it corresponds to, making review easier
* once a conflict is resolved and the base PR merges, automation restacks and proceeds with the next PR

sync PRs use **squash and merge**.  the squash commit message includes the full PR title and description (ensuring the `Repo-Sync-Origin` trailer is preserved for infinite loop prevention).  clean (no-conflict) sync PRs receive an approving review from the bot and are **auto-merged** without human review.  conflict-resolved PRs do not receive a bot approval, ensuring that a human must approve them before they can merge.

stack management is implemented using a **custom tool** built on `git` and `gh` (no third-party dependency like graphite).  the tool handles:
* creating branches stacked on the previous sync branch (or `main` if no stack exists)
* creating PRs with the correct base branch
* restacking (rebasing + updating PR base) when a PR in the stack merges

# conflict resolution

when a sync PR encounters a **git merge conflict**:
1. a **local oz agent** is invoked from within the github actions workflow (using the oz CLI), to avoid paying for idle github actions minutes while a cloud agent runs
2. the agent adds a commit to the sync PR with a proposed conflict resolution
3. the person who merged the source PR is requested as reviewer; for direct pushes (no source PR), the commit author is requested instead.  if neither can be determined, request review from `@oncall-client-primary`
4. if the reviewer doesn't respond within a configurable timeout, review is requested from the configured escalation team (defaults to `@oncall-client-primary`)
5. if the agent fails (e.g., it errors or produces a resolution that does not apply cleanly), the PR is still assigned to the merger, but without an agent-proposed resolution

build failures after a clean rebase do **not** trigger the conflict resolution agent; they are assigned directly to a human reviewer.

the oz agent uses a skill defined in this repository.

# marking internal-only code

there are two mechanisms for marking code as internal-only:

## `private` directories

any directory named `private` at **any depth** in the repo is excluded from syncs to the public repo.  for example, `crates/private/` would be fully private.

## `!repo-sync` markers

a range of lines in any file can be marked as internal-only using comment markers:
* `!repo-sync: private-start` at the beginning of the range
* `!repo-sync: private-end` at the end of the range

the comment syntax depends on the file's language:
* rust: `// !repo-sync: private-start`
* python: `# !repo-sync: private-start`

the entire range of lines (including the marker lines) is stripped when generating the clean snapshot for public sync.

example -- private repo:
```rust
fn my_func() {
  // !repo-sync: private-start
  println!("this code exists only in our private repo");
  // !repo-sync: private-end
  println!("this code is public");
}
```

public repo after sync:
```rust
fn my_func() {
  println!("this code is public");
}
```

### rules

* markers **must** be properly paired -- every `private-start` must have a corresponding `private-end` in the same file
* nesting is **not allowed** -- a `private-start` inside an existing `private-start`/`private-end` range is an error
* if stripping markers leaves a file completely empty, the file is kept as an empty file (developers should prefer placing fully-private files in a `private/` directory instead)
* references to `private/` paths in config files (e.g., `Cargo.toml` workspace members, `.gitignore`) are the developer's responsibility to wrap in `!repo-sync: private-start`/`!repo-sync: private-end` markers

### CI validation

this repository provides a **reusable github action** that can be integrated into CI workflows in private repos.  the action validates:
* all `!repo-sync` markers are properly paired (every `private-start` has a matching `private-end`)
* no nested markers exist
* no symlinks are present in the repository (symlinks could bypass `private/` directory exclusion)

# components

this repository contains:

* **reusable github workflows** -- sync creation (triggered per-commit on the default branch), restack (triggered on sync PR merge), and escalation (cron)
* **reusable github action** -- CI validation of `!repo-sync` marker correctness
* **stripping tool** -- generates a clean snapshot of the private repo with all internal code removed
* **stack management tool** -- manages the stacked sync PR lifecycle using `git` and `gh`
* **oz agent skill** -- conflict resolution skill for the local oz agent

# consuming repo requirements

consuming repos will need to provide:

* github credentials with permission to create branches and PRs in the counterpart repo
* branch protection rules covering `repo-sync/*` branches so only the sync workflow can create or update them
* **auto-merge enabled** as a repo-level setting (sync PRs use GitHub's auto-merge feature)
* any workflow configuration needed to identify the counterpart repo and authenticate cross-repo operations
* source repos must use **squash merge** for PRs to their default branch (this ensures each merge produces exactly one commit, which is the unit of sync)
* squash merge must be configured to **preserve the PR description in the commit message** (required for `Repo-Sync-Origin` trailer to survive into the merged commit, which is needed for infinite loop prevention and watermark tracking)
* the default branch must have **required PR approvals** enabled.  the bot submits an approving review on clean (conflict-free) sync PRs, satisfying this requirement.  the "require review from someone other than the last pusher" branch protection setting must **not** be enabled, since the bot both pushes the sync branch and approves the PR
