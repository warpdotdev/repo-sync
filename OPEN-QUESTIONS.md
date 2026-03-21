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

## deferred to tech design

these are real questions, but they fit better in the technical design than in the PRD:

* which specific local oz agent flow writes private->public PR descriptions? should it be the same agent flow used for conflict resolution, or a separate one?
* what is the configuration surface for the conflict resolution timeout, and what should the default be?
* what are the exact constraints on marker placement? for example: must markers appear on their own line, or can they appear at the end of a code line or inside a larger comment?
* which file types and comment syntaxes are supported for markers, and how is file language determined?
* how should "completely empty after stripping" be defined? does whitespace-only count as empty?
* should the CI validation action also validate that markers use the correct comment syntax for the file type?
* what are the exact interfaces for the reusable workflows, stripping tool, and stack management tool?
* what are the exact GitHub permissions, tokens, and repo settings required for consuming repos?

## open
