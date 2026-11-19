from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import parse_conflict
from repo_sync.workflows.restack_logic import run_restack
from tests.conftest import make_commit


PARENT = "repo-sync/public-to-private/aaaaaaa"
CHILD = "repo-sync/public-to-private/bbbbbbb"
BODY = "Sync child.\n\nRepo-Sync-Origin: org/public@bbbbbbb\n"


@pytest.fixture
def closed_child(tmp_git_repo_pair: tuple[GitOps, GitOps]):
    git, remote = tmp_git_repo_pair
    git.checkout_force_branch(PARENT)
    make_commit(git, "parent.txt", "parent\n", "Parent change")
    git.checkout_force_branch(CHILD)
    child_sha = make_commit(git, "child.txt", "child\n", BODY)
    git.push("origin", CHILD)
    git.checkout("main")
    git._run(["merge", "--squash", PARENT])
    git.commit("Squashed parent")
    git.push("origin", "main")

    pr = {
        "number": 2,
        "state": "closed",
        "merged_at": None,
        "head": {"ref": CHILD, "sha": child_sha, "repo": {"full_name": "org/repo"}},
        "base": {"ref": PARENT},
        "title": "Sync child",
        "body": BODY,
    }
    gh = GhOps("org/repo")
    gh.pr_exists = MagicMock(return_value=None)
    gh.create_pr = MagicMock(return_value=MagicMock(number=3))
    events = [
        {"event": "base_ref_deleted", "actor": {"id": 42}},
        {"event": "closed", "actor": {"id": 42}},
    ]

    def api(args):
        if args[0] == "api" and args[1] == "repos/org/repo/pulls":
            assert f"base={PARENT}" in args
            assert "state=closed" in args
            return json.dumps([pr])
        if args[0] == "api" and args[1] == "repos/org/repo/issues/2/events":
            return json.dumps(events)
        raise AssertionError(args)

    gh._run = MagicMock(side_effect=api)
    return git, remote, gh, pr, events


def restack(git: GitOps, gh: GhOps) -> None:
    with patch("repo_sync.workflows.restack_logic.find_next_pr", return_value=None):
        run_restack(
            git, gh, "normal", "public-to-private", "main", "org/repo",
            merged_head_branch=PARENT,
        )


def test_recovers_closed_child_after_squash_merge(closed_child) -> None:
    git, remote, gh, pr, events = closed_child

    def create(**kwargs):
        assert remote.rev_parse(f"{CHILD}~1") == remote.rev_parse("main")
        return MagicMock(number=3)

    gh.create_pr.side_effect = create
    restack(git, gh)

    gh.create_pr.assert_called_once_with(
        head=CHILD, base="main", title="Sync child", body=BODY,
    )
    assert remote._run(["show", f"{CHILD}:child.txt"]).stdout == "child"
    assert remote._run(["show", f"{CHILD}:parent.txt"]).stdout == "parent"
    gh.pr_exists.assert_called_with(CHILD, any_state=True)


@pytest.mark.parametrize(
    "reason",
    [
        "manual-close", "merged", "other-direction", "other-base",
        "fork", "replaced", "moved-head",
    ],
)
def test_does_not_recover_ineligible_prs(closed_child, reason) -> None:
    git, remote, gh, pr, events = closed_child
    original_sha = remote.rev_parse(CHILD)
    if reason == "manual-close":
        events.append({"event": "closed", "actor": {"id": 99}})
    elif reason == "merged":
        pr["merged_at"] = "2026-11-18T21:52:03Z"
    elif reason == "other-direction":
        pr["head"]["ref"] = "repo-sync/private-to-public/bbbbbbb"
    elif reason == "other-base":
        pr["base"]["ref"] = "another-branch"
    elif reason == "fork":
        pr["head"]["repo"]["full_name"] = "someone/fork"
    elif reason == "replaced":
        gh.pr_exists.return_value = MagicMock(state="MERGED")
    elif reason == "moved-head":
        pr["head"]["sha"] = "outdated"

    restack(git, gh)

    gh.create_pr.assert_not_called()
    assert remote.rev_parse(CHILD) == original_sha


def test_conflict_replacement_requires_human_approval_before_restack(closed_child) -> None:
    git, remote, gh, pr, events = closed_child
    make_commit(git, "child.txt", "concurrent edit\n", "Concurrent change")
    git.push("origin", "main")
    original_sha = remote.rev_parse(CHILD)

    def create(**kwargs):
        assert parse_conflict(kwargs["body"])
        assert remote.rev_parse(CHILD) == original_sha
        assert git._run(["status", "--porcelain"]).stdout == ""
        return MagicMock(number=3)

    gh.create_pr.side_effect = create
    with patch("repo_sync.workflows.restack_logic.rebase_pr") as rebase:
        restack(git, gh)

    gh.create_pr.assert_called_once()
    rebase.assert_called_once_with(
        git, gh, 3, CHILD, "main", "@oncall-client-primary",
    )


@pytest.mark.parametrize("signal", ["trailer", "label"])
def test_existing_conflict_still_has_a_reviewer_on_replacement(closed_child, signal) -> None:
    git, remote, gh, pr, events = closed_child
    if signal == "trailer":
        pr["body"] += "Repo-Sync-Conflict: cherry-pick\n"
    else:
        pr["labels"] = [{"name": "repo-sync:conflict"}]

    with (
        patch("repo_sync.workflows.restack_logic.add_conflict_label") as label,
        patch("repo_sync.workflows.restack_logic.assign_conflict_reviewer") as assign,
    ):
        restack(git, gh)

    assert parse_conflict(gh.create_pr.call_args.kwargs["body"])
    label.assert_called_once_with(gh, 3)
    assign.assert_called_once_with(
        gh, 3, source_repo="org/public", source_sha="bbbbbbb",
        escalate_to="@oncall-client-primary",
    )
