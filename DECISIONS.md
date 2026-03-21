# decisions log

this document records the key design decisions made for repo-sync, the alternatives we considered, and why we chose what we did.

## sync trigger: per-commit vs. periodic

**decision:** per-commit (on each merge to the default branch), for both directions.

**alternatives considered:**
1. **per-commit for both directions** (chosen)
2. **periodic sync** (e.g., every 15–30 minutes) — a batched version of per-commit
3. **per-commit for private-to-public, periodic for public-to-private** — hybrid approach, since the public repo has higher commit volume

**justification:** with stacked sync PRs (see below), the "backed up due to merge conflict" problem is handled gracefully — new sync PRs simply queue up in the stack.  this eliminates the main advantage of periodic batching (avoiding pileups) while preserving per-commit granularity for easier review.  private-to-public commit volume is expected to be low; public-to-private is higher but manageable with stacking.

## sync mechanism (private-to-public): clean snapshot vs. filtered diff vs. git filter

**decision:** generate a clean snapshot on-demand and diff it against the public repo.

**alternatives considered:**
1. **filtered-diff approach** — take the diff of new commits on private, strip hunks touching internal code, apply to public
2. **clean-snapshot approach** (chosen) — generate a full stripped copy of the private repo, diff against public
3. **git filter approach** — use `git filter-repo` or clean/smudge filters to produce a stripped branch

**justification:** the filtered-diff approach is brittle — it operates on diffs, so moved files or context-dependent hunks can break.  the git filter approach is powerful but adds complexity.  the clean-snapshot approach is the simplest and most robust: you always know the ground truth of what public should look like.  it's also self-healing — if something gets out of sync, the next run produces the correct diff automatically.  the tradeoff is that it's more computationally expensive (regenerating the full snapshot each time), but this is not a concern until proven otherwise.

## sync state tracking: stateless (full snapshot) vs. tracking last-synced commit

**decision:** stateless — always generate a full clean snapshot and diff against public.

**alternatives considered:**
1. **track last-synced commit SHA** (e.g., in a file in the public repo, or as a git tag in the private repo) — enables replaying individual commits since the last sync
2. **stateless full snapshot** (chosen) — no sync state to maintain; the diff *is* the delta

**justification:** the stateless approach is simpler and self-healing.  tracking state would enable preserving individual commit messages in the public repo, but we decided against that anyway (see commit history decision below), so the main benefit of tracking is moot.

## commit history in public repo: individual commits vs. single sync commit

**decision:** single sync commit per PR, with an agent-generated description based on the public diff.

**alternatives considered:**
1. **preserve individual commit messages** from the private repo
2. **single sync commit** with agent-generated description (chosen)

**justification:** private commit messages could leak information we intend to keep private (internal feature names, ticket references, etc.).  a single sync commit with a description based on the concrete public diff avoids this risk entirely.

## public-to-private commit metadata: preserve vs. rewrite

**decision:** preserve original author and commit message.

**justification:** public commits are already public, so there's no information leakage concern.  preserving metadata gives better attribution in the private repo.

## PR queuing: stacked PRs vs. independent ordered PRs vs. single updatable PR

**decision:** stacked PRs, managed by a custom tool.

**alternatives considered:**
1. **single updatable PR** — if a sync PR exists, force-push to update it
2. **independent PRs targeting main** with ordered merge — each PR targets `main`, but a merge queue enforces ordering
3. **stacked PRs** (chosen) — each new sync PR bases on the previous sync PR's branch

**justification:** stacked PRs provide the best review experience — each PR shows only the diff for the single commit it corresponds to, not the accumulated diff of all unsynced changes.  independent PRs targeting `main` would show the full delta from `main` to each sync point, making review noisy.  a single updatable PR collapses all pending changes into one, losing per-commit granularity.  the stacked approach also provides natural FIFO queuing: if a PR is blocked by a conflict, subsequent PRs queue behind it.

## stack management: graphite vs. custom tool

**decision:** custom tool built on `git` and `gh`.

**alternatives considered:**
1. **graphite CLI (`gt`)** — purpose-built for stacked PR management
2. **custom tool using `git` + `gh`** (chosen)

**justification:** graphite requires authentication to graphite's service, adding a third-party dependency to CI.  the stacking behavior we need is narrow (create stacked branches, create PRs with correct base, restack on merge), so the custom tool is straightforward.  this avoids the auth complexity and gives us full control over the behavior.

## conflict resolution: agent on same PR vs. stacked resolution PR

**decision:** agent adds a resolution commit directly to the sync PR.

**alternatives considered:**
1. **commit on the same PR** (chosen) — agent pushes a resolution commit; human reviewer can amend or approve
2. **stacked resolution PR** — agent opens a separate PR on top of the conflicted sync PR

**justification:** a commit on the same PR is simpler — the reviewer sees the full picture (sync diff + resolution) in one place.  a stacked resolution PR adds dependency management complexity (rebasing if the base is updated).  since the human reviewer can easily amend or revert the agent's commit, the simpler approach is sufficient.

