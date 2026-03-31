"""Stack branch operations: naming, creation, and idempotency guards.

Sync branches use a naming convention that includes the source commit SHA:
  - private-to-public: repo-sync/private-to-public/<short-sha>
  - public-to-private: repo-sync/public-to-private/<short-sha>
"""

from __future__ import annotations

from dataclasses import dataclass

from repo_sync.stack.constants import SYNC_BRANCH_PREFIX
from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.git_ops import GitOps


def sync_branch_name(direction: str, short_sha: str) -> str:
    """Generate the sync branch name for a commit.

    Direction should be 'private-to-public' or 'public-to-private'.
    """
    return f"{SYNC_BRANCH_PREFIX}{direction}/{short_sha}"


def is_sync_branch(branch_name: str) -> bool:
    """Check if a branch name follows the repo-sync/ naming convention."""
    return branch_name.startswith(SYNC_BRANCH_PREFIX)


@dataclass
class IdempotencyResult:
    """Result of an idempotency check for a sync branch."""

    # True if the sync branch or PR already exists.
    already_exists: bool
    # The existing PR, if one was found.
    existing_pr: PullRequest | None = None


def check_idempotency(
    git: GitOps, gh: GhOps, branch: str
) -> IdempotencyResult:
    """Check if a sync branch or PR already exists (idempotency guard).

    Prevents duplicates if the workflow crashes and restarts mid-run.
    Checks local branch existence, remote branch existence (via GitHub API),
    and PRs (any state) with that head branch.
    """
    branch_found = (
        git.branch_exists(branch) or gh.branch_exists_on_remote(branch)
    )
    # Use any_state=True so we find merged PRs too (covers the
    # crash-between-merge-and-watermark-update edge case per TECH-DESIGN.md).
    # PRs closed without merging are excluded by pr_exists so that a user can
    # close a sync PR and have the next workflow run recreate it.
    existing_pr = gh.pr_exists(branch, any_state=True)

    if branch_found or existing_pr is not None:
        return IdempotencyResult(
            already_exists=True, existing_pr=existing_pr
        )

    return IdempotencyResult(already_exists=False)


def create_stack_branch(
    git: GitOps,
    branch: str,
    base_ref: str,
    remote: str = "origin",
) -> None:
    """Create a new sync branch at the given base ref and push it.

    The base_ref is typically the top of the current stack (previous sync
    branch) or the default branch if this is the first PR in the stack.
    """
    git.create_branch(branch, base_ref)
    git.push(remote, branch)


def determine_stack_base(
    existing_stack: list[str], default_branch: str
) -> str:
    """Determine the base for a new sync branch.

    If there are existing sync branches in the stack, the base is the last one.
    Otherwise, the base is the default branch.
    """
    if existing_stack:
        return existing_stack[-1]
    return default_branch
