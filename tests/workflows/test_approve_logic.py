"""Tests for repo_sync.workflows.approve_logic."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from repo_sync.workflows import approve_logic
from repo_sync.workflows.approve_logic import approve_and_auto_merge


def _make_called_process_error(stderr: str) -> subprocess.CalledProcessError:
    """Build a CalledProcessError matching what GhOps._run raises."""
    return subprocess.CalledProcessError(
        returncode=1,
        cmd=["gh", "pr", "merge"],
        output="",
        stderr=stderr,
    )


class TestApproveAndAutoMerge:
    """Tests for the approve+auto-merge step, including retry behavior."""

    def test_succeeds_on_first_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gh = MagicMock()
        gh.repo = "owner/repo"
        gh._run = MagicMock(return_value="")

        sleep_calls: list[float] = []
        monkeypatch.setattr(approve_logic.time, "sleep", sleep_calls.append)

        approve_and_auto_merge(gh, pr_number=42)

        # Approval and a single merge attempt; no retries.
        assert gh._run.call_count == 2
        assert sleep_calls == []

    def test_retries_on_base_branch_modified_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gh = MagicMock()
        gh.repo = "owner/repo"

        retryable_err = _make_called_process_error(
            "GraphQL: Base branch was modified. Review and try the merge "
            "again. (mergePullRequest)"
        )
        # First call is the approval (check=False, so it returns "").  The
        # next call is the first auto-merge attempt (raises), and the third
        # call is the retry (succeeds).
        gh._run = MagicMock(side_effect=["", retryable_err, ""])

        sleep_calls: list[float] = []
        monkeypatch.setattr(approve_logic.time, "sleep", sleep_calls.append)

        approve_and_auto_merge(
            gh, pr_number=42, max_attempts=3, retry_delay=10
        )

        # Approval + 2 merge attempts; one sleep of 10s between them.
        assert gh._run.call_count == 3
        assert sleep_calls == [10]

    def test_gives_up_after_max_attempts_on_retryable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gh = MagicMock()
        gh.repo = "owner/repo"

        retryable_err = _make_called_process_error(
            "GraphQL: Base branch was modified. (mergePullRequest)"
        )
        gh._run = MagicMock(
            side_effect=["", retryable_err, retryable_err, retryable_err]
        )

        sleep_calls: list[float] = []
        monkeypatch.setattr(approve_logic.time, "sleep", sleep_calls.append)

        # Should NOT raise — match original check=False behavior of logging
        # and returning so the queue can be unblocked by a later event.
        approve_and_auto_merge(
            gh, pr_number=42, max_attempts=3, retry_delay=10
        )

        # Approval + 3 merge attempts; 2 sleeps (no sleep after the final
        # attempt).
        assert gh._run.call_count == 4
        assert sleep_calls == [10, 10]

    def test_does_not_retry_on_non_retryable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gh = MagicMock()
        gh.repo = "owner/repo"

        non_retryable_err = _make_called_process_error(
            "GraphQL: something else went wrong"
        )
        gh._run = MagicMock(side_effect=["", non_retryable_err])

        sleep_calls: list[float] = []
        monkeypatch.setattr(approve_logic.time, "sleep", sleep_calls.append)

        approve_and_auto_merge(
            gh, pr_number=42, max_attempts=3, retry_delay=10
        )

        # Approval + a single failed merge attempt; no retries, no sleeps.
        assert gh._run.call_count == 2
        assert sleep_calls == []