## conflict resolution agent: local oz agent vs. cloud oz agent vs. github actions LLM step

**decision:** local oz agent invoked via the oz CLI from within the github actions workflow.

**alternatives considered:**
1. **cloud oz agent** kicked off from the workflow — workflow triggers a cloud agent and waits
2. **local oz agent** via oz CLI (chosen) — runs directly in the github actions runner
3. **github actions step calling an LLM API directly** — no oz involvement

**justification:** a cloud agent would mean paying for github actions minutes while the workflow idles waiting for the cloud agent to finish (which itself incurs compute costs).  a local oz agent runs within the workflow's runner, avoiding the double billing.  using oz (vs. raw LLM calls) gives us the skill system and agent tooling for free.

## private directory naming: `internal` vs. `private`

**decision:** use `private` as the directory name.

**alternatives considered:**
1. **`internal`** — common convention (especially in Go)
2. **`private`** (chosen) — consistent with the `!repo-sync: private-start`/`private-end` marker naming

**justification:** using `private` is consistent with the `private-start`/`private-end` directive naming, making the system's terminology uniform.  `internal` has specific meaning in Go (import restrictions), which could cause confusion.

## `private` directory scope: top-level only vs. any depth

**decision:** any directory named `private` at any depth.

**justification:** a top-level-only restriction would be too limiting.  we expect patterns like `crates/private/` for fully-private crates, which requires matching at any depth.

## `!repo-sync` marker naming

**decision:** use `!repo-sync: private-start` and `!repo-sync: private-end` as the marker syntax.

**alternatives considered:**
1. **`!warp-internal: start`/`!warp-internal: end`** — ties the marker to a specific repo name
2. **`!repo-sync: private-start`/`!repo-sync: private-end`** (chosen) — namespaced to the tooling, with descriptive commands

**justification:** `!repo-sync` as a namespace ties the markers to the tooling that consumes them, not a specific repo.  this makes the markers meaningful when adopted by other repos (e.g., `warp-proto-apis`).  `private-start`/`private-end` is more descriptive than bare `start`/`end`, and the namespaced format (`!repo-sync: <command>`) leaves room for future marker types if needed.

## `!repo-sync` marker nesting: allowed vs. error

**decision:** nesting is an error, enforced by CI validation.

**justification:** nesting adds complexity to the stripping logic for no clear benefit.  if code is already inside an internal range, a nested marker is redundant at best and confusing at worst.

## files empty after stripping: delete vs. keep as empty

**decision:** keep as empty files.

**justification:** deleting files after stripping could cause issues with imports or references in the public repo.  keeping them as empty files is safer.  in practice, developers should prefer placing fully-private files in `private/` directories rather than wrapping an entire file's contents in markers.

## config file cleanup (e.g., `Cargo.toml` references to `private/`): automatic vs. developer responsibility

**decision:** developer responsibility — wrap such references in `!repo-sync` markers.

**justification:** automatically detecting and cleaning config file references is fragile and format-specific.  the `!repo-sync` marker system is general-purpose and already solves this problem.  developers writing internal code are in the best position to know which config references need to be private.

## infinite loop prevention: commit trailer vs. bot author vs. branch name

**decision:** layered approach — commit trailer (`Repo-Sync-Origin: <repo>@<sha>`) verified against PR source branch (`repo-sync/*` prefix), with branch protection rules.

**alternatives considered:**
1. **commit message marker** (e.g., `[repo-sync]` prefix) — simple but spoofable
2. **bot author** — requires a separate service account
3. **commit trailer + PR branch verification + branch protection** (chosen) — layered defense

**justification:** no single mechanism is sufficient.  a trailer alone is spoofable (anyone can write it in a commit message).  a branch check alone doesn't help for direct pushes.  branch protection alone doesn't tell the workflow what to skip.  the layered approach requires all three: the trailer identifies the commit, the PR branch check confirms it came from a sync PR, and branch protection ensures only the sync workflow can create `repo-sync/*` branches.

## source repo merge strategy: squash only

**decision:** source repos must use squash merge for PRs to their default branch.

**justification:** squash merge ensures each PR merge produces exactly one commit on the default branch, which is the unit of sync.  merge commits and rebase merges would complicate the sync model — merge commits require special handling when cherry-picking (mainline parent selection), and rebase merges produce multiple commits per PR, each of which would generate a separate sync PR.  constraining to squash merge keeps the 1:1 mapping between source PRs and sync PRs.

## auto-merge of clean sync PRs

**decision:** clean (no-conflict) sync PRs are auto-merged without human review.

**justification:** the purpose of sync PRs is to keep repos in sync, not to gate changes.  the changes have already been reviewed and merged in the source repo.  requiring human review for every clean sync PR would create unnecessary bottlenecks and defeat the goal of automated syncing.
