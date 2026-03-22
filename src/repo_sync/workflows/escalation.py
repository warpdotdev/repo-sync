"""Escalation checks for open sync PRs.

Runs periodically (via cron) and performs three checks:
1. Timeout escalation: if a Repo-Sync-Assigned trailer exceeds the timeout.
2. CI failure detection: if auto-merge is enabled but CI has failed.
3. Stuck stack recovery: if a PR's base branch no longer exists.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.trailers import parse_assigned

logger = logging.getLogger(__name__)


class EscalationAction(Enum):
    """Actions to be taken by the YAML workflow layer."""

    ESCALATE_TIMEOUT = "escalate_timeout"
    CI_FAILURE = "ci_failure"
    STUCK_STACK = "stuck_stack"


@dataclass
class EscalationCheck:
    """Result of checking a single sync PR."""

    pr_number: int
    head_branch: str
    actions: list[EscalationAction] = field(default_factory=list)
    reviewer: str | None = None


def parse_duration(duration_str: str) -> timedelta:
    """Parse a duration string like '5m', '1h', '2h30m' into a timedelta.

    Supports hours (h) and minutes (m).  Raises ValueError on invalid input.
    """
    hours = 0
    minutes = 0

    hour_match = re.search(r"(\d+)h", duration_str)
    if hour_match:
        hours = int(hour_match.group(1))

    minute_match = re.search(r"(\d+)m", duration_str)
    if minute_match:
        minutes = int(minute_match.group(1))

    if hours == 0 and minutes == 0:
        raise ValueError(f"Could not parse duration: {duration_str}")

    return timedelta(hours=hours, minutes=minutes)


def check_timeout_escalation(
    pr: PullRequest,
    escalate_after: timedelta,
    now: datetime | None = None,
) -> bool:
    """Check if a PR's Repo-Sync-Assigned trailer has exceeded the timeout.

    Returns True if escalation should be triggered.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    assignment = parse_assigned(pr.body)
    if assignment is None:
        return False

    elapsed = now - assignment.timestamp
    return elapsed > escalate_after


def check_ci_failure(
    pr: PullRequest,
    ci_has_failed: bool,
) -> bool:
    """Check if a PR with auto-merge enabled has failed CI.

    Returns True if CI failure action should be taken.  Returns False if the PR
    already has a Repo-Sync-Assigned trailer (reviewer already assigned, don't
    reset the escalation clock).
    """
    if not pr.auto_merge_enabled:
        return False

    # Don't reset the escalation clock if a reviewer is already assigned.
    existing_assignment = parse_assigned(pr.body)
    if existing_assignment is not None:
        return False

    return ci_has_failed


def check_stuck_stack(
    pr: PullRequest,
    default_branch: str,
    base_branch_exists: bool,
) -> bool:
    """Check if a PR's base branch no longer exists (stuck stack).

    Returns True if a restack dispatch should be triggered.
    """
    if pr.base_branch == default_branch:
        # Base is the default branch -- not stuck.
        return False

    return not base_branch_exists


def evaluate_pr(
    pr: PullRequest,
    escalate_after: timedelta,
    default_branch: str,
    ci_has_failed: bool,
    base_branch_exists: bool,
    now: datetime | None = None,
) -> EscalationCheck:
    """Evaluate all escalation checks for a single PR.

    Returns an EscalationCheck with the list of actions to take.  The actual
    execution of these actions (API calls, workflow dispatch) is handled by
    the YAML workflow or CLI layer.
    """
    check = EscalationCheck(pr_number=pr.number, head_branch=pr.head_branch)

    if check_timeout_escalation(pr, escalate_after, now):
        check.actions.append(EscalationAction.ESCALATE_TIMEOUT)
        logger.info(
            "PR #%d: escalation timeout exceeded.", pr.number
        )

    if check_ci_failure(pr, ci_has_failed):
        check.actions.append(EscalationAction.CI_FAILURE)
        logger.info(
            "PR #%d: CI failed with auto-merge enabled.", pr.number
        )

    if check_stuck_stack(pr, default_branch, base_branch_exists):
        check.actions.append(EscalationAction.STUCK_STACK)
        logger.info(
            "PR #%d: base branch '%s' no longer exists (stuck stack).",
            pr.number,
            pr.base_branch,
        )

    return check
