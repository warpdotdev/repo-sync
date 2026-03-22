"""Tests for repo_sync.workflows.escalation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from repo_sync.stack.gh_ops import PullRequest
from repo_sync.workflows.escalation import (
    EscalationAction,
    check_ci_failure,
    check_stuck_stack,
    check_timeout_escalation,
    evaluate_pr,
    parse_duration,
)


def _make_pr(
    number: int = 1,
    head: str = "repo-sync/private-to-public/abc1234",
    base: str = "main",
    body: str = "",
    auto_merge: bool = False,
) -> PullRequest:
    """Helper to create a PullRequest for testing."""
    return PullRequest(
        number=number,
        head_branch=head,
        base_branch=base,
        title="test",
        body=body,
        url=f"https://github.com/test/repo/pull/{number}",
        state="OPEN",
        auto_merge_enabled=auto_merge,
    )


class TestParseDuration:
    """Tests for the duration parser."""

    def test_parse_minutes(self) -> None:
        assert parse_duration("5m") == timedelta(minutes=5)

    def test_parse_hours(self) -> None:
        assert parse_duration("2h") == timedelta(hours=2)

    def test_parse_hours_and_minutes(self) -> None:
        assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)

    def test_invalid_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="Could not parse"):
            parse_duration("invalid")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("")


class TestCheckTimeoutEscalation:
    """Tests for the timeout escalation check."""

    def test_no_assignment_trailer(self) -> None:
        pr = _make_pr(body="No trailers here.")
        assert check_timeout_escalation(pr, timedelta(hours=1)) is False

    def test_assignment_within_timeout(self) -> None:
        now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        # Assigned 30 minutes ago; timeout is 1 hour.
        assigned_time = now - timedelta(minutes=30)
        body = f"Repo-Sync-Assigned: someone@{assigned_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        pr = _make_pr(body=body)
        assert check_timeout_escalation(pr, timedelta(hours=1), now=now) is False

    def test_assignment_exceeds_timeout(self) -> None:
        now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        # Assigned 2 hours ago; timeout is 1 hour.
        assigned_time = now - timedelta(hours=2)
        body = f"Repo-Sync-Assigned: someone@{assigned_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        pr = _make_pr(body=body)
        assert check_timeout_escalation(pr, timedelta(hours=1), now=now) is True

    def test_uses_last_trailer_occurrence(self) -> None:
        now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        # First trailer is old (exceeds timeout), second is recent.
        old_time = now - timedelta(hours=5)
        recent_time = now - timedelta(minutes=10)
        body = (
            f"Repo-Sync-Assigned: old@{old_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"Repo-Sync-Assigned: new@{recent_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        pr = _make_pr(body=body)
        # Should use the last (recent) trailer, so not timed out.
        assert check_timeout_escalation(pr, timedelta(hours=1), now=now) is False


class TestCheckCIFailure:
    """Tests for the CI failure detection check."""

    def test_no_auto_merge(self) -> None:
        pr = _make_pr(auto_merge=False)
        assert check_ci_failure(pr, ci_has_failed=True) is False

    def test_auto_merge_ci_passing(self) -> None:
        pr = _make_pr(auto_merge=True)
        assert check_ci_failure(pr, ci_has_failed=False) is False

    def test_auto_merge_ci_failed(self) -> None:
        pr = _make_pr(auto_merge=True)
        assert check_ci_failure(pr, ci_has_failed=True) is True

    def test_already_assigned_skips(self) -> None:
        # Reviewer already assigned -- don't reset the escalation clock.
        body = "Repo-Sync-Assigned: someone@2026-03-22T10:00:00Z"
        pr = _make_pr(auto_merge=True, body=body)
        assert check_ci_failure(pr, ci_has_failed=True) is False


class TestCheckStuckStack:
    """Tests for the stuck stack recovery check."""

    def test_base_is_default_branch(self) -> None:
        pr = _make_pr(base="main")
        assert check_stuck_stack(pr, "main", base_branch_exists=False) is False

    def test_base_exists(self) -> None:
        pr = _make_pr(base="repo-sync/private-to-public/old1234")
        assert check_stuck_stack(pr, "main", base_branch_exists=True) is False

    def test_base_missing(self) -> None:
        pr = _make_pr(base="repo-sync/private-to-public/old1234")
        assert check_stuck_stack(pr, "main", base_branch_exists=False) is True


class TestEvaluatePR:
    """Tests for the combined PR evaluation."""

    def test_no_actions_for_healthy_pr(self) -> None:
        pr = _make_pr(base="main")
        check = evaluate_pr(
            pr=pr,
            escalate_after=timedelta(hours=1),
            default_branch="main",
            ci_has_failed=False,
            base_branch_exists=True,
        )
        assert check.actions == []

    def test_multiple_actions_can_fire(self) -> None:
        now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        old_time = now - timedelta(hours=5)
        body = f"Repo-Sync-Assigned: someone@{old_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        pr = _make_pr(
            base="repo-sync/private-to-public/old1234",
            body=body,
            auto_merge=False,
        )
        check = evaluate_pr(
            pr=pr,
            escalate_after=timedelta(hours=1),
            default_branch="main",
            ci_has_failed=False,
            base_branch_exists=False,
            now=now,
        )
        assert EscalationAction.ESCALATE_TIMEOUT in check.actions
        assert EscalationAction.STUCK_STACK in check.actions

    def test_ci_failure_action(self) -> None:
        pr = _make_pr(base="main", auto_merge=True)
        check = evaluate_pr(
            pr=pr,
            escalate_after=timedelta(hours=1),
            default_branch="main",
            ci_has_failed=True,
            base_branch_exists=True,
        )
        assert EscalationAction.CI_FAILURE in check.actions
