"""Shared conflict handling helpers for sync and restack workflows.

Provides the common logic that both the sync PR creation and restack
workflows use when a cherry-pick or rebase produces conflicts:

- Writing the modify/delete manifest and invoking the conflict-resolution
  agent (``run_agent_with_manifest``).
- Adding the ``repo-sync:conflict`` label (``add_conflict_label``).
- Determining and assigning a reviewer with the ``Repo-Sync-Assigned``
  trailer (``assign_conflict_reviewer``).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import (
    append_trailer,
    format_assigned_trailer,
)
from repo_sync.workflows.sync import determine_sync_reviewer

logger = logging.getLogger(__name__)


def run_agent_with_manifest(
    git: GitOps,
    md_conflicts: list[dict[str, str]],
    manifest_context: str,
    agent_context: str,
) -> bool:
    """Write the modify/delete manifest, invoke the agent, and clean up.

    The manifest (``.repo-sync-conflicts.json``) is a transient file
    consumed by the conflict-resolution agent.  It is never committed.

    Must be called *after* the conflict markers have been committed (so
    the agent sees committed markers, not a paused git operation).

    Returns True if the agent resolved the conflicts, False otherwise.
    """
    manifest_path = os.path.join(git.repo_dir, ".repo-sync-conflicts.json")

    if md_conflicts:
        logger.info(
            "Detected %d modify/delete conflict(s): %s",
            len(md_conflicts),
            [c["path"] for c in md_conflicts],
        )
        manifest = {
            "context": manifest_context,
            "modify_delete_conflicts": md_conflicts,
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")

    from repo_sync.workflows.agent import run_conflict_resolution_agent

    agent_succeeded = run_conflict_resolution_agent(
        repo_dir=git.repo_dir,
        context=agent_context,
    )

    # Clean up the manifest if the agent left it behind.
    if os.path.exists(manifest_path):
        os.remove(manifest_path)

    if agent_succeeded:
        logger.info("Conflict-resolution agent succeeded.")
    else:
        logger.warning(
            "Conflict-resolution agent did not resolve conflicts. "
            "PR will contain raw conflict markers."
        )

    return agent_succeeded


def add_conflict_label(gh: GhOps, pr_number: int) -> None:
    """Create (if needed) and apply the ``repo-sync:conflict`` label."""
    gh._run(
        ["label", "create", "repo-sync:conflict",
         "--color", "D93F0B",
         "--description", "Sync PR has merge conflicts",
         "--repo", gh.repo],
        check=False,
    )
    gh._run(
        ["pr", "edit", str(pr_number), "--repo", gh.repo,
         "--add-label", "repo-sync:conflict"],
    )


def assign_conflict_reviewer(
    gh: GhOps,
    pr_number: int,
    source_repo: str | None,
    source_sha: str | None,
    escalate_to: str,
) -> str:
    """Determine a reviewer, assign them, and append the Assigned trailer.

    Looks up the original commit author via ``determine_sync_reviewer``
    when *source_repo* and *source_sha* are provided; falls back to
    *escalate_to* otherwise.

    Returns the reviewer login or team slug.
    """
    if source_repo and source_sha:
        source_gh = GhOps(source_repo, token=os.environ.get("GH_TOKEN"))
        reviewer = determine_sync_reviewer(
            source_gh=source_gh,
            source_sha=source_sha,
            fallback_team=escalate_to,
        )
    else:
        reviewer = escalate_to

    # Request review.
    gh._run(
        ["pr", "edit", str(pr_number), "--repo", gh.repo,
         "--add-reviewer", reviewer],
        check=False,
    )

    # Append Repo-Sync-Assigned trailer to the PR body.
    assigned_trailer = format_assigned_trailer(
        reviewer, datetime.now(timezone.utc),
    )
    current_body = gh._run(
        ["pr", "view", str(pr_number), "--repo", gh.repo,
         "--json", "body", "--jq", ".body"],
        check=False,
    ) or ""
    updated_body = append_trailer(current_body, assigned_trailer)
    gh.update_pr_body(pr_number, updated_body)

    logger.info("Assigned reviewer %s on PR #%d.", reviewer, pr_number)
    return reviewer
