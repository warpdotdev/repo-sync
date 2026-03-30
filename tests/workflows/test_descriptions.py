"""Tests for repo_sync.workflows.descriptions."""

from __future__ import annotations

from repo_sync.workflows.descriptions import (
    parse_agent_output,
    private_to_public_default_title,
    private_to_public_fallback,
    public_to_private_from_commit,
    public_to_private_from_pr,
)


class TestParseAgentOutput:
    """Tests for parsing the PR description agent's structured output."""

    def test_parses_valid_output(self) -> None:
        raw = "TITLE: Add retry logic to sync client\n\nDESCRIPTION:\nAdds exponential backoff when the sync API returns 429."
        desc = parse_agent_output(raw)
        assert desc is not None
        assert desc.title == "Add retry logic to sync client"
        assert desc.body == "Adds exponential backoff when the sync API returns 429."

    def test_parses_multiline_description(self) -> None:
        raw = (
            "TITLE: Refactor config loader\n\n"
            "DESCRIPTION:\n"
            "Splits the monolithic config loader into separate modules.\n"
            "Each module handles a single config section."
        )
        desc = parse_agent_output(raw)
        assert desc is not None
        assert "Splits the monolithic" in desc.body
        assert "single config section." in desc.body

    def test_returns_none_for_missing_title(self) -> None:
        raw = "DESCRIPTION:\nSome body text."
        assert parse_agent_output(raw) is None

    def test_returns_none_for_missing_description(self) -> None:
        raw = "TITLE: Some title"
        assert parse_agent_output(raw) is None

    def test_returns_none_for_empty_string(self) -> None:
        assert parse_agent_output("") is None

    def test_returns_none_for_empty_title(self) -> None:
        raw = "TITLE:   \n\nDESCRIPTION:\nSome body."
        assert parse_agent_output(raw) is None

    def test_returns_none_for_empty_body(self) -> None:
        raw = "TITLE: Some title\n\nDESCRIPTION:\n"
        assert parse_agent_output(raw) is None

    def test_handles_extra_text_before_title(self) -> None:
        raw = (
            "Let me analyze the diff.\n\n"
            "TITLE: Fix null pointer in parser\n\n"
            "DESCRIPTION:\nHandles the case where input is None."
        )
        desc = parse_agent_output(raw)
        assert desc is not None
        assert desc.title == "Fix null pointer in parser"

    def test_strips_whitespace_from_title_and_body(self) -> None:
        raw = "TITLE:   Add tests   \n\nDESCRIPTION:\n  Adds unit tests.  \n"
        desc = parse_agent_output(raw)
        assert desc is not None
        assert desc.title == "Add tests"
        assert desc.body == "Adds unit tests."

    def test_single_newline_between_title_and_description(self) -> None:
        raw = "TITLE: Fix bug\nDESCRIPTION:\nFixed it."
        desc = parse_agent_output(raw)
        assert desc is not None
        assert desc.title == "Fix bug"
        assert desc.body == "Fixed it."

    def test_description_on_same_line(self) -> None:
        raw = "TITLE: Fix bug\n\nDESCRIPTION: Fixed the null check."
        desc = parse_agent_output(raw)
        assert desc is not None
        assert desc.body == "Fixed the null check."

    def test_description_immediately_after_colon_with_newline(self) -> None:
        raw = "TITLE: Fix bug\n\nDESCRIPTION:Fixed it."
        desc = parse_agent_output(raw)
        assert desc is not None
        assert desc.body == "Fixed it."


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
