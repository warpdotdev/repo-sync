"""Restack logic for sync PR stacks after a squash merge.

When a sync PR at the bottom of the stack is squash-merged, the next PR needs
to be rebased onto the updated default branch.  A naive rebase would fail
because the squash merge creates a new commit object, causing duplicate-change
conflicts.  The fix is to use:

    git rebase --onto main <merged-pr-branch-tip> <next-pr-branch>

This drops the commits from the merged PR and replays only the next PR's
commits onto main.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.prs import set_auto_merge


class RestackResult(Enum):
    """Outcome of a restack operation."""

    SUCCESS = "success"
    CONFLICT = "conflict"


@dataclass
class RestackOutcome:
    """Full result of a restack operation."""

    result: RestackResult
    # The list of conflicting files, if any.
    conflicting_files: list[str] | None = None


def restack_pr(
    git: GitOps,
    gh: GhOps,
    next_pr_branch: str,
    merged_pr_branch_tip: str,
    default_branch: str,
    next_pr_number: int,
    remote: str = "origin",
) -> RestackOutcome:
    """Rebase the next PR in the stack onto the updated default branch.

    Steps:
    1. Rebase using --onto to avoid duplicate-change conflicts from squash merge.
    2. Force-push the rebased branch.
    3. Update the PR's base branch to the default branch.
    4. Enable auto-merge if the rebase succeeds cleanly (no conflicts).

    Returns a RestackOutcome indicating success or conflict.
    """
    # Rebase: drop the merged PR's commits and replay only the next PR's.
    # Note: rebase --onto checks out the target branch automatically.
    result = git.rebase_onto(
        new_base=default_branch,
        old_base=merged_pr_branch_tip,
        branch=next_pr_branch,
    )

    if not result.success:
        # Rebase failed -- likely conflicts.
        conflicting = _get_conflicting_files(git)
        git.rebase_abort()
        return RestackOutcome(
            result=RestackResult.CONFLICT,
            conflicting_files=conflicting,
        )

    # Rebase succeeded -- force-push and update the PR.
    git.push(remote, next_pr_branch, force=True)
    gh.update_pr_base(next_pr_number, default_branch)

    # Enable auto-merge since this is now the bottom of the stack.
    set_auto_merge(
        gh,
        next_pr_number,
        base_branch=default_branch,
        default_branch=default_branch,
        conflict_resolved=False,
    )

    return RestackOutcome(result=RestackResult.SUCCESS)


def restack_after_conflict_resolution(
    git: GitOps,
    gh: GhOps,
    pr_branch: str,
    pr_number: int,
    default_branch: str,
    remote: str = "origin",
) -> None:
    """Update PR metadata after an agent resolves conflicts during restack.

    Force-pushes the resolved branch, updates the base, but does NOT enable
    auto-merge because conflict-resolved PRs require human sign-off.
    """
    git.push(remote, pr_branch, force=True)
    gh.update_pr_base(pr_number, default_branch)


def _get_conflicting_files(git: GitOps) -> list[str]:
    """Get the list of files with merge conflicts."""
    return git.conflicting_files()
