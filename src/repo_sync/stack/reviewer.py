"""Reviewer assignment logic for sync PRs.

Assignment priority:
1. The person who clicked merge on the source PR.
2. The commit author (for direct pushes with no source PR).
3. The fallback team (defaults to @oncall-client-primary).

After assignment, a Repo-Sync-Assigned trailer is appended to the PR
description to start the escalation clock.
"""

from __future__ import annotations

from datetime import datetime, timezone

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.trailers import (
    append_trailer,
    format_assigned_trailer,
)

# Default fallback team for reviewer assignment.
DEFAULT_FALLBACK_TEAM = "oncall-client-primary"


def determine_reviewer(
    gh: GhOps,
    source_pr_number: int | None,
    commit_author: str | None,
    fallback_team: str = DEFAULT_FALLBACK_TEAM,
) -> str:
    """Determine who to request as reviewer for a sync PR.

    Tries the merger of the source PR first, then the commit author, then the
    fallback team.
    """
    # Try the merger of the source PR.
    if source_pr_number is not None:
        merger = gh.get_pr_merger(source_pr_number)
        if merger:
            return merger

    # Fall back to the commit author.
    if commit_author:
        return commit_author

    # Last resort: the fallback team.
    return fallback_team


def assign_reviewer(
    gh: GhOps,
    pr_number: int,
    reviewer: str,
    current_body: str,
    now: datetime | None = None,
) -> str:
    """Request a review and append a Repo-Sync-Assigned trailer.

    Returns the updated PR body with the trailer appended.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Request the review.
    gh.request_reviewer(pr_number, reviewer)

    # Append the assignment trailer to track when the reviewer was assigned.
    trailer = format_assigned_trailer(reviewer, now)
    updated_body = append_trailer(current_body, trailer)
    gh.update_pr_body(pr_number, updated_body)

    return updated_body
