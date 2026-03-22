"""Sync creation orchestration.

Enumerates unsynced commits on the source repo's default branch and creates
stacked sync PRs in the peer repo.  Handles both private-to-public and
public-to-private directions.

This module contains the decision-making logic.  External side effects (git
commands, GitHub API calls, Docker invocation) are delegated to the stack
library's GitOps/GhOps wrappers, which can be replaced in tests.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

# These imports reference the stack management library built by ws2.
# During integration, these will be real imports.  For now, we reference the
# expected interfaces.
from repo_sync.stack.branches import (
    check_idempotency,
    sync_branch_name,
)
from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.loop_detection import is_sync_originated
from repo_sync.stack.prs import create_sync_pr
from repo_sync.stack.reviewer import assign_reviewer, determine_reviewer
from repo_sync.stack.trailers import SyncOrigin, parse_origin
from repo_sync.stack.watermark import read_watermark

from repo_sync.workflows.descriptions import (
    PRDescription,
    private_to_public_default_title,
    private_to_public_fallback,
    public_to_private_from_commit,
    public_to_private_from_pr,
)

logger = logging.getLogger(__name__)


@dataclass
class SyncConfig:
    """Configuration for a sync workflow run."""

    source_repo: str
    peer_repo: str
    peer_default_branch: str
    source_is_private: bool
    escalate_to: str = "oncall-client-primary"
    slack_webhook_url: str = ""


@dataclass
class SyncPlan:
    """The plan produced by analyzing unsynced commits.

    This separates planning from execution so that tests can verify the plan
    without needing to execute side effects.
    """

    # Direction label: 'private-to-public' or 'public-to-private'.
    direction: str
    # Unsynced commit SHAs to process, in chronological order (oldest first).
    unsynced_commits: list[str] = field(default_factory=list)
    # Commits skipped because a sync branch/PR already exists.
    skipped_existing: list[str] = field(default_factory=list)
    # Commits skipped because the diff is empty (internal-only changes).
    skipped_empty: list[str] = field(default_factory=list)
    # Whether the triggering commit is itself sync-originated.
    trigger_is_sync: bool = False


def determine_direction(source_is_private: bool) -> str:
    """Return the sync direction label."""
    return "private-to-public" if source_is_private else "public-to-private"


def enumerate_unsynced_commits(
    source_git: GitOps,
    source_gh: GhOps,
    direction: str,
    default_branch: str,
    watermark_origin: SyncOrigin,
) -> list[str]:
    """Find all unsynced commits on the source repo's default branch.

    Returns commit SHAs in chronological order (oldest first), filtering out
    any commits that are themselves sync-originated.
    """
    last_sha = watermark_origin.sha
    range_spec = f"{last_sha}..{default_branch}"
    all_commits = source_git.log_oneline(range_spec)

    if not all_commits:
        return []

    # Filter out sync-originated commits.
    result = []
    for sha in all_commits:
        if is_sync_originated(source_git, source_gh, sha):
            logger.info("Skipping sync-originated commit %s.", sha[:12])
            continue
        result.append(sha)

    return result


def plan_sync(
    source_git: GitOps,
    source_gh: GhOps,
    peer_gh: GhOps,
    config: SyncConfig,
    trigger_sha: str,
    default_branch: str,
) -> SyncPlan:
    """Analyze the current state and produce a sync plan.

    This does not create any branches or PRs -- it only reads state and decides
    what needs to be done.
    """
    direction = determine_direction(config.source_is_private)
    plan = SyncPlan(direction=direction)

    # Check if the triggering commit is sync-originated.
    if is_sync_originated(source_git, source_gh, trigger_sha):
        plan.trigger_is_sync = True
        return plan

    # Read the watermark from the peer repo to find the last-synced source SHA.
    # Note: read_watermark operates on a local git repo.  The caller must ensure
    # the peer repo's watermark tag is fetched or use the GH API.  For the
    # workflow, we read the watermark via the GH API in the CLI layer and pass
    # the result here.
    watermark = read_watermark(source_git, direction)
    if watermark is None:
        raise RuntimeError(
            f"No watermark found for direction '{direction}'. Run bootstrap first."
        )

    plan.unsynced_commits = enumerate_unsynced_commits(
        source_git, source_gh, direction, default_branch, watermark
    )

    return plan


def find_existing_stack_top(
    peer_gh: GhOps,
    direction: str,
) -> str | None:
    """Find the top of the existing sync PR stack in the peer repo.

    Returns the head branch name of the top PR, or None if no stack exists.
    """
    prefix = f"repo-sync/{direction}/"
    open_prs = peer_gh.list_open_sync_prs()
    matching = [pr for pr in open_prs if pr.head_branch.startswith(prefix)]
    if not matching:
        return None
    # Sort by PR number (ascending) and take the last one (highest = most recent).
    matching.sort(key=lambda pr: pr.number)
    return matching[-1].head_branch


@dataclass
class CommitSyncResult:
    """Result of syncing a single commit."""

    sha: str
    short_sha: str
    skipped: bool = False
    skip_reason: str = ""
    pr: PullRequest | None = None
    conflict: bool = False


def check_commit_idempotency(
    source_git: GitOps,
    peer_gh: GhOps,
    branch_name: str,
) -> bool:
    """Check if a sync branch or PR already exists for a commit.

    Returns True if the commit should be skipped (already processed).
    """
    result = check_idempotency(source_git, peer_gh, branch_name)
    return result.already_exists


def build_public_to_private_description(
    source_gh: GhOps,
    source_repo: str,
    source_sha: str,
    commit_subject: str,
    commit_body: str,
) -> PRDescription:
    """Build PR title/body for a public-to-private sync PR.

    Tries to find the source PR first.  Falls back to the commit message for
    direct pushes.
    """
    source_repo_name = source_repo.split("/")[-1]
    source_pr = source_gh.get_pr_for_commit(source_sha)

    if source_pr is not None:
        return public_to_private_from_pr(
            source_repo_name=source_repo_name,
            source_pr_title=source_pr.title,
            source_pr_body=source_pr.body,
            source_pr_url=source_pr.url,
        )

    commit_url = f"https://github.com/{source_repo}/commit/{source_sha}"
    return public_to_private_from_commit(
        source_repo_name=source_repo_name,
        commit_subject=commit_subject,
        commit_body=commit_body,
        commit_url=commit_url,
    )


def determine_sync_reviewer(
    source_gh: GhOps,
    source_sha: str,
    fallback_team: str,
) -> str:
    """Determine the reviewer for a conflict-resolution sync PR.

    Delegates to the stack library's reviewer.determine_reviewer, looking up
    the source PR merger and commit author.
    """
    source_pr = source_gh.get_pr_for_commit(source_sha)
    pr_number = source_pr.number if source_pr else None
    # For commit author, we'd need to look it up via the GH API.
    # The stack library's determine_reviewer handles this.
    commit_author: str | None = None
    if source_pr is None:
        # No source PR -- try commit author via GH API.
        # This would be done in gh_ops; for now we pass None and let fallback work.
        pass

    return determine_reviewer(
        source_gh,
        source_pr_number=pr_number,
        commit_author=commit_author,
        fallback_team=fallback_team,
    )
