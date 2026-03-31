"""Restack logic for sync PRs.

Migrated from the shell logic in restack.yml.  Handles:
- Identifying the PR context (post-merge vs needs-restack/stuck-recovery).
- Watermark updates (post-merge mode only).
- Finding the next PR in the stack (with auto-retarget fallback).
- Trailer-based rebase using Repo-Sync-Origin to find the old base.
- Push guard (only push if the rebase changed the branch).
- Label removal and PR base update.
"""

from __future__ import annotations

import json
import logging
import subprocess

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import parse_origin
from repo_sync.workflows.restack_workflow import determine_direction

logger = logging.getLogger(__name__)


class RestackError(Exception):
    """Raised when the restack workflow encounters a fatal error."""
    pass


def update_watermark(
    gh: GhOps,
    watermark_repo: str,
    direction: str,
    merge_sha: str,
) -> None:
    """Update the watermark tag to point to the given merge commit.

    Validates the merge commit has a Repo-Sync-Origin trailer before updating.
    """
    watermark_tag = f"repo-sync/watermark/{direction}"

    # Validate the trailer is present.
    commit_msg = gh.get_commit_message(merge_sha) or ""
    if "Repo-Sync-Origin:" not in commit_msg:
        raise RestackError(
            f"Merge commit {merge_sha} has no Repo-Sync-Origin trailer. "
            "Cannot update watermark. Check that squash merge is configured "
            "to preserve the PR description in the commit message."
        )

    logger.info(
        "Updating watermark tag '%s' in %s to %s.",
        watermark_tag, watermark_repo, merge_sha,
    )

    # Try to update existing tag; create if it doesn't exist.
    try:
        gh._run([
            "api", f"repos/{watermark_repo}/git/ref/tags/{watermark_tag}",
            "--jq", ".ref",
        ])
        # Tag exists — update it.
        gh._run([
            "api", "-X", "PATCH",
            f"repos/{watermark_repo}/git/refs/tags/{watermark_tag}",
            "-f", f"sha={merge_sha}",
            "-F", "force=true",
        ])
    except subprocess.CalledProcessError:
        # Tag doesn't exist — create it.
        gh._run([
            "api", "-X", "POST",
            f"repos/{watermark_repo}/git/refs",
            "-f", f"ref=refs/tags/{watermark_tag}",
            "-f", f"sha={merge_sha}",
        ])

    logger.info("Watermark updated.")


def find_next_pr(
    gh: GhOps,
    merged_branch: str | None,
    direction: str,
    default_branch: str,
) -> tuple[int, str] | None:
    """Find the next PR in the stack to restack.

    Returns (pr_number, head_branch) or None if no next PR exists.
    Tries the direct base-branch search first, then the auto-retarget fallback.
    """
    # Primary search: PR whose base is the just-merged branch.
    if merged_branch:
        output = gh._run([
            "pr", "list", "--repo", gh.repo,
            "--state", "open",
            "--base", merged_branch,
            "--json", "number,headRefName",
            "--jq", ".[0].number // empty",
        ], check=False)
        if output:
            pr_number = int(output)
            head = gh._run([
                "pr", "view", str(pr_number), "--repo", gh.repo,
                "--json", "headRefName", "--jq", ".headRefName",
            ])
            return pr_number, head

    # Fallback: auto-retargeted PRs (>1 commit, no conflict label).
    branch_prefix = f"repo-sync/{direction}/"
    output = gh._run([
        "pr", "list", "--repo", gh.repo,
        "--state", "open",
        "--base", default_branch,
        "--json", "number,headRefName,commits,labels",
        "--jq",
        f'[.[] | select(.headRefName | startswith("{branch_prefix}")) '
        f'| select(.commits | length > 1) '
        f'| select((.labels | map(.name) | index("repo-sync:conflict")) | not)] '
        f'| sort_by(.number) | .[0].number // empty',
    ], check=False)
    if output:
        pr_number = int(output)
        head = gh._run([
            "pr", "view", str(pr_number), "--repo", gh.repo,
            "--json", "headRefName", "--jq", ".headRefName",
        ])
        return pr_number, head

    return None


def find_sync_commit_old_base(git: GitOps) -> str | None:
    """Find the sync commit and return its parent SHA as the old base.

    Searches backwards from HEAD for the most recent commit with a
    Repo-Sync-Origin trailer.  Returns the parent SHA, or None if no
    sync commit is found.
    """
    for sha in git.log_shas("HEAD"):
        msg = git.commit_message(sha)
        if "Repo-Sync-Origin:" in msg:
            return git.rev_parse(f"{sha}~1")
    return None


