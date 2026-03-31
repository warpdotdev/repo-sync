"""Infinite loop prevention for sync commits.

When a sync commit merges into the target repo's default branch, it would
normally trigger a reverse sync.  To prevent this, a three-layer check is used:

1. Commit trailer: sync commits include a Repo-Sync-Origin trailer.
2. PR branch verification: the commit was merged from a repo-sync/ branch.
3. Branch protection: only the sync workflow can create repo-sync/ branches.

A commit is only recognized as sync-originated (and skipped) if BOTH the
trailer is present AND the PR branch check passes.
"""

from __future__ import annotations

from repo_sync.stack.branches import is_sync_branch
from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import parse_origin


def is_sync_originated(
    git: GitOps,
    gh: GhOps,
    commit_sha: str,
) -> bool:
    """Check if a commit is sync-originated and should be skipped.

    Both conditions must be true:
    1. The commit message contains a Repo-Sync-Origin trailer.
    2. The commit was merged from a PR whose head branch starts with repo-sync/.

    Returns True if the commit should be skipped (do not reverse-sync it).
    """
    # Check 1: does the commit have a Repo-Sync-Origin trailer?
    message = git.commit_message(commit_sha)
    origin = parse_origin(message)
    if origin is None:
        return False

    # Check 2: was the commit merged from a repo-sync/ branch?
    pr = gh.get_pr_for_commit(commit_sha)
    if pr is None:
        # No PR found -- this could be a direct push with a spoofed trailer.
        return False

    if not is_sync_branch(pr.head_branch):
        # PR exists but its branch doesn't match repo-sync/ prefix.
        return False

    return True
