"""Tests for sync PR creation and retries."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.workflows.create_sync_prs import (
    TransientSyncError,
    _create_pr,
    create_sync_prs,
)


PARENT = "repo-sync/public-to-private/parent1"
CHILD_SHA = "abcdef123456"
CHILD = f"repo-sync/public-to-private/{CHILD_SHA[:7]}"


def test_merged_parent_prevents_pr_creation() -> None:
    gh = MagicMock(spec=GhOps)
    gh.pr_exists.return_value.state = "MERGED"

    with pytest.raises(TransientSyncError, match="merged"):
        _create_pr(gh, CHILD, PARENT, "title", "body")

    gh.pr_exists.assert_called_once_with(PARENT, any_state=True)
    gh.create_pr_simple.assert_not_called()


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_unmerged_parent_allows_pr_creation(state: str) -> None:
    gh = GhOps("org/peer")
    gh._run = MagicMock(return_value=json.dumps([{
        "number": 1,
        "headRefName": PARENT,
        "baseRefName": "main",
        "title": "parent",
        "url": "https://github.com/org/peer/pull/1",
        "state": state,
    }]))
    gh.create_pr_simple = MagicMock(return_value="child-url")

    assert _create_pr(gh, CHILD, PARENT, "title", "body") == "child-url"
    gh.create_pr_simple.assert_called_once_with(
        head=CHILD, base=PARENT, title="title", body="body",
    )


def test_default_branch_does_not_need_parent_lookup() -> None:
    gh = MagicMock(spec=GhOps)

    _create_pr(gh, CHILD, "main", "title", "body")

    gh.pr_exists.assert_not_called()
    gh.create_pr_simple.assert_called_once()


def test_parent_lookup_failure_prevents_pr_creation() -> None:
    gh = GhOps("org/peer")
    gh.create_pr_simple = MagicMock()
    with patch(
        "repo_sync.stack.gh_ops.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="API unavailable",
        ),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            _create_pr(gh, CHILD, PARENT, "title", "body")

    gh.create_pr_simple.assert_not_called()


@pytest.mark.parametrize(
    ("stack_top", "refreshed_top"),
    [(PARENT, None), (None, "repo-sync/public-to-private/other12")],
)
def test_retry_rebuilds_child_on_current_stack(
    stack_top: str | None, refreshed_top: str | None,
) -> None:
    source_git = MagicMock(spec=GitOps)
    source_git.repo_dir = "/source"
    source_git.diff_name_only.return_value = []
    peer_git = MagicMock(spec=GitOps)
    peer_git.cherry_pick.return_value.success = True
    peer_git._run.return_value.stdout = "commit"
    peer_git.commit_message.return_value = "commit"
    gh = MagicMock(spec=GhOps)
    gh.get_pr_state_for_branch.side_effect = (
        [None, None] if stack_top else ["OPEN", None, "MERGED", None]
    )
    gh.pr_exists.side_effect = [
        MagicMock(state="MERGED"), MagicMock(state="OPEN"),
    ]
    gh.create_pr_simple.return_value = "https://github.com/org/peer/pull/2"

    with (
        patch("repo_sync.workflows.create_sync_prs.time.sleep"),
        patch(
            "repo_sync.workflows.create_sync_prs.find_existing_stack_top",
            return_value=refreshed_top,
        ) as find_top,
        patch(
            "repo_sync.workflows.create_sync_prs.build_public_to_private_description",
        ) as describe,
    ):
        describe.return_value.title = "title"
        describe.return_value.body = "body"
        create_sync_prs(
            source_git=source_git,
            peer_git=peer_git,
            peer_gh=gh,
            unsynced_commits=(
                [CHILD_SHA] if stack_top else ["parent123456", CHILD_SHA]
            ),
            source_repo="org/source",
            peer_repo="org/peer",
            direction="public-to-private",
            branch_prefix="repo-sync/public-to-private",
            source_is_private=False,
            peer_default_branch="main",
            stack_top=stack_top,
        )

    find_top.assert_called_once_with(gh, "public-to-private")
    peer_git.checkout_force_branch.assert_has_calls([
        call("_sync_work", f"origin/{PARENT}"),
        call(CHILD),
        call("_sync_work", f"origin/{refreshed_top or 'main'}"),
        call(CHILD),
    ])
    assert peer_git.cherry_pick.call_args_list == [
        call(CHILD_SHA, allow_empty=True, x=True),
        call(CHILD_SHA, allow_empty=True, x=True),
    ]
    assert call("origin") in peer_git.fetch.call_args_list
    gh.create_pr_simple.assert_called_once_with(
        head=CHILD,
        base=refreshed_top or "main",
        title="title",
        body=f"body\n\nRepo-Sync-Origin: org/source@{CHILD_SHA}\n",
    )