def rebase_pr(
    git: GitOps,
    gh: GhOps,
    pr_number: int,
    head_branch: str,
    default_branch: str,
) -> bool:
    """Rebase a PR onto the default branch using trailer-based old base.

    Returns True if the rebase succeeded and a push was made.
    Returns False if the rebase conflicted or was a no-op.
    """
    git.fetch("origin")
    git.checkout(head_branch)
    pre_rebase_sha = git.rev_parse("HEAD")

    # Find the sync commit and determine the old base.
    old_base = find_sync_commit_old_base(git)
    if old_base is None:
        logger.error(
            "No commit with Repo-Sync-Origin trailer found on branch %s.",
            head_branch,
        )
        return False

    # Attempt the rebase.
    result = git.rebase_onto(
        new_base=f"origin/{default_branch}",
        old_base=old_base,
        branch=head_branch,
    )

    if not result.success:
        logger.warning("Rebase has conflicts.")
        git.rebase_abort()
        # Update the PR base to main so the approve workflow can handle it.
        gh.update_pr_base(pr_number, default_branch)
        return False

    # Push guard: only push if the rebase actually changed something.
    post_rebase_sha = git.rev_parse("HEAD")
    if post_rebase_sha == pre_rebase_sha:
        logger.warning(
            "Rebase was a no-op (SHA unchanged). Not pushing. "
            "Leaving repo-sync:needs-restack label for investigation."
        )
        # Still update the base so the PR targets main.
        gh.update_pr_base(pr_number, default_branch)
        return False

    # Push the rebased branch (force-with-lease to match shell behavior).
    git.push("origin", head_branch, force_with_lease=True)

    # Update PR base and remove the needs-restack label.
    gh.update_pr_base(pr_number, default_branch)
    gh.remove_label(pr_number, "repo-sync:needs-restack")

    return True


def run_restack(
    git: GitOps,
    gh: GhOps,
    mode: str,
    direction: str,
    default_branch: str,
    watermark_repo: str,
    # Post-merge mode fields.
    merge_sha: str | None = None,
    merged_head_branch: str | None = None,
    # Stuck-recovery / needs-restack mode fields.
    stuck_pr_number: int | None = None,
    stuck_head_branch: str | None = None,
) -> None:
    """Run the full restack workflow logic.

    Modes:
    - 'normal': post-merge.  Updates watermark, finds next PR, rebases.
    - 'stuck_recovery': needs-restack or workflow_dispatch.  Rebases the
      specified PR without updating the watermark.
    """
    # Step 1: Update watermark (post-merge only).
    if mode == "normal" and merge_sha:
        update_watermark(gh, watermark_repo, direction, merge_sha)

    # Step 2: Find the next PR to restack.
    if mode == "stuck_recovery":
        if stuck_pr_number is None or stuck_head_branch is None:
            raise RestackError("stuck_recovery mode requires pr_number and head_branch.")
        next_pr = stuck_pr_number
        next_head = stuck_head_branch
    else:
        result = find_next_pr(gh, merged_head_branch, direction, default_branch)
        if result is None:
            logger.info("No next PR in stack. Stack is fully merged.")
            return
        next_pr, next_head = result

    logger.info("Next PR in stack: #%d (branch: %s).", next_pr, next_head)

    # Step 3: Rebase the PR.
    rebase_pr(git, gh, next_pr, next_head, default_branch)


def run_restack_from_event(
    git: GitOps,
    gh: GhOps,
    event_path: str,
    event_name: str,
    event_action: str,
    repository: str,
    public_repo: str,
    private_repo: str,
    default_branch: str,
) -> None:
    """Run the restack workflow from a GitHub Actions event context.

    Derives mode, direction, PR context, and watermark repo from the event
    payload, then delegates to run_restack().
    """
    # Read the event payload.
    with open(event_path) as f:
        event = json.load(f)

    # Derive sync context.
    source_is_private = repository == private_repo

    # Determine mode and PR context.
    if event_name == "workflow_dispatch" or event_action == "labeled":
        # Stuck-recovery or needs-restack mode.
        if event_name == "workflow_dispatch":
            pr_number = int(event.get("inputs", {}).get("pr_number", 0))
        else:
            pr_number = event.get("pull_request", {}).get("number", 0)

        if not pr_number:
            raise RestackError("No PR number provided.")

        # Look up the PR's head branch.
        head_branch = gh._run([
            "pr", "view", str(pr_number), "--repo", gh.repo,
            "--json", "headRefName", "--jq", ".headRefName",
        ])

        # Detect direction from the head branch.
        direction = determine_direction(
            merged_head_branch=head_branch,
            source_is_private=source_is_private,
        )

        run_restack(
            git=git, gh=gh,
            mode="stuck_recovery",
            direction=direction,
            default_branch=default_branch,
            watermark_repo=repository,
            stuck_pr_number=pr_number,
            stuck_head_branch=head_branch,
        )
    else:
        # Normal post-merge mode.
        pr = event.get("pull_request", {})
        pr_number = pr.get("number", 0)
        head_branch = pr.get("head", {}).get("ref", "")

        # Detect direction from the merged PR's head branch.
        direction = determine_direction(
            merged_head_branch=head_branch,
            source_is_private=source_is_private,
        )

        # Get merge commit SHA via API (not in the event payload for
        # workflow_call triggers).
        merge_sha = gh._run([
            "pr", "view", str(pr_number), "--repo", gh.repo,
            "--json", "mergeCommit", "--jq", ".mergeCommit.oid",
        ])
        merged_head_branch_name = gh._run([
            "pr", "view", str(pr_number), "--repo", gh.repo,
            "--json", "headRefName", "--jq", ".headRefName",
        ])

        run_restack(
            git=git, gh=gh,
            mode="normal",
            direction=direction,
            default_branch=default_branch,
            watermark_repo=repository,
            merge_sha=merge_sha,
            merged_head_branch=merged_head_branch_name,
        )
