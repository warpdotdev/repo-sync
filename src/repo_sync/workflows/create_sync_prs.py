"""Sync PR creation loop.

Migrated from the shell logic in sync.yml's "Create sync PRs" step.
This module handles the full lifecycle of creating sync PRs for unsynced
commits: idempotency guard, snapshot diffing (private-to-public),
cherry-pick (public-to-private), push, and PR creation.

The outer retry loop handles transient failures (e.g., base branch deleted
mid-loop because a PR merged via auto-merge).  The idempotency guard makes
restarts safe.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import SyncOrigin
from repo_sync.workflows.descriptions import parse_agent_output, private_to_public_fallback
from repo_sync.workflows.sync import (
    build_public_to_private_description,
    enumerate_unsynced_commits,
    find_existing_stack_top,
    read_watermark_from_peer,
)

logger = logging.getLogger(__name__)


class TransientSyncError(Exception):
    """Raised when a transient failure occurs that should trigger a loop restart."""

    pass


class PermanentSyncError(Exception):
    """Raised when a permanent failure occurs that should stop the workflow."""

    pass


def _notify_slack(webhook_url: str, message: str) -> None:
    """Send a Slack notification.  Errors are silently ignored."""
    if not webhook_url:
        return
    try:
        subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                "-H", "Content-type: application/json",
                "--data", f'{{"text":"{message}"}}',
                webhook_url,
            ],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def _run_strip(snapshot_dir: str) -> bool:
    """Run the stripping tool on a snapshot directory.

    Returns True on success, False on failure.
    """
    try:
        # The strip CLI is designed to be called as `python -m repo_sync.strip.cli <dir>`.
        # We call it via subprocess to match the workflow behavior exactly.
        result = subprocess.run(
            ["python", "-m", "repo_sync.strip.cli", snapshot_dir],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


# Docker image for the PR description agent.  Built locally by the sync
# workflow from docker/pr-description/Dockerfile (not pushed to a registry).
# Can be overridden via environment variable for testing.
PR_DESCRIPTION_IMAGE = os.environ.get(
    "REPO_SYNC_PR_DESCRIPTION_IMAGE",
    "repo-sync-pr-description",
)


def _run_pr_description_agent(
    snapshot_dir: str,
    patch_file: str,
    short_sha: str,
) -> tuple[str | None, str | None]:
    """Run the PR description agent via Docker.

    The agent runs inside an isolated container with:
      - the clean codebase snapshot mounted at /mnt/snapshot (read-only)
      - the public diff mounted at /mnt/diff/public.diff (read-only)
      - the pr-description skill baked into the image

    The agent writes its output to stdout as JSON lines.  This function
    extracts text from ``{"type": "agent"}`` messages and parses the
    structured TITLE/DESCRIPTION format.

    Returns (title, body) if the agent succeeds, (None, None) otherwise.
    """
    try:
        print(f"::group::Generating PR description for {short_sha}", flush=True)

        proc = subprocess.Popen(
            [
                "docker", "run", "--rm",
                "-e", "WARP_API_KEY",
                "-v", f"{os.path.abspath(snapshot_dir)}:/mnt/snapshot:ro",
                "-v", f"{os.path.abspath(patch_file)}:/mnt/diff/public.diff:ro",
                PR_DESCRIPTION_IMAGE,
                "agent", "run",
                "--skill", "pr-description",
                "--cwd", "/mnt/snapshot",
                "--output-format", "json",
                "--model", "claude-4-5-haiku",
                "--share",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Use a timer to enforce a timeout, since we read stdout line by
        # line and cannot use Popen.wait(timeout=) concurrently.
        timed_out = False

        def _kill_on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            proc.kill()

        timer = threading.Timer(300, _kill_on_timeout)
        timer.start()

        stdout_lines: list[str] = []
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                stdout_lines.append(line)
            proc.wait()
        finally:
            timer.cancel()
            print("::endgroup::", flush=True)

        if timed_out:
            logger.warning("PR description agent timed out.")
            return None, None

        if proc.returncode != 0:
            stderr_output = proc.stderr.read() if proc.stderr else ""
            logger.warning(
                "PR description agent exited with code %d: %s",
                proc.returncode,
                (stderr_output or "")[:500],
            )
            return None, None

        # Parse agent text from JSON-lines stdout.
        agent_text_parts: list[str] = []
        for line in stdout_lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "agent":
                agent_text_parts.append(msg.get("text", ""))

        agent_text = "".join(agent_text_parts)
        desc = parse_agent_output(agent_text)
        if desc:
            return desc.title, desc.body

        logger.warning("PR description agent produced no parseable output.")
        return None, None

    except Exception:
        logger.warning("PR description agent failed.", exc_info=True)
        return None, None


def _create_diff_repo(
    source_git: GitOps,
    source_sha: str,
    short_sha: str,
    slack_webhook_url: str,
    source_repo: str,
) -> tuple[str, str, str, str, str] | None:
    """Create a temp git repo with consecutive clean snapshots.

    Returns (snapshot_dir, prev_snapshot_dir, diff_repo, patch_file, diff_commit)
    or None if the diff is empty (all changes were internal-only).
    Raises PermanentSyncError on stripping failures.
    """
    snapshot_dir = f"/tmp/snapshot-{short_sha}"
    os.makedirs(snapshot_dir, exist_ok=True)
    source_git.archive_to_dir(source_sha, snapshot_dir)
    if not _run_strip(snapshot_dir):
        _notify_slack(
            slack_webhook_url,
            f"repo-sync: stripping failed for {short_sha} in {source_repo}.",
        )
        raise PermanentSyncError(
            f"Stripping failed for {short_sha}."
        )

    # Generate clean snapshot at the parent commit.
    prev_sha = source_git.rev_parse(f"{source_sha}^")
    prev_snapshot_dir = f"/tmp/snapshot-prev-{short_sha}"
    os.makedirs(prev_snapshot_dir, exist_ok=True)
    source_git.archive_to_dir(prev_sha, prev_snapshot_dir)
    if not _run_strip(prev_snapshot_dir):
        _notify_slack(
            slack_webhook_url,
            f"repo-sync: stripping failed for parent of {short_sha} in {source_repo}.",
        )
        raise PermanentSyncError(
            f"Stripping failed for parent of {short_sha}."
        )

    # Compute diff between the two clean snapshots using a temp git repo.
    diff_repo = f"/tmp/diff-repo-{short_sha}"
    os.makedirs(diff_repo, exist_ok=True)
    diff_git = GitOps(diff_repo)
    subprocess.run(["git", "init", "-q", diff_repo], check=True, capture_output=True)
    diff_git._run(["config", "user.name", "warp-repo-sync[bot]"])
    diff_git._run(["config", "user.email", "270220925+warp-repo-sync[bot]@users.noreply.github.com"])

    # Commit the previous snapshot.
    shutil.copytree(prev_snapshot_dir, diff_repo, dirs_exist_ok=True)
    diff_git.add_all()
    diff_git.commit("prev", allow_empty=True)

    # Replace with the current snapshot.
    diff_git.rm_tracked_files()
    shutil.copytree(snapshot_dir, diff_repo, dirs_exist_ok=True)
    diff_git.add_all()
    diff_git.commit("curr", allow_empty=True)

    # Check if the diff is empty (all changes were internal-only).
    if diff_git.diff_is_empty("HEAD~1", "HEAD"):
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        shutil.rmtree(prev_snapshot_dir, ignore_errors=True)
        shutil.rmtree(diff_repo, ignore_errors=True)
        return None

    # Generate the patch for the PR description agent.
    patch_file = f"/tmp/patch-{short_sha}.patch"
    patch_content = diff_git.diff_binary_patch("HEAD~1", "HEAD")
    with open(patch_file, "w") as f:
        f.write(patch_content)

    diff_commit = diff_git.rev_parse("HEAD")
    return snapshot_dir, prev_snapshot_dir, diff_repo, patch_file, diff_commit


def _push_branch(
    peer_gh: GhOps,
    peer_git: GitOps,
    sync_branch: str,
) -> None:
    """Push the sync branch.  Force-pushes if it already exists on the remote."""
    remote_ref = peer_gh.get_branch_ref_sha(sync_branch)
    if remote_ref:
        logger.warning(
            "Branch %s already exists on remote. Force-pushing.", sync_branch
        )
        peer_git.push("origin", sync_branch, force=True)
    else:
        peer_git.push("origin", sync_branch)


def _create_pr(
    peer_gh: GhOps,
    sync_branch: str,
    stack_base_branch: str,
    pr_title: str,
    pr_body: str,
) -> None:
    """Create a PR.  Raises TransientSyncError if the base branch was deleted."""
    try:
        peer_gh.create_pr_simple(
            head=sync_branch,
            base=stack_base_branch,
            title=pr_title,
            body=pr_body,
        )
    except subprocess.CalledProcessError as e:
        error_output = (e.stdout or "") + (e.stderr or "")
        if "Base ref must be a branch" in error_output:
            raise TransientSyncError(
                f"Base branch {stack_base_branch} no longer exists."
            ) from e
        raise


def _sync_private_to_public(
    source_git: GitOps,
    peer_git: GitOps,
    peer_gh: GhOps,
    source_sha: str,
    short_sha: str,
    sync_branch: str,
    stack_base_branch: str,
    source_repo: str,
    slack_webhook_url: str,
) -> bool:
    """Handle private-to-public sync for a single commit.

    Returns True if a PR was created, False if the commit was skipped
    (empty diff or empty cherry-pick).
    Raises PermanentSyncError on stripping/conflict failures.
    Raises TransientSyncError on base-branch-deleted failures.
    """
    # Create the diff repo with consecutive clean snapshots.
    diff_result = _create_diff_repo(
        source_git, source_sha, short_sha, slack_webhook_url, source_repo,
    )
    if diff_result is None:
        return False  # Empty diff — all changes were internal-only.

    snapshot_dir, prev_snapshot_dir, diff_repo, patch_file, diff_commit = diff_result

    try:
        # Apply the delta to the peer repo by cherry-picking from the temp repo.
        try:
            peer_git.checkout_force_branch(
                "_sync_work", f"origin/{stack_base_branch}"
            )
        except subprocess.CalledProcessError:
            peer_git.checkout_force_branch("_sync_work", stack_base_branch)
        peer_git.checkout_force_branch(sync_branch)
        peer_git.remote_add_or_update("diff_source", diff_repo)
        peer_git.fetch("diff_source")

        cp_result = peer_git.cherry_pick(diff_commit, allow_empty=True)
        if not cp_result.success:
            # Distinguish between empty cherry-pick and real conflict.
            conflicting = peer_git.conflicting_files()
            if not conflicting:
                # No conflicts — the delta produces no net change.  Skip.
                logger.info(
                    "Cherry-pick produced no changes (delta already applied). "
                    "Skipping %s.", short_sha,
                )
                peer_git.cherry_pick_abort()
                return False

            # Real conflict.
            peer_git.cherry_pick_abort()
            msg = (
                f"repo-sync: clean delta cherry-pick failed for {source_sha} "
                f"in {source_repo}. See RUNBOOK.md."
            )
            _notify_slack(slack_webhook_url, msg)
            raise PermanentSyncError(msg)

        # Amend the commit message with a generic message + trailer.
        trailer = f"Repo-Sync-Origin: {source_repo}@{source_sha}"
        peer_git.commit_amend_message("repo-sync: sync from private", trailer)

        # PR description agent.
        fallback = private_to_public_fallback(short_sha)
        pr_title = fallback.title
        pr_body = fallback.body

        agent_title, agent_body = _run_pr_description_agent(
            snapshot_dir, patch_file, short_sha,
        )
        if agent_title:
            pr_title = agent_title
        if agent_body:
            pr_body = agent_body

        pr_body = f"{pr_body}\n\nRepo-Sync-Origin: {source_repo}@{source_sha}"

        _push_branch(peer_gh, peer_git, sync_branch)
        _create_pr(peer_gh, sync_branch, stack_base_branch, pr_title, pr_body)
        return True

    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        shutil.rmtree(prev_snapshot_dir, ignore_errors=True)
        shutil.rmtree(diff_repo, ignore_errors=True)
        if os.path.exists(patch_file):
            os.remove(patch_file)


def _sync_public_to_private(
    source_git: GitOps,
    peer_git: GitOps,
    peer_gh: GhOps,
    source_sha: str,
    short_sha: str,
    sync_branch: str,
    stack_base_branch: str,
    source_repo: str,
    slack_webhook_url: str,
) -> bool:
    """Handle public-to-private sync for a single commit.

    Returns True if a PR was created.
    Raises PermanentSyncError on cherry-pick failures.
    Raises TransientSyncError on base-branch-deleted failures.
    """
    # Add the source repo as a remote.  Use the absolute path because
    # peer_git runs with cwd=peer_repo_dir, so a relative path would
    # resolve to the wrong location.
    peer_git.remote_add_or_update("source", os.path.abspath(source_git.repo_dir))
    peer_git.fetch("source")

    try:
        peer_git.checkout_force_branch(
            "_sync_work", f"origin/{stack_base_branch}"
        )
    except subprocess.CalledProcessError:
        peer_git.checkout_force_branch("_sync_work", stack_base_branch)
    peer_git.checkout_force_branch(sync_branch)

    # Cherry-pick the public commit.
    cp_result = peer_git.cherry_pick(source_sha, allow_empty=True, x=True)
    if not cp_result.success:
        peer_git.cherry_pick_abort()
        msg = (
            f"repo-sync: cherry-pick failed for {short_sha} "
            f"in {source_repo}. See RUNBOOK.md."
        )
        _notify_slack(slack_webhook_url, msg)
        raise PermanentSyncError(msg)

    # Append Repo-Sync-Origin trailer to the cherry-picked commit.
    current_msg = peer_git.commit_message("HEAD")
    trailer = f"Repo-Sync-Origin: {source_repo}@{source_sha}"
    peer_git.commit_amend_message(current_msg, trailer)

    # Build description via Python.
    source_gh = GhOps(source_repo, token=os.environ.get("GH_TOKEN"))
    commit_subject = peer_git._run(
        ["log", "-1", "--format=%s", source_sha], check=False,
    ).stdout or "sync"
    commit_body = peer_git._run(
        ["log", "-1", "--format=%b", source_sha], check=False,
    ).stdout or ""

    desc = build_public_to_private_description(
        source_gh=source_gh,
        source_repo=source_repo,
        source_sha=source_sha,
        commit_subject=commit_subject,
        commit_body=commit_body,
    )
    pr_title = desc.title
    pr_body = f"{desc.body}\n\nRepo-Sync-Origin: {source_repo}@{source_sha}"

    _push_branch(peer_gh, peer_git, sync_branch)
    _create_pr(peer_gh, sync_branch, stack_base_branch, pr_title, pr_body)
    return True


def create_sync_prs(
    source_git: GitOps,
    peer_git: GitOps,
    peer_gh: GhOps,
    unsynced_commits: list[str],
    source_repo: str,
    peer_repo: str,
    direction: str,
    branch_prefix: str,
    source_is_private: bool,
    default_branch: str,
    stack_top: str | None,
    slack_webhook_url: str = "",
) -> None:
    """Create sync PRs for all unsynced commits.

    Implements the outer retry loop with progress-based termination and
    the per-commit idempotency guard.
    """
    last_failure_sha = ""

    while True:
        stack_base_branch = stack_top or default_branch
        transient_failure = False
        failure_sha = ""

        for source_sha in unsynced_commits:
            if not source_sha:
                continue
            short_sha = source_sha[:7]
            sync_branch = f"{branch_prefix}/{short_sha}"

            # Idempotency guard: check if a PR was previously created.
            existing_state = peer_gh.get_pr_state_for_branch(sync_branch)
            if existing_state:
                if existing_state == "OPEN":
                    # PR is open — use its branch as the stack base.
                    stack_base_branch = sync_branch
                # For MERGED PRs, skip but don't update stack_base_branch.
                continue

            # No PR found — proceed with the full sync flow.
            try:
                if source_is_private:
                    created = _sync_private_to_public(
                        source_git=source_git,
                        peer_git=peer_git,
                        peer_gh=peer_gh,
                        source_sha=source_sha,
                        short_sha=short_sha,
                        sync_branch=sync_branch,
                        stack_base_branch=stack_base_branch,
                        source_repo=source_repo,
                        slack_webhook_url=slack_webhook_url,
                    )
                else:
                    created = _sync_public_to_private(
                        source_git=source_git,
                        peer_git=peer_git,
                        peer_gh=peer_gh,
                        source_sha=source_sha,
                        short_sha=short_sha,
                        sync_branch=sync_branch,
                        stack_base_branch=stack_base_branch,
                        source_repo=source_repo,
                        slack_webhook_url=slack_webhook_url,
                    )

                if created:
                    stack_base_branch = sync_branch

            except TransientSyncError as e:
                logger.warning("Transient failure: %s", e)
                transient_failure = True
                failure_sha = source_sha
                break

        # Outer retry loop: check if we should restart.
        if not transient_failure:
            break  # All commits processed successfully.

        if failure_sha == last_failure_sha:
            raise PermanentSyncError(
                f"Stuck on commit {failure_sha[:7]} after retry. Giving up."
            )

        last_failure_sha = failure_sha
        logger.info("Sleeping 5s before retry...")
        time.sleep(5)
        # Fetch latest state in the peer repo before retrying.
        peer_git.fetch("origin")


def run_sync(
    source_repo_dir: str,
    peer_repo_dir: str,
    source_repo: str,
    public_repo: str,
    private_repo: str,
    default_branch: str,
    slack_webhook_url: str = "",
) -> None:
    """Run the full sync workflow: watermark, commit enumeration, PR creation.

    Derives direction, peer repo, and source_is_private from the repo names.
    """
    source_is_private = source_repo == private_repo
    peer_repo = public_repo if source_is_private else private_repo
    direction = "private-to-public" if source_is_private else "public-to-private"
    branch_prefix = f"repo-sync/{direction}"

    source_git = GitOps(source_repo_dir)
    source_gh = GhOps(source_repo, token=os.environ.get("GH_TOKEN"))
    peer_git = GitOps(peer_repo_dir)
    peer_gh = GhOps(peer_repo, token=os.environ.get("GH_TOKEN"))

    # Read watermark.
    watermark = read_watermark_from_peer(peer_gh, direction)
    if watermark is None:
        raise PermanentSyncError(
            f"No watermark for direction '{direction}' in {peer_repo}."
        )
    logger.info("Watermark: %s@%s", watermark.repo, watermark.sha)

    # Enumerate unsynced commits.
    unsynced = enumerate_unsynced_commits(
        source_git, source_gh, direction, default_branch,
        SyncOrigin(repo=watermark.repo, sha=watermark.sha),
    )
    if not unsynced:
        logger.info("No unsynced commits to process.")
        return
    logger.info("%d unsynced commit(s) to process.", len(unsynced))

    # Find existing stack top.
    stack_top = find_existing_stack_top(peer_gh, direction)

    # Create sync PRs.
    create_sync_prs(
        source_git=source_git,
        peer_git=peer_git,
        peer_gh=peer_gh,
        unsynced_commits=unsynced,
        source_repo=source_repo,
        peer_repo=peer_repo,
        direction=direction,
        branch_prefix=branch_prefix,
        source_is_private=source_is_private,
        default_branch=default_branch,
        stack_top=stack_top,
        slack_webhook_url=slack_webhook_url,
    )
