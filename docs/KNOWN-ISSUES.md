# known issues

bugs and quirks in the existing implementation, discovered during the shell-to-Python migration code review.  these are documented for awareness — they existed in the shell logic prior to migration.

## sync workflow shell logic (pre-migration)

### empty agent output overwrites fallback title/body

in the shell, if the PR description agent writes an empty `title.txt`, the fallback title is overwritten with an empty string:
```bash
[ -f "${AGENT_OUT}/title.txt" ] && PR_TITLE=$(cat "${AGENT_OUT}/title.txt")
```

the Python migration intentionally fixes this by checking truthiness (`if agent_title:`), so an empty file preserves the fallback.  this is a deliberate behavioral improvement, not a 1:1 migration.

### `IS_STACK_BOTTOM` is a dead variable

set at the top of the loop and updated during iteration, but never read by any logic in the "Create sync PRs" step.  it was likely used in an earlier version when auto-merge was enabled inline.

### `ESCALATE_TO` env var is set but unused

the "Create sync PRs" step sets `ESCALATE_TO` in the `env:` block but never references it.  escalation is handled by the separate escalation workflow.

### temp files leaked on conflict path

when a private-to-public cherry-pick hits a real conflict, the shell `exit 1`s without cleaning up `SNAPSHOT_DIR`, `PREV_SNAPSHOT_DIR`, `DIFF_REPO`, `PATCH_FILE`, or `AGENT_OUT`.  harmless on ephemeral CI runners but sloppy.  the Python migration's `finally` block improves this.
