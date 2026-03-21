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
