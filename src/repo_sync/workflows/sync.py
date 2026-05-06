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
import subprocess
from dataclasses import dataclass

from repo_sync.stack.branches import check_idempotency
from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.loop_detection import is_sync_originated
from repo_sync.stack.reviewer import determine_reviewer
from repo_sync.stack.trailers import SyncOrigin, parse_origin
from repo_sync.stack.watermark import watermark_tag_name

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


def determine_direction(source_is_private: bool) -> str:
    """Return the sync direction label."""
    return "private-to-public" if source_is_private else "public-to-private"


def read_watermark_from_peer(
    peer_gh: GhOps,
    direction: str,
) -> SyncOrigin | None:
    """Read the watermark tag from the peer repo via the GitHub API.

    The watermark tag lives in the peer (target) repo.  This function reads it
    via the GH CLI API, avoiding the need to have the peer repo checked out.
    Returns the parsed SyncOrigin, or None if the tag does not exist.
    """
    tag_name = watermark_tag_name(direction)
    tag_sha = peer_gh.get_tag_sha(tag_name)
    if not tag_sha:
        return None

    commit_msg = peer_gh.get_commit_message(tag_sha)
    if not commit_msg:
        return None

    return parse_origin(commit_msg)


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
        try:
            if is_sync_originated(source_git, source_gh, sha):
                logger.info("Skipping sync-originated commit %s.", sha[:12])
                continue
        except subprocess.CalledProcessError:
            logger.error(
                "GitHub API failure during loop detection for commit %s. "
                "Aborting sync to avoid creating an infinite loop.",
                sha[:12],
            )
            raise
        result.append(sha)

    return result


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
    try:
        source_pr = source_gh.get_pr_for_commit(source_sha)
    except subprocess.CalledProcessError:
        logger.warning(
            "GitHub API error looking up PR for commit %s; "
            "falling back to commit-based description.",
            source_sha[:12],
        )
        source_pr = None

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


def get_commit_author(gh: GhOps, sha: str) -> str | None:
    """Look up the GitHub login of a commit's author via the API."""
    return gh.get_commit_author_login(sha)


def determine_sync_reviewer(
    source_gh: GhOps,
    source_sha: str,
    fallback_team: str,
) -> str:
    """Determine the reviewer for a conflict-resolution sync PR.

    Tries the merger of the source PR first, then the commit author (via
    GitHub API), then the fallback team.
    """
    try:
        source_pr = source_gh.get_pr_for_commit(source_sha)
    except subprocess.CalledProcessError:
        logger.warning(
            "GitHub API error looking up PR for commit %s; "
            "falling back to commit author or team for reviewer.",
            source_sha[:12],
        )
        source_pr = None
    pr_number = source_pr.number if source_pr else None

    # For direct pushes (no source PR), look up the commit author via the API.
    commit_author: str | None = None
    if source_pr is None:
        commit_author = get_commit_author(source_gh, source_sha)

    return determine_reviewer(
        source_gh,
        source_pr_number=pr_number,
        commit_author=commit_author,
        fallback_team=fallback_team,
    )
