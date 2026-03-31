"""Tests for repo_sync.workflows.descriptions."""

from __future__ import annotations

from repo_sync.workflows.descriptions import (
    private_to_public_default_title,
    private_to_public_fallback,
    public_to_private_from_commit,
    public_to_private_from_pr,
)


class TestPrivateToPublicFallback:
    """Tests for the private-to-public fallback description."""

    def test_includes_short_sha_in_title(self) -> None:
        desc = private_to_public_fallback("abc1234")
        assert "abc1234" in desc.title

    def test_includes_short_sha_in_body(self) -> None:
        desc = private_to_public_fallback("abc1234")
        assert "abc1234" in desc.body

    def test_title_starts_with_repo_sync(self) -> None:
        desc = private_to_public_fallback("abc1234")
        assert desc.title.startswith("repo-sync:")

    def test_body_contains_source_marker(self) -> None:
        desc = private_to_public_fallback("abc1234")
        assert "source:" in desc.body


class TestPrivateToPublicDefaultTitle:
    """Tests for the default title generator."""

    def test_contains_short_sha(self) -> None:
        title = private_to_public_default_title("def5678")
        assert "def5678" in title

    def test_starts_with_repo_sync(self) -> None:
        title = private_to_public_default_title("def5678")
        assert title.startswith("repo-sync:")


class TestPublicToPrivateFromPR:
    """Tests for constructing a description from a source PR."""

    def test_title_matches_source_pr(self) -> None:
        desc = public_to_private_from_pr(
            source_repo_name="warp-public",
            source_pr_title="Fix typo in README",
            source_pr_body="Fixed a small typo.",
            source_pr_url="https://github.com/warpdotdev/warp-public/pull/42",
        )
        assert desc.title == "Fix typo in README"

    def test_body_has_synced_from_header(self) -> None:
        desc = public_to_private_from_pr(
            source_repo_name="warp-public",
            source_pr_title="Fix typo",
            source_pr_body="Fixed a typo.",
            source_pr_url="https://github.com/warpdotdev/warp-public/pull/42",
        )
        assert desc.body.startswith("Synced from warp-public:")

    def test_body_includes_source_url(self) -> None:
        url = "https://github.com/warpdotdev/warp-public/pull/42"
        desc = public_to_private_from_pr(
            source_repo_name="warp-public",
            source_pr_title="Fix typo",
            source_pr_body="Body text.",
            source_pr_url=url,
        )
        assert url in desc.body

    def test_body_includes_source_body(self) -> None:
        desc = public_to_private_from_pr(
            source_repo_name="warp-public",
            source_pr_title="Fix typo",
            source_pr_body="The original PR body.",
            source_pr_url="https://github.com/warpdotdev/warp-public/pull/42",
        )
        assert "The original PR body." in desc.body


class TestPublicToPrivateFromCommit:
    """Tests for constructing a description from a direct push commit."""

    def test_title_matches_commit_subject(self) -> None:
        desc = public_to_private_from_commit(
            source_repo_name="warp-public",
            commit_subject="Update config",
            commit_body="",
            commit_url="https://github.com/warpdotdev/warp-public/commit/abc123",
        )
        assert desc.title == "Update config"

    def test_body_has_synced_from_header(self) -> None:
        desc = public_to_private_from_commit(
            source_repo_name="warp-public",
            commit_subject="Update config",
            commit_body="",
            commit_url="https://github.com/warpdotdev/warp-public/commit/abc123",
        )
        assert desc.body.startswith("Synced from warp-public:")

    def test_body_includes_commit_url(self) -> None:
        url = "https://github.com/warpdotdev/warp-public/commit/abc123"
        desc = public_to_private_from_commit(
            source_repo_name="warp-public",
            commit_subject="Update",
            commit_body="",
            commit_url=url,
        )
        assert url in desc.body

    def test_body_includes_commit_body_when_present(self) -> None:
        desc = public_to_private_from_commit(
            source_repo_name="warp-public",
            commit_subject="Update",
            commit_body="Detailed description of changes.",
            commit_url="https://github.com/warpdotdev/warp-public/commit/abc123",
        )
        assert "Detailed description of changes." in desc.body

    def test_body_omits_extra_newlines_when_no_body(self) -> None:
        desc = public_to_private_from_commit(
            source_repo_name="warp-public",
            commit_subject="Update",
            commit_body="",
            commit_url="https://github.com/warpdotdev/warp-public/commit/abc123",
        )
        # Should not have a trailing double newline without a body.
        assert not desc.body.endswith("\n\n")
