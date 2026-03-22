"""PR management: creation with correct base, auto-merge control, descriptions.

Sync PRs are created with the base set to the previous sync branch (or the
default branch for the first PR in the stack).  Auto-merge is only enabled
on the bottom of the stack (base = default branch).
"""

from __future__ import annotations

from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.trailers import append_trailer, format_origin_trailer


def create_sync_pr(
    gh: GhOps,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    source_repo: str,
    source_sha: str,
    default_branch: str,
) -> PullRequest:
    """Create a sync PR with the Repo-Sync-Origin trailer appended.

    Enables auto-merge if this PR is at the bottom of the stack (base equals
    the default branch).
    """
    # Append the origin trailer to the body.
    trailer = format_origin_trailer(source_repo, source_sha)
    full_body = append_trailer(body, trailer)

    pr = gh.create_pr(
        head=head_branch,
        base=base_branch,
        title=title,
        body=full_body,
    )

    # Enable auto-merge only on the bottom of the stack.
    if base_branch == default_branch:
        gh.enable_auto_merge(pr.number)

    return pr


def should_enable_auto_merge(base_branch: str, default_branch: str) -> bool:
    """Return True if auto-merge should be enabled for a PR with this base.

    Auto-merge is only enabled when the PR's base is the default branch,
    meaning it is at the bottom of the stack.
    """
    return base_branch == default_branch


def set_auto_merge(
    gh: GhOps,
    pr_number: int,
    base_branch: str,
    default_branch: str,
    conflict_resolved: bool = False,
) -> None:
    """Enable or disable auto-merge based on the PR's position in the stack.

    Auto-merge is never enabled on conflict-resolved PRs -- those require
    human sign-off.
    """
    if conflict_resolved:
        return
    if should_enable_auto_merge(base_branch, default_branch):
        gh.enable_auto_merge(pr_number)
