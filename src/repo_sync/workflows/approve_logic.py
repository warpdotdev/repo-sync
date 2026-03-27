"""Approve workflow logic for sync PRs.

Migrated from the shell logic in approve.yml.  Handles:
- Skip checks (existing approval, Repo-Sync-Assigned trailer).
- Commit count check (skip if != 1).
- Mergeability polling with retries.
- Clean path: approve + enable auto-merge (API only).
- Conflict path: rebase for conflict markers, invoke agent, assign reviewer.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import parse_origin
from repo_sync.workflows.sync import determine_sync_reviewer

logger = logging.getLogger(__name__)

# Git identity for conflict resolution commits.
# TODO(vorporeal): Consider changing this to the Oz agent identity so it's
# clear that the agent (not the approver bot) produced the resolution commit.
_CONFLICT_RESOLUTION_GIT_NAME = "repo-sync-approver-bot"
_CONFLICT_RESOLUTION_GIT_EMAIL = "repo-sync-approver-bot@users.noreply.github.com"


class ApproveSkipped(Exception):
    """Raised when the approve workflow should skip this PR."""
    pass


def check_already_handled(gh: GhOps, repo: str, pr_number: int) -> bool:
    """Check if the PR has already been handled.

    Returns True if the PR should be skipped (has an approval or
    Repo-Sync-Assigned trailer).
    """
    # Check for existing approval.
    output = gh._run([
        "api", f"repos/{repo}/pulls/{pr_number}/reviews",
        "--jq", '[.[] | select(.state == "APPROVED")] | length',
    ], check=False)
    approvals = int(output) if output else 0
    if approvals > 0:
        logger.info("PR #%d already has an approval. Skipping.", pr_number)
        return True

    # Check for Repo-Sync-Assigned trailer.
    body = gh._run([
        "pr", "view", str(pr_number), "--repo", gh.repo,
        "--json", "body", "--jq", ".body",
    ], check=False)
    if body and "Repo-Sync-Assigned:" in body:
        logger.info(
            "PR #%d already has a Repo-Sync-Assigned trailer. Skipping.",
            pr_number,
        )
        return True

    return False


def check_commit_count(gh: GhOps, pr_number: int) -> int:
    """Return the number of commits on the PR."""
    output = gh._run([
        "pr", "view", str(pr_number), "--repo", gh.repo,
        "--json", "commits", "--jq", ".commits | length",
    ])
    return int(output)


def check_mergeability(
    gh: GhOps,
    pr_number: int,
    max_retries: int = 5,
    delay: int = 5,
) -> str:
    """Poll PR mergeability.

    Returns 'clean', 'conflicting', or 'unknown'.
    """
    for i in range(1, max_retries + 1):
        output = gh._run([
            "pr", "view", str(pr_number), "--repo", gh.repo,
            "--json", "mergeable", "--jq", ".mergeable",
        ])
        logger.info("Attempt %d: mergeable=%s", i, output)

        if output == "MERGEABLE":
            return "clean"
        elif output == "CONFLICTING":
            return "conflicting"

        # UNKNOWN — wait and retry.
        if i < max_retries:
            logger.info("Mergeability unknown, retrying in %ds...", delay)
            time.sleep(delay)

    logger.warning(
        "Mergeability still unknown after %d retries. Skipping.", max_retries,
    )
    return "unknown"


def approve_and_auto_merge(gh: GhOps, pr_number: int) -> None:
    """Approve a clean PR and enable auto-merge."""
    gh._run([
        "pr", "review", str(pr_number), "--repo", gh.repo,
        "--approve", "--body", "Clean sync — no conflicts.",
    ], check=False)

    gh._run([
        "pr", "merge", str(pr_number), "--repo", gh.repo,
        "--auto", "--squash",
    ], check=False)


def handle_conflict(
    git: GitOps,
    gh: GhOps,
    pr_number: int,
    pr_branch: str,
    default_branch: str,
    repo: str,
    escalate_to: str,
) -> None:
    """Handle a conflicting PR: rebase, invoke agent, assign reviewer.

    The approve workflow does NOT approve conflict-resolved PRs.
    """
    # Add conflict label.
    gh._run([
        "label", "create", "repo-sync:conflict",
        "--color", "D93F0B",
        "--description", "Sync PR has merge conflicts",
        "--repo", gh.repo,
    ], check=False)
    gh._run([
        "pr", "edit", str(pr_number), "--repo", gh.repo,
        "--add-label", "repo-sync:conflict",
    ])

    # Configure git identity for any commits produced during conflict resolution.
    git._run(["config", "user.name", _CONFLICT_RESOLUTION_GIT_NAME])
    git._run(["config", "user.email", _CONFLICT_RESOLUTION_GIT_EMAIL])

    git.fetch("origin")
    git.checkout(pr_branch)

    # Attempt rebase to produce conflict markers.
    old_base = git.rev_parse("HEAD~1")
    result = git.rebase_onto(
        new_base=f"origin/{default_branch}",
        old_base=old_base,
        branch=pr_branch,
    )

    if result.success:
        # Rebase succeeded unexpectedly (transient GitHub state).
        logger.info("Rebase succeeded unexpectedly. Pushing.")
        git.push("origin", pr_branch, force_with_lease=True)
        return

    # Rebase failed — invoke the conflict resolution agent.
    conflicting_files = git.conflicting_files()
    logger.info("Conflicting files: %s", conflicting_files)

    agent_succeeded = False
    try:
        agent_result = subprocess.run(
            [
                "oz", "agent", "run",
                "--skill", "warpdotdev/repo-sync:conflict-resolution",
                "--context",
                f"Conflicting files on PR #{pr_number}: {' '.join(conflicting_files)}",
            ],
            capture_output=True,
            timeout=600,
        )
        if agent_result.returncode == 0:
            logger.info("Agent resolved conflicts.")
            agent_succeeded = True
        else:
            logger.warning("Agent failed to resolve conflicts.")
            git.rebase_abort()
    except Exception:
        logger.warning("Agent failed to resolve conflicts.")
        git.rebase_abort()

    # Push if the agent succeeded.
    if agent_succeeded:
        git.push("origin", pr_branch, force_with_lease=True)

    # Assign a reviewer (always, regardless of agent success).
    _assign_reviewer(gh, pr_number, repo, escalate_to)


def _assign_reviewer(
    gh: GhOps,
    pr_number: int,
    repo: str,
    escalate_to: str,
) -> None:
    """Assign a reviewer and add the Repo-Sync-Assigned trailer."""
    # Parse the source info from the PR body trailer.
    body = gh._run([
        "pr", "view", str(pr_number), "--repo", gh.repo,
        "--json", "body", "--jq", ".body",
    ], check=False)
    origin = parse_origin(body) if body else None

    if origin:
        source_repo = origin.repo
        source_sha = origin.sha
        source_gh = GhOps(source_repo, token=os.environ.get("GH_TOKEN"))
        reviewer = determine_sync_reviewer(
            source_gh=source_gh,
            source_sha=source_sha,
            fallback_team=escalate_to,
        )
    else:
        reviewer = escalate_to

    gh._run([
        "pr", "edit", str(pr_number), "--repo", gh.repo,
        "--add-reviewer", reviewer,
    ], check=False)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current_body = gh._run([
        "pr", "view", str(pr_number), "--repo", gh.repo,
        "--json", "body", "--jq", ".body",
    ], check=False) or ""
    updated_body = f"{current_body}\n\nRepo-Sync-Assigned: {reviewer}@{timestamp}"
    gh.update_pr_body(pr_number, updated_body)

    logger.info("Requested review from %s on PR #%d.", reviewer, pr_number)


def run_approve(
    gh: GhOps,
    repo: str,
    pr_number: int,
    pr_branch: str,
    default_branch: str,
    escalate_to: str,
    git: GitOps | None = None,
) -> None:
    """Run the full approve workflow logic.

    The `git` parameter is only needed for the conflict path.  For the
    clean path, all operations are API-only.
    """
    # Step 1: Already handled?
    if check_already_handled(gh, repo, pr_number):
        return

    # Step 2: Commit count check.
    commit_count = check_commit_count(gh, pr_number)
    logger.info("PR #%d has %d commit(s).", pr_number, commit_count)
    if commit_count != 1:
        logger.info(
            "PR #%d has %d commits (expected 1). Needs rebase. Skipping.",
            pr_number, commit_count,
        )
        return

    # Step 3: Mergeability check.
    status = check_mergeability(gh, pr_number)

    if status == "clean":
        # Step 4a: Approve and enable auto-merge.
        approve_and_auto_merge(gh, pr_number)
    elif status == "conflicting":
        # Step 4b: Conflict resolution.
        if git is None:
            logger.error(
                "Conflict path requires a GitOps instance (full checkout)."
            )
            return
        handle_conflict(
            git=git,
            gh=gh,
            pr_number=pr_number,
            pr_branch=pr_branch,
            default_branch=default_branch,
            repo=repo,
            escalate_to=escalate_to,
        )
    else:
        # Unknown — skip, next event will re-trigger.
        logger.info("Mergeability unknown. Skipping.")
