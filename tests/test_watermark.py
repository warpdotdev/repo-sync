"""Tests for watermark tag management and recovery.

Covers VALIDATION.md watermark recovery cases:
- Source SHA is correctly recovered from the watermark tag's commit trailer.
- Recovery works after the sync branch is auto-deleted.
- Missing watermark returns None.
- Watermark without trailer raises ValueError.
"""

from __future__ import annotations

import os

import pytest

from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import format_origin_trailer
from repo_sync.stack.watermark import (
    read_watermark,
    update_watermark,
    watermark_tag_name,
)
from tests.conftest import make_commit


class TestWatermarkTagName:
    """Tests for watermark tag naming."""

    def test_private_to_public(self) -> None:
        """Correct tag name for private-to-public direction."""
        assert (
            watermark_tag_name("private-to-public")
            == "repo-sync/watermark/private-to-public"
        )

    def test_public_to_private(self) -> None:
        """Correct tag name for public-to-private direction."""
        assert (
            watermark_tag_name("public-to-private")
            == "repo-sync/watermark/public-to-private"
        )


class TestReadWatermark:
    """Tests for reading the watermark tag."""

    def test_missing_watermark_returns_none(self, tmp_git_repo: GitOps) -> None:
        """If no watermark tag exists, returns None."""
        result = read_watermark(tmp_git_repo, "private-to-public")
        assert result is None

    def test_read_watermark_from_tagged_commit(
        self, tmp_git_repo: GitOps
    ) -> None:
        """Source SHA is recovered from the tagged commit's trailer."""
        # Create a commit with a Repo-Sync-Origin trailer (simulating a merged sync PR).
        trailer = format_origin_trailer("warpdotdev/warp-internal", "abc123def456")
        sha = make_commit(
            tmp_git_repo,
            "synced.txt",
            "synced content",
            f"repo-sync: sync\n\n{trailer}",
        )

        # Tag it as the watermark.
        tmp_git_repo.tag("repo-sync/watermark/private-to-public", sha)

        result = read_watermark(tmp_git_repo, "private-to-public")
        assert result is not None
        assert result.repo == "warpdotdev/warp-internal"
        assert result.sha == "abc123def456"

    def test_watermark_without_trailer_raises(
        self, tmp_git_repo: GitOps
    ) -> None:
        """A watermark tag on a commit without a trailer raises ValueError."""
        sha = make_commit(
            tmp_git_repo, "file.txt", "content", "a commit without trailers"
        )
        tmp_git_repo.tag("repo-sync/watermark/private-to-public", sha)

        with pytest.raises(ValueError, match="no Repo-Sync-Origin trailer"):
            read_watermark(tmp_git_repo, "private-to-public")

    def test_watermark_recovery_after_branch_deletion(
        self, tmp_git_repo: GitOps
    ) -> None:
        """After a sync branch is deleted, watermark still recovers the source SHA.

        This simulates the scenario where GitHub auto-deletes the sync branch
        after merge, but the watermark tag still points to the merge commit.
        """
        # Create a "merge commit" with the origin trailer.
        trailer = format_origin_trailer("warpdotdev/warp-internal", "deadbeef")
        merge_sha = make_commit(
            tmp_git_repo,
            "merged.txt",
            "merged content",
            f"Merge pull request #42\n\n{trailer}",
        )

        # Create a sync branch, then delete it (simulating auto-delete).
        tmp_git_repo.create_branch("repo-sync/private-to-public/deadbee", merge_sha)
        tmp_git_repo.checkout("main")
        tmp_git_repo._run(["branch", "-D", "repo-sync/private-to-public/deadbee"])

        # Set watermark to the merge commit.
        tmp_git_repo.tag("repo-sync/watermark/private-to-public", merge_sha)

        # Recovery should still work.
        result = read_watermark(tmp_git_repo, "private-to-public")
        assert result is not None
        assert result.sha == "deadbeef"


class TestUpdateWatermark:
    """Tests for updating the watermark tag."""

    def test_update_watermark(self, tmp_git_repo_pair: tuple[GitOps, GitOps]) -> None:
        """Watermark tag is created/updated and pushed to the remote."""
        source_git, _ = tmp_git_repo_pair
        trailer = format_origin_trailer("warpdotdev/warp-internal", "newsha123")
        sha = make_commit(
            source_git,
            "synced.txt",
            "content",
            f"merge commit\n\n{trailer}",
        )

        update_watermark(source_git, "private-to-public", sha)

        # Verify the tag exists locally.
        tag_sha = source_git.tag_target("repo-sync/watermark/private-to-public")
        assert tag_sha == sha

    def test_update_watermark_without_trailer_raises(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """Cannot update watermark to a commit without a Repo-Sync-Origin trailer."""
        source_git, _ = tmp_git_repo_pair
        sha = make_commit(
            source_git, "file.txt", "content", "no trailer here"
        )

        with pytest.raises(ValueError, match="does not contain a Repo-Sync-Origin"):
            update_watermark(source_git, "private-to-public", sha)

    def test_update_watermark_force_updates_existing(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """Updating the watermark when a tag already exists force-moves it."""
        source_git, _ = tmp_git_repo_pair

        # First watermark.
        trailer1 = format_origin_trailer("org/repo", "first_sha")
        sha1 = make_commit(
            source_git, "f1.txt", "c1", f"merge 1\n\n{trailer1}"
        )
        update_watermark(source_git, "private-to-public", sha1)

        # Second watermark (force-updates).
        trailer2 = format_origin_trailer("org/repo", "second_sha")
        sha2 = make_commit(
            source_git, "f2.txt", "c2", f"merge 2\n\n{trailer2}"
        )
        update_watermark(source_git, "private-to-public", sha2)

        # Should now point to the second commit.
        result = read_watermark(source_git, "private-to-public")
        assert result is not None
        assert result.sha == "second_sha"
