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

from dataclasses import dataclass
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from datetime import datetime, timezone

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.trailers import (
    SyncOrigin,
    append_trailer,
    format_assigned_trailer,
    format_conflict_trailer,
)
from repo_sync.workflows.descriptions import parse_agent_output, private_to_public_fallback
from repo_sync.workflows.sync import (
    build_public_to_private_description,
    determine_sync_reviewer,
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


@dataclass
class _PrDescriptionCache:
    """Mutable cache for the last generated PR description.

    Populated before creating the PR so that the description survives a
    TransientSyncError and can be reused on retry instead of re-running
    the expensive PR description agent.
    """

    source_sha: str | None = None
    title: str = ""
    body: str = ""


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


def _run_fixup_script(script_path: str, working_dir: str) -> bool:
    """Run an optional fixup script on a directory.

    The script receives the working directory as its sole argument.
    Returns True on success, False on failure.
    """
    try:
        result = subprocess.run(
            [script_path, working_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                "Fixup script %s failed (exit %d).\nstdout: %s\nstderr: %s",
                script_path, result.returncode,
                result.stdout[:1000], result.stderr[:1000],
            )
        return result.returncode == 0
    except Exception:
        logger.error("Fixup script %s raised an exception.", script_path, exc_info=True)
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
    fixup_script: str = "",
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
    if fixup_script and not _run_fixup_script(fixup_script, snapshot_dir):
        _notify_slack(
            slack_webhook_url,
            f"repo-sync: fixup script failed for {short_sha} in {source_repo}.",
        )
        raise PermanentSyncError(
            f"Fixup script failed for {short_sha}."
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
    if fixup_script and not _run_fixup_script(fixup_script, prev_snapshot_dir):
        _notify_slack(
            slack_webhook_url,
            f"repo-sync: fixup script failed for parent of {short_sha} in {source_repo}.",
        )
        raise PermanentSyncError(
            f"Fixup script failed for parent of {short_sha}."
        )

    # Compute diff between the two clean snapshots using a temp git repo.
    diff_repo = f"/tmp/diff-repo-{short_sha}"
    os.makedirs(diff_repo, exist_ok=True)
    diff_git = GitOps(diff_repo)
    subprocess.run(["git", "init", "-q", diff_repo], check=True, capture_output=True)
    diff_git._run(["config", "user.name", "warp-repo-sync[bot]"])
    diff_git._run(["config", "user.email", "270220925+warp-repo-sync[bot]@users.noreply.github.com"])

    # Commit the previous snapshot.
    shutil.copytree(prev_snapshot_dir, diff_repo, dirs_exist_ok=True, symlinks=True)
    diff_git.add_all()
    diff_git.commit("prev", allow_empty=True)

    # Replace with the current snapshot.
    diff_git.rm_tracked_files()
    shutil.copytree(snapshot_dir, diff_repo, dirs_exist_ok=True, symlinks=True)
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
    patch_content = diff_git.diff_patch("HEAD~1", "HEAD")
    with open(patch_file, "w") as f:
        f.write(patch_content)

    diff_commit = diff_git.rev_parse("HEAD")
    return snapshot_dir, prev_snapshot_dir, diff_repo, patch_file, diff_commit


def _pr_number_from_url(url: str) -> int | None:
    """Extract a PR number from a GitHub PR URL.

    Expected format: https://github.com/<owner>/<repo>/pull/<number>
    Returns None if the URL does not match.
    """
    try:
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == "pull":
            return int(parts[-1])
    except (ValueError, IndexError):
        pass
    return None


def _push_branch(
    peer_gh: GhOps,
    peer_git: GitOps,
    sync_branch: str,
) -> None:
    """Push the sync branch.  Force-pushes if it already exists on the remote."""
    if peer_gh.branch_exists_on_remote(sync_branch):
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
) -> str:
    """Create a PR and return its URL.

    Raises TransientSyncError if the base branch was deleted.
    """
    try:
        return peer_gh.create_pr_simple(
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


def _handle_cherry_pick_conflict(
    peer_git: GitOps,
    peer_gh: GhOps,
    short_sha: str,
    source_sha: str,
    source_repo: str,
    sync_branch: str,
    stack_base_branch: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    slack_webhook_url: str,
    escalate_to: str,
) -> None:
    """Handle a cherry-pick conflict by creating a conflict PR.

    Commits the raw conflict state (with markers or modify/delete artifacts),
    optionally invokes the conflict-resolution agent to add a resolution
    commit on top, then creates the PR with appropriate trailers and labels.
    """
    # Step 1: Commit the raw conflict as-is.
    origin_trailer = f"Repo-Sync-Origin: {source_repo}@{source_sha}"
    peer_git.add_all()
    peer_git.commit(commit_message, trailers=[origin_trailer])

    # Step 2: Invoke the conflict-resolution agent to produce a separate
    # resolution commit on top.  The agent runs in Docker and sees the
    # committed conflict markers (case 2 in the skill).
    from repo_sync.workflows.agent import run_conflict_resolution_agent

    agent_succeeded = run_conflict_resolution_agent(
        repo_dir=peer_git.repo_dir,
        context=f"Cherry-pick conflict on branch {sync_branch}.",
    )
    if agent_succeeded:
        logger.info("Agent produced a resolution commit for %s.", short_sha)
    else:
        logger.warning(
            "Agent did not resolve conflicts for %s. "
            "PR will contain raw conflict markers.", short_sha,
        )

    # Step 3: Push and create the PR.
    conflict_trailer = format_conflict_trailer()
    pr_body = append_trailer(pr_body, origin_trailer)
    pr_body = append_trailer(pr_body, conflict_trailer)

    _push_branch(peer_gh, peer_git, sync_branch)
    pr_url = _create_pr(
        peer_gh, sync_branch, stack_base_branch, pr_title, pr_body,
    )
    pr_number = _pr_number_from_url(pr_url)

    if pr_number is None:
        logger.error(
            "Could not extract PR number from URL: %s. "
            "Skipping label and reviewer assignment.", pr_url,
        )
        return

    # Step 4: Add conflict label.
    peer_gh._run(
        ["label", "create", "repo-sync:conflict",
         "--color", "D93F0B",
         "--description", "Sync PR has merge conflicts",
         "--repo", peer_gh.repo],
        check=False,
    )
    peer_gh._run(
        ["pr", "edit", str(pr_number), "--repo", peer_gh.repo,
         "--add-label", "repo-sync:conflict"],
    )

    # Step 5: Determine and assign reviewer.
    source_gh = GhOps(source_repo, token=os.environ.get("GH_TOKEN"))
    reviewer = determine_sync_reviewer(
        source_gh=source_gh,
        source_sha=source_sha,
        fallback_team=escalate_to,
    )
    peer_gh._run(
        ["pr", "edit", str(pr_number), "--repo", peer_gh.repo,
         "--add-reviewer", reviewer],
        check=False,
    )

    # Step 6: Add Repo-Sync-Assigned trailer to PR body.
    assigned_trailer = format_assigned_trailer(reviewer, datetime.now(timezone.utc))
    current_body = peer_gh._run(
        ["pr", "view", str(pr_number), "--repo", peer_gh.repo,
         "--json", "body", "--jq", ".body"],
        check=False,
    ) or ""
    updated_body = append_trailer(current_body, assigned_trailer)
    peer_gh.update_pr_body(pr_number, updated_body)

    logger.info(
        "Conflict PR #%d created for %s. Reviewer: %s.",
        pr_number, short_sha, reviewer,
    )

    # Step 7: Slack notification.
    msg = (
        f"repo-sync: cherry-pick conflict for {source_sha[:7]} in "
        f"{peer_gh.repo}. Conflict PR #{pr_number} created."
    )
    _notify_slack(slack_webhook_url, msg)


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
    pr_desc_cache: _PrDescriptionCache,
    fixup_script: str = "",
    escalate_to: str = "@oncall-client-primary",
) -> bool:
    """Handle private-to-public sync for a single commit.

    Returns True if a PR was created, False if the commit was skipped
    (empty diff or empty cherry-pick).
    Raises PermanentSyncError on stripping failures.
    Raises TransientSyncError on base-branch-deleted failures.
    """
    # Create the diff repo with consecutive clean snapshots.
    logger.info("Creating clean diff for %s...", short_sha)
    diff_result = _create_diff_repo(
        source_git, source_sha, short_sha, slack_webhook_url, source_repo,
        fixup_script=fixup_script,
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

            # Real conflict.  Log diagnostics and create a conflict PR.
            logger.error(
                "Cherry-pick failed for %s. returncode=%d\n"
                "stdout: %s\nstderr: %s\nconflicting files: %s",
                short_sha, cp_result.returncode,
                cp_result.stdout, cp_result.stderr, conflicting,
            )
            diff_stat = peer_git._run(
                ["diff", "--stat", "--cached"], check=False,
            )
            logger.error(
                "Staged diff stat at time of conflict:\n%s",
                diff_stat.stdout,
            )

            # Build the PR description before creating the conflict PR.
            fallback = private_to_public_fallback(short_sha)
            pr_title = f"[CONFLICT] {fallback.title}"
            pr_body = fallback.body

            _handle_cherry_pick_conflict(
                peer_git=peer_git,
                peer_gh=peer_gh,
                short_sha=short_sha,
                source_sha=source_sha,
                source_repo=source_repo,
                sync_branch=sync_branch,
                stack_base_branch=stack_base_branch,
                commit_message="repo-sync: sync from private (conflict)",
                pr_title=pr_title,
                pr_body=pr_body,
                slack_webhook_url=slack_webhook_url,
                escalate_to=escalate_to,
            )
            return True

        # Amend the commit message with a generic message + trailer.
        trailer = f"Repo-Sync-Origin: {source_repo}@{source_sha}"
        peer_git.commit_amend_message("repo-sync: sync from private", trailer)

        # PR description: reuse cached result if retrying the same commit.
        if pr_desc_cache.source_sha == source_sha:
            logger.info(
                "Reusing cached PR description for %s.", short_sha,
            )
            pr_title = pr_desc_cache.title
            pr_body = pr_desc_cache.body
        else:
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

        # Cache the description before the PR creation step, which may
        # raise TransientSyncError.
        pr_desc_cache.source_sha = source_sha
        pr_desc_cache.title = pr_title
        pr_desc_cache.body = pr_body

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
    escalate_to: str = "@oncall-client-primary",
) -> bool:
    """Handle public-to-private sync for a single commit.

    Returns True if a PR was created.
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
        conflicting = peer_git.conflicting_files()
        logger.error(
            "Cherry-pick failed for %s. returncode=%d\n"
            "stdout: %s\nstderr: %s\nconflicting files: %s",
            short_sha, cp_result.returncode,
            cp_result.stdout, cp_result.stderr, conflicting,
        )

        # Build the conflict PR description from the source commit.
        commit_subject = peer_git._run(
            ["log", "-1", "--format=%s", source_sha], check=False,
        ).stdout or "sync"

        _handle_cherry_pick_conflict(
            peer_git=peer_git,
            peer_gh=peer_gh,
            short_sha=short_sha,
            source_sha=source_sha,
            source_repo=source_repo,
            sync_branch=sync_branch,
            stack_base_branch=stack_base_branch,
            commit_message=commit_subject,
            pr_title=f"[CONFLICT] {commit_subject}",
            pr_body=f"Cherry-pick conflict for `{source_sha[:7]}` from {source_repo}.",
            slack_webhook_url=slack_webhook_url,
            escalate_to=escalate_to,
        )
        return True

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
    fixup_script: str = "",
    escalate_to: str = "@oncall-client-primary",
) -> None:
    """Create sync PRs for all unsynced commits.

    Implements the outer retry loop with progress-based termination and
    the per-commit idempotency guard.
    """
    last_failure_sha = ""
    pr_desc_cache = _PrDescriptionCache()

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
                        pr_desc_cache=pr_desc_cache,
                        fixup_script=fixup_script,
                        escalate_to=escalate_to,
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
                        escalate_to=escalate_to,
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
    private_to_public_fixup_script: str = "",
    public_to_private_fixup_script: str = "",
    escalate_to: str = "@oncall-client-primary",
) -> None:
    """Run the full sync workflow: watermark, commit enumeration, PR creation.

    Derives direction, peer repo, and source_is_private from the repo names.
    """
    source_is_private = source_repo == private_repo
    peer_repo = public_repo if source_is_private else private_repo
    direction = "private-to-public" if source_is_private else "public-to-private"
    branch_prefix = f"repo-sync/{direction}"

    # Select the fixup script for this direction.
    if source_is_private:
        fixup_script = os.path.abspath(private_to_public_fixup_script) if private_to_public_fixup_script else ""
    else:
        if public_to_private_fixup_script:
            logger.warning(
                "public-to-private fixup script is not yet implemented; ignoring '%s'.",
                public_to_private_fixup_script,
            )
        fixup_script = ""

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
        fixup_script=fixup_script,
        escalate_to=escalate_to,
    )
