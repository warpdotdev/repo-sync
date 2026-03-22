# agent failure handling

this document describes how the sync workflow handles failures from oz agent invocations.

## PR description agent

the PR description agent is used only for private-to-public sync PRs, where the description must be generated from the public diff without access to private information.

### failure modes

the agent may fail due to:
- oz CLI errors (e.g., API timeout, authentication failure).
- the agent producing output that does not match the expected `TITLE:` / `DESCRIPTION:` format.
- the agent producing empty output.
- the Docker container failing to start or crashing.

### fallback behavior

if the PR description agent fails for any reason, the workflow creates the sync PR with a **generic fallback description**:

- **title:** `repo-sync: sync from private (source: <short-sha>)`
- **description:** `Sync changes from private repository.\n\nSource commit: <short-sha>`

the `Repo-Sync-Origin` trailer is still appended by deterministic code, regardless of whether the agent succeeded or failed.

**the sync is never blocked by a PR description agent failure.**  the PR is created with the fallback description and proceeds through the normal auto-merge flow.

### workflow pseudocode

```
title, description = run_pr_description_agent(clean_snapshot, diff)

if agent_failed or output_parse_failed or title is empty or description is empty:
    short_sha = source_commit[:7]
    title = f"repo-sync: sync from private (source: {short_sha})"
    description = f"Sync changes from private repository.\n\nSource commit: {short_sha}"

description += f"\n\nRepo-Sync-Origin: {private_repo}@{source_commit}"
create_pr(title, description)
```

## conflict resolution agent

the conflict resolution agent is used in both sync directions when a merge or rebase produces conflicts.

### failure modes

the agent may fail due to:
- oz CLI errors.
- the agent being unable to resolve the conflicts.
- the agent producing a resolution that does not compile.
- the agent producing a resolution that still contains conflict markers.
- the agent timing out.

### fallback behavior

if the conflict resolution agent fails for any reason:

1. the workflow aborts the in-progress git operation (`git rebase --abort`, `git cherry-pick --abort`, or `git merge --abort` as appropriate) to return the branch to a clean state.  the remote branch remains unchanged from before the agent was invoked.
2. the PR is created (or left in its current state) **without** an agent-proposed resolution.
3. a reviewer is requested from the person who merged the source PR (or the commit author for direct pushes, or `@oncall-client-primary` as a last resort).
4. a `Repo-Sync-Assigned` trailer is appended to the PR description to start the escalation clock.
5. auto-merge is **not** enabled on the PR.

the human reviewer is responsible for resolving the conflicts manually.

### workflow pseudocode

```
success = run_conflict_resolution_agent(repo_path, conflicting_files)

if not success:
    # Agent failed -- no resolution commit was pushed.
    log("conflict resolution agent failed, assigning to human")

# Whether the agent succeeded or failed, always request a reviewer.
# Agent-resolved conflicts still require human sign-off.
reviewer = determine_reviewer(source_commit)
request_review(pr, reviewer)
append_trailer(pr, f"Repo-Sync-Assigned: {reviewer}@{now_iso8601()}")

# Note: auto-merge is NOT enabled regardless of outcome.
# If the agent succeeded, the human reviews the proposed resolution.
# If the agent failed, the human must resolve conflicts manually.
```
