"""Tests for repo_sync.workflows.sync."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from repo_sync.stack.branches import IdempotencyResult
from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.git_ops import CommandResult, GitOps
from repo_sync.stack.trailers import SyncOrigin
from repo_sync.workflows.sync import (
    build_public_to_private_description,
    check_commit_idempotency,
    determine_direction,
    determine_sync_reviewer,
    enumerate_unsynced_commits,
    find_existing_stack_top,
    get_commit_author,
    read_watermark_from_peer,
)


def _make_pr(
    number: int = 1,
    head: str = "repo-sync/private-to-public/abc1234",
    base: str = "main",
    title: str = "test",
    body: str = "",
    url: str = "https://github.com/test/repo/pull/1",
) -> PullRequest:
    """Helper to create a PullRequest for testing."""
    return PullRequest(
        number=number,
        head_branch=head,
        base_branch=base,
        title=title,
        body=body,
        url=url,
        state="OPEN",
    )


class TestDetermineDirection:
    """Tests for the sync direction helper."""

    def test_private_source(self) -> None:
        assert determine_direction(True) == "private-to-public"

    def test_public_source(self) -> None:
        assert determine_direction(False) == "public-to-private"


class TestEnumerateUnsyncedCommits:
    """Tests for enumerate_unsynced_commits."""

    def test_no_commits(self) -> None:
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)
        git.log_oneline.return_value = []

        result = enumerate_unsynced_commits(
            git, gh, "private-to-public", "main",
            SyncOrigin(repo="test/repo", sha="abc123"),
        )
        assert result == []

    @patch("repo_sync.workflows.sync.is_sync_originated")
    def test_filters_sync_originated(self, mock_is_sync: MagicMock) -> None:
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)
        git.log_oneline.return_value = ["sha1", "sha2", "sha3"]
        # sha2 is sync-originated; sha1 and sha3 are not.
        mock_is_sync.side_effect = [False, True, False]

        result = enumerate_unsynced_commits(
            git, gh, "private-to-public", "main",
            SyncOrigin(repo="test/repo", sha="abc123"),
        )
        assert result == ["sha1", "sha3"]

    @patch("repo_sync.workflows.sync.is_sync_originated")
    def test_all_sync_originated(self, mock_is_sync: MagicMock) -> None:
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)
        git.log_oneline.return_value = ["sha1", "sha2"]
        mock_is_sync.return_value = True

        result = enumerate_unsynced_commits(
            git, gh, "private-to-public", "main",
            SyncOrigin(repo="test/repo", sha="abc123"),
        )
        assert result == []

    @patch("repo_sync.workflows.sync.is_sync_originated")
    def test_preserves_order(self, mock_is_sync: MagicMock) -> None:
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)
        git.log_oneline.return_value = ["oldest", "middle", "newest"]
        mock_is_sync.return_value = False

        result = enumerate_unsynced_commits(
            git, gh, "private-to-public", "main",
            SyncOrigin(repo="test/repo", sha="abc123"),
        )
        assert result == ["oldest", "middle", "newest"]

    def test_uses_correct_range_spec(self) -> None:
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)
        git.log_oneline.return_value = []

        enumerate_unsynced_commits(
            git, gh, "private-to-public", "main",
            SyncOrigin(repo="test/repo", sha="watermark_sha"),
        )
        git.log_oneline.assert_called_once_with("watermark_sha..main")


class TestFindExistingStackTop:
    """Tests for find_existing_stack_top."""

    def test_no_open_prs(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.list_open_sync_prs.return_value = []

        result = find_existing_stack_top(gh, "private-to-public")
        assert result is None

    def test_returns_highest_pr_number(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.list_open_sync_prs.return_value = [
            _make_pr(number=10, head="repo-sync/private-to-public/aaa1111"),
            _make_pr(number=12, head="repo-sync/private-to-public/ccc3333"),
            _make_pr(number=11, head="repo-sync/private-to-public/bbb2222"),
        ]

        result = find_existing_stack_top(gh, "private-to-public")
        assert result == "repo-sync/private-to-public/ccc3333"

    def test_filters_by_direction(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.list_open_sync_prs.return_value = [
            _make_pr(number=10, head="repo-sync/private-to-public/aaa1111"),
            _make_pr(number=20, head="repo-sync/public-to-private/bbb2222"),
        ]

        result = find_existing_stack_top(gh, "private-to-public")
        assert result == "repo-sync/private-to-public/aaa1111"

    def test_no_matching_direction(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.list_open_sync_prs.return_value = [
            _make_pr(number=10, head="repo-sync/public-to-private/aaa1111"),
        ]

        result = find_existing_stack_top(gh, "private-to-public")
        assert result is None


class TestCheckCommitIdempotency:
    """Tests for check_commit_idempotency."""

    @patch("repo_sync.workflows.sync.check_idempotency")
    def test_already_exists(self, mock_check: MagicMock) -> None:
        mock_check.return_value = IdempotencyResult(already_exists=True)
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)

        result = check_commit_idempotency(git, gh, "repo-sync/p2p/abc")
        assert result is True

    @patch("repo_sync.workflows.sync.check_idempotency")
    def test_does_not_exist(self, mock_check: MagicMock) -> None:
        mock_check.return_value = IdempotencyResult(already_exists=False)
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)

        result = check_commit_idempotency(git, gh, "repo-sync/p2p/abc")
        assert result is False


class TestBuildPublicToPrivateDescription:
    """Tests for build_public_to_private_description."""

    def test_with_source_pr(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = _make_pr(
            title="Fix bug",
            body="This fixes a bug.",
            url="https://github.com/org/public/pull/42",
        )

        desc = build_public_to_private_description(
            source_gh=gh,
            source_repo="org/public",
            source_sha="abc123",
            commit_subject="unused",
            commit_body="unused",
        )
        assert desc.title == "Fix bug"
        assert "Synced from public:" in desc.body
        assert "https://github.com/org/public/pull/42" in desc.body
        assert "This fixes a bug." in desc.body

    def test_without_source_pr_falls_back_to_commit(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = None

        desc = build_public_to_private_description(
            source_gh=gh,
            source_repo="org/public",
            source_sha="abc123",
            commit_subject="Update README",
            commit_body="Added docs.",
        )
        assert desc.title == "Update README"
        assert "Synced from public:" in desc.body
        assert "https://github.com/org/public/commit/abc123" in desc.body
        assert "Added docs." in desc.body

    def test_repo_name_extracted_correctly(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = None

        desc = build_public_to_private_description(
            source_gh=gh,
            source_repo="warpdotdev/warp-public",
            source_sha="abc123",
            commit_subject="test",
            commit_body="",
        )
        assert "Synced from warp-public:" in desc.body


class TestReadWatermarkFromPeer:
    """Tests for read_watermark_from_peer."""

    def test_returns_none_when_tag_missing(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_tag_sha.return_value = None

        result = read_watermark_from_peer(gh, "private-to-public")
        assert result is None

    def test_returns_none_when_commit_message_missing(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_tag_sha.return_value = "abc123"
        gh.get_commit_message.return_value = None

        result = read_watermark_from_peer(gh, "private-to-public")
        assert result is None

    def test_returns_none_when_no_trailer_in_message(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_tag_sha.return_value = "abc123"
        gh.get_commit_message.return_value = "Just a regular commit message."

        result = read_watermark_from_peer(gh, "private-to-public")
        assert result is None

    def test_parses_trailer_correctly(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_tag_sha.return_value = "watermark_sha"
        gh.get_commit_message.return_value = (
            "repo-sync: sync from private\n\n"
            "Repo-Sync-Origin: warpdotdev/warp-internal@deadbeef1234"
        )

        result = read_watermark_from_peer(gh, "private-to-public")
        assert result is not None
        assert result.repo == "warpdotdev/warp-internal"
        assert result.sha == "deadbeef1234"

    def test_uses_correct_tag_name(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_tag_sha.return_value = None

        read_watermark_from_peer(gh, "public-to-private")
        gh.get_tag_sha.assert_called_once_with(
            "repo-sync/watermark/public-to-private"
        )


class TestGetCommitAuthor:
    """Tests for get_commit_author."""

    def test_returns_author_login(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_commit_author_login.return_value = "octocat"

        result = get_commit_author(gh, "abc123")
        assert result == "octocat"

    def test_returns_none_when_not_found(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_commit_author_login.return_value = None

        result = get_commit_author(gh, "abc123")
        assert result is None


class TestDetermineSyncReviewer:
    """Tests for determine_sync_reviewer."""

    def test_uses_pr_merger_when_available(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = _make_pr(number=42)
        gh.get_pr_merger.return_value = "merger-user"

        result = determine_sync_reviewer(gh, "abc123", "fallback-team")
        assert result == "merger-user"

    def test_uses_commit_author_for_direct_push(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = None
        gh.get_commit_author_login.return_value = "author-user"
        # determine_reviewer is called with commit_author="author-user".
        gh.get_pr_merger.return_value = None

        result = determine_sync_reviewer(gh, "abc123", "fallback-team")
        assert result == "author-user"

    def test_falls_back_to_team(self) -> None:
        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = None
        gh.get_commit_author_login.return_value = None
        gh.get_pr_merger.return_value = None

        result = determine_sync_reviewer(gh, "abc123", "fallback-team")
        assert result == "fallback-team"
