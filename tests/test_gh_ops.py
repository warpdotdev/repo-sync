"""Tests for GhOps methods that have non-trivial logic.

Covers list_open_sync_prs pagination, filtering, field mapping, and
branch_exists_on_remote URL encoding.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

from repo_sync.stack.gh_ops import GhOps, PullRequest


def _make_rest_pr(
    number: int, head_ref: str, auto_merge: bool = False
) -> dict:
    """Create a mock REST API PR response dict."""
    return {
        "number": number,
        "head": {"ref": head_ref},
        "base": {"ref": "main"},
        "title": f"PR #{number}",
        "body": f"Body for #{number}",
        "html_url": f"https://github.com/org/repo/pull/{number}",
        "state": "open",
        "auto_merge": {"merge_method": "squash"} if auto_merge else None,
    }


class TestListOpenSyncPrs:
    """Tests for list_open_sync_prs."""

    def test_filters_to_sync_branches_only(self) -> None:
        """Only PRs with repo-sync/ head branches are returned."""
        gh = GhOps(repo="org/repo")
        prs = [
            _make_rest_pr(1, "repo-sync/private-to-public/aaa"),
            _make_rest_pr(2, "feature/my-feature"),
            _make_rest_pr(3, "repo-sync/public-to-private/bbb"),
        ]
        gh._run = MagicMock(return_value=json.dumps(prs))

        result = gh.list_open_sync_prs()

        assert len(result) == 2
        assert result[0].number == 1
        assert result[1].number == 3

    def test_empty_result(self) -> None:
        """Returns empty list when no open PRs exist."""
        gh = GhOps(repo="org/repo")
        gh._run = MagicMock(return_value="[]")

        result = gh.list_open_sync_prs()
        assert result == []

    def test_pagination(self) -> None:
        """Paginates through multiple pages until a partial page signals end."""
        gh = GhOps(repo="org/repo")

        # Page 1: 100 PRs (full page, triggers next page).
        page1 = [_make_rest_pr(i, f"repo-sync/p2p/{i}") for i in range(100)]
        # Page 2: 50 PRs (partial page, signals end).
        page2 = [_make_rest_pr(100 + i, f"repo-sync/p2p/{100 + i}") for i in range(50)]

        gh._run = MagicMock(side_effect=[json.dumps(page1), json.dumps(page2)])

        result = gh.list_open_sync_prs()

        assert len(result) == 150
        assert gh._run.call_count == 2

    def test_auto_merge_field_mapping(self) -> None:
        """auto_merge REST field maps to auto_merge_enabled correctly."""
        gh = GhOps(repo="org/repo")
        prs = [
            _make_rest_pr(1, "repo-sync/p2p/aaa", auto_merge=True),
            _make_rest_pr(2, "repo-sync/p2p/bbb", auto_merge=False),
        ]
        gh._run = MagicMock(return_value=json.dumps(prs))

        result = gh.list_open_sync_prs()

        assert result[0].auto_merge_enabled is True
        assert result[1].auto_merge_enabled is False

    def test_state_normalized_to_uppercase(self) -> None:
        """REST API lowercase state is normalized to uppercase."""
        gh = GhOps(repo="org/repo")
        prs = [_make_rest_pr(1, "repo-sync/p2p/aaa")]
        gh._run = MagicMock(return_value=json.dumps(prs))

        result = gh.list_open_sync_prs()
        assert result[0].state == "OPEN"

    def test_null_body_handled(self) -> None:
        """A PR with null body does not crash."""
        gh = GhOps(repo="org/repo")
        pr = _make_rest_pr(1, "repo-sync/p2p/aaa")
        pr["body"] = None
        gh._run = MagicMock(return_value=json.dumps([pr]))

        result = gh.list_open_sync_prs()
        assert result[0].body == ""
