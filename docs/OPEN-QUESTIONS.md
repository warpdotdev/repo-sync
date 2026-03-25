## resolved

* when a sync PR has a merge conflict, who should be assigned to resolve it?
  * **decision:** in both directions, assign to the person who clicked merge on the source PR.  in the public->private direction, this works because only people with private repo access have merge privileges on the public repo.
  * escalation: if the assignee doesn't respond within some time, reassign to `@oncall-client-primary`.  implementation details TBD in tech design phase.
* what merge strategy should be used for sync PRs?
  * **decision:** squash and merge.  each source commit produces a single commit in the target repo.  requires `--onto` rebase when restacking (see TECH-DESIGN.md).
* how should sync PR descriptions be written?
  * **decision:**
    * **private->public:** an agent generates the PR description based on the public diff only (no access to private PR description, to avoid leaking internal information).
    * **public->private:** keep the same PR title and description from the public repo.  add a header: "Synced from \<public repo name\>: \<URL to public repo PR\>".  the squash commit title should match the original public PR title.

## deferred to tech design (now addressed)

all items below have been addressed in TECH-DESIGN.md:

* ~~which specific local oz agent flow writes private->public PR descriptions?~~ -- separate skill (`pr-description`), distinct from conflict resolution
* ~~what is the configuration surface for the conflict resolution timeout?~~ -- `escalate_after` workflow input + `Repo-Sync-Assigned` trailer + cron workflow
* ~~what are the exact constraints on marker placement?~~ -- no constraints; simple substring match, developers trusted to use sensibly
* ~~which file types and comment syntaxes are supported for markers?~~ -- all text files scanned; binary detection via null-byte heuristic
* ~~how should "completely empty after stripping" be defined?~~ -- not further defined; empty files are kept as-is per PRD
* ~~should the CI validation action also validate comment syntax?~~ -- no; markers are language-agnostic substring matches
* ~~what are the exact interfaces for the reusable workflows?~~ -- three workflows documented with full input/secret specs
* ~~what are the exact GitHub permissions, tokens, and repo settings?~~ -- GitHub App with `contents:write`, `pull_requests:write`, `metadata:read`

## open

### watermark update robustness

the watermark tag is updated by the restack workflow, which triggers on sync PR merge events.  if the restack workflow is delayed or fails before updating the watermark, the next sync workflow run will reprocess already-synced commits, potentially creating duplicate sync PRs.

the idempotency guard prevents duplicate PRs from being created (it checks for existing PRs with matching head branches), so the impact is wasted work rather than data corruption.  however, this is still a robustness concern:

* github actions does not guarantee timely execution — workflows can be queued for minutes during high load
* if the restack workflow fails mid-run (after the watermark update but before rebasing), the watermark is correct but the next PR is stuck.  if it fails before the watermark update, the watermark is stale
* the sync workflow's concurrency group (`cancel-in-progress: false`) means a stale-watermark run will queue and eventually execute, but it will reprocess commits that were already synced

potential mitigations to explore:
* **advance the watermark in the sync workflow** — update the watermark as each sync PR is created (not just when it merges).  this decouples watermark tracking from the restack workflow's execution.  downside: the watermark would point to a PR that hasn't merged yet, which changes the recovery semantics
* **use the idempotency guard as the primary deduplication mechanism** — accept that the watermark may be stale and rely on the per-commit idempotency checks to skip already-processed commits.  this is already the behavior, but it's not intentional and the extra API calls per commit add latency
* **dual watermark** — maintain a "last-created" watermark (updated by the sync workflow) and a "last-merged" watermark (updated by the restack workflow).  the sync workflow uses the "last-created" watermark to avoid reprocessing, while the "last-merged" watermark is used for conflict detection and recovery
