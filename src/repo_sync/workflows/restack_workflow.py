"""Restack workflow orchestration.

Thin layer on top of the stack library's restack module.  Adds the
direction-detection logic (from the merged PR's branch name) and coordinates
watermark updates with restacking.

The actual rebase/push/PR-update operations are in repo_sync.stack.restack.
"""

from __future__ import annotations

import logging

from repo_sync.stack.branches import is_sync_branch

logger = logging.getLogger(__name__)


def detect_direction_from_branch(head_branch: str) -> str | None:
    """Detect the sync direction from a merged PR's head branch name.

    Returns 'private-to-public', 'public-to-private', or None if the branch
    does not match the sync naming convention.
    """
    if head_branch.startswith("repo-sync/private-to-public/"):
        return "private-to-public"
    if head_branch.startswith("repo-sync/public-to-private/"):
        return "public-to-private"
    return None


def determine_direction(
    merged_head_branch: str | None,
    source_is_private: bool,
) -> str:
    """Determine the sync direction for a restack operation.

    Prefers detection from the merged PR's branch name.  Falls back to the
    source_is_private input (where source_is_private=True means this repo is
    the public target receiving private-to-public sync PRs).
    """
    if merged_head_branch:
        detected = detect_direction_from_branch(merged_head_branch)
        if detected:
            return detected

    # Fallback: source_is_private=True means the SOURCE is private, so the
    # TARGET (this repo) receives private-to-public sync PRs.
    return "private-to-public" if source_is_private else "public-to-private"


def watermark_repo_is_self() -> bool:
    """The watermark always lives in the target repo (this repo), not the peer.

    This function exists as documentation and for test assertions.
    """
    return True
