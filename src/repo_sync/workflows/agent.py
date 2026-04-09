"""Shared agent invocation helpers.

Provides Docker-based invocation for the conflict-resolution agent, used
by both create_sync_prs.py (cherry-pick conflicts) and approve_logic.py
(rebase conflicts).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

# Docker image for the conflict-resolution agent.  Built locally by the
# sync/approve workflows from docker/conflict-resolution/Dockerfile (not
# pushed to a registry).  Can be overridden via environment variable for
# testing.
CONFLICT_RESOLUTION_IMAGE = os.environ.get(
    "REPO_SYNC_CONFLICT_RESOLUTION_IMAGE",
    "repo-sync-conflict-resolution",
)

# Git identity used by the conflict-resolution agent inside Docker.
_GIT_USER_NAME = "warp-repo-sync[bot]"
_GIT_USER_EMAIL = "270220925+warp-repo-sync[bot]@users.noreply.github.com"


def run_conflict_resolution_agent(
    repo_dir: str,
    context: str = "",
    timeout_seconds: int = 600,
) -> bool:
    """Run the conflict-resolution agent via Docker.

    The agent runs inside an isolated container with the repo mounted at
    /mnt/repo (read-write).  It resolves conflict markers, verifies the
    result compiles, and commits the resolution.  It does NOT push.

    The ``context`` parameter is currently unused (the oz CLI does not
    support a --context flag), but is accepted for forward compatibility.
    The skill detects the conflict type from the repo state.

    Returns True if the agent succeeded (exit code 0), False otherwise.
    """
    try:
        print("::group::Running conflict-resolution agent", flush=True)

        proc = subprocess.Popen(
            [
                "docker", "run", "--rm",
                "-e", "WARP_API_KEY",
                "-e", "GH_TOKEN",
                "-e", f"GIT_USER_NAME={_GIT_USER_NAME}",
                "-e", f"GIT_USER_EMAIL={_GIT_USER_EMAIL}",
                "-v", f"{os.path.abspath(repo_dir)}:/mnt/repo",
                CONFLICT_RESOLUTION_IMAGE,
                "agent", "run",
                "--skill", "conflict-resolution",
                "--cwd", "/mnt/repo",
                "--output-format", "json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Use a timer to enforce the timeout, since we read stdout line
        # by line and cannot use Popen.wait(timeout=) concurrently.
        timed_out = False

        def _kill_on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            proc.kill()

        timer = threading.Timer(timeout_seconds, _kill_on_timeout)
        timer.start()

        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
            proc.wait()
        finally:
            timer.cancel()
            print("::endgroup::", flush=True)

        if timed_out:
            logger.warning("Conflict-resolution agent timed out.")
            return False

        if proc.returncode != 0:
            stderr_output = proc.stderr.read() if proc.stderr else ""
            logger.warning(
                "Conflict-resolution agent exited with code %d: %s",
                proc.returncode,
                (stderr_output or "")[:500],
            )
            return False

        logger.info("Conflict-resolution agent succeeded.")
        return True

    except FileNotFoundError:
        logger.warning(
            "Docker not found. Skipping conflict-resolution agent."
        )
        return False
    except Exception:
        logger.warning(
            "Conflict-resolution agent failed.", exc_info=True,
        )
        return False
