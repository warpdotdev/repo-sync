"""Watermark tag management for tracking the last-synced source commit.

The watermark tag points to the target repo's merge commit for the most recently
merged sync PR.  The source commit SHA is recovered by reading the
Repo-Sync-Origin trailer from that commit's message.
"""

from __future__ import annotations

from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import SyncOrigin, parse_origin

# Tag naming convention.
_TAG_PREFIX = "repo-sync/watermark"


def watermark_tag_name(direction: str) -> str:
    """Return the watermark tag name for a sync direction.

    Direction should be 'private-to-public' or 'public-to-private'.
    """
    return f"{_TAG_PREFIX}/{direction}"


def read_watermark(git: GitOps, direction: str) -> SyncOrigin | None:
    """Read the watermark tag and extract the source SHA from the trailer.

    Returns the parsed SyncOrigin (repo + sha), or None if the watermark tag
    does not exist.  Raises ValueError if the tag exists but the commit message
    does not contain a valid Repo-Sync-Origin trailer.
    """
    tag_name = watermark_tag_name(direction)
    tag_sha = git.tag_target(tag_name)
    if tag_sha is None:
        return None

    # Read the commit message of the tagged commit.
    message = git.commit_message(tag_sha)
    origin = parse_origin(message)
    if origin is None:
        raise ValueError(
            f"Watermark tag '{tag_name}' points to commit {tag_sha[:12]}, "
            f"but its message has no Repo-Sync-Origin trailer."
        )
    return origin


def update_watermark(
    git: GitOps, direction: str, merge_commit_sha: str, remote: str = "origin"
) -> None:
    """Update the watermark tag to point to the given merge commit.

    The merge commit must contain a Repo-Sync-Origin trailer in its message.
    The tag is force-updated locally and pushed to the remote.
    """
    tag_name = watermark_tag_name(direction)

    # Verify the commit has the required trailer before updating.
    message = git.commit_message(merge_commit_sha)
    origin = parse_origin(message)
    if origin is None:
        raise ValueError(
            f"Cannot update watermark: commit {merge_commit_sha[:12]} "
            f"does not contain a Repo-Sync-Origin trailer."
        )

    git.tag(tag_name, merge_commit_sha, force=True)
    git.push(remote, f"refs/tags/{tag_name}", force=True)
