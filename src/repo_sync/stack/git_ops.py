"""Wrapper around git CLI operations for testability."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass
class CommandResult:
    """Result of a shell command execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        """Return True if the command exited with code 0."""
        return self.returncode == 0


class GitOps:
    """Encapsulates git CLI operations so they can be replaced in tests."""

    def __init__(self, repo_dir: str, env: dict[str, str] | None = None) -> None:
        self.repo_dir = repo_dir
        # Additional environment variables to merge with os.environ.
        self._env_additions = env or {}

    def _run(self, args: list[str], check: bool = True) -> CommandResult:
        """Run a git command and return the result."""
        env = {**os.environ, **self._env_additions} if self._env_additions else None
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, ["git", *args], result.stdout, result.stderr
            )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
        )

    def rev_parse(self, ref: str) -> str:
        """Resolve a ref to a full SHA."""
        return self._run(["rev-parse", ref]).stdout

    def short_sha(self, ref: str) -> str:
        """Resolve a ref to a short SHA."""
        return self._run(["rev-parse", "--short", ref]).stdout

    def branch_exists(self, branch: str) -> bool:
        """Check if a ref resolves locally (branch, tag, or other local ref)."""
        result = self._run(["rev-parse", "--verify", branch], check=False)
        return result.success

    def create_branch(self, branch: str, start_point: str) -> None:
        """Create a new branch at the given start point."""
        self._run(["checkout", "-b", branch, start_point])

    def checkout(self, ref: str) -> None:
        """Check out a ref."""
        self._run(["checkout", ref])

    def push(
        self,
        remote: str,
        refspec: str,
        force: bool = False,
        force_with_lease: bool = False,
    ) -> None:
        """Push a refspec to a remote."""
        args = ["push", remote, refspec]
        if force_with_lease:
            args.append("--force-with-lease")
        elif force:
            args.append("--force")
        self._run(args)

    def commit_message(self, ref: str) -> str:
        """Get the full commit message for a ref."""
        return self._run(["log", "-1", "--format=%B", ref]).stdout

    def commit_author_name(self, ref: str) -> str:
        """Get the display name of a commit's author (e.g. 'Alice Smith').

        Note: this returns the git author name, NOT a GitHub login.  Callers
        that need a GitHub login must resolve it separately via the GitHub API.
        """
        return self._run(["log", "-1", "--format=%an", ref]).stdout

    def commit_author_email(self, ref: str) -> str:
        """Get the author email of a commit."""
        return self._run(["log", "-1", "--format=%ae", ref]).stdout

    def rebase_onto(
        self, new_base: str, old_base: str, branch: str
    ) -> CommandResult:
        """Run git rebase --onto and return the result (may fail on conflicts)."""
        return self._run(
            ["rebase", "--onto", new_base, old_base, branch], check=False
        )

    def rebase_abort(self) -> None:
        """Abort an in-progress rebase."""
        self._run(["rebase", "--abort"], check=False)

    def tag(self, name: str, ref: str, force: bool = False) -> None:
        """Create or update a tag."""
        args = ["tag", name, ref]
        if force:
            args.insert(1, "-f")
        self._run(args)

    def tag_target(self, tag_name: str) -> str | None:
        """Get the commit SHA a tag points to, or None if the tag doesn't exist."""
        result = self._run(["rev-parse", f"refs/tags/{tag_name}"], check=False)
        if result.success:
            return result.stdout
        return None

    def fetch(self, remote: str, refspec: str = "") -> None:
        """Fetch from a remote."""
        args = ["fetch", remote]
        if refspec:
            args.append(refspec)
        self._run(args)

    def current_branch(self) -> str:
        """Get the current branch name."""
        return self._run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout

    def log_oneline(self, range_spec: str) -> list[str]:
        """Return a list of commit SHAs in the given range (oldest first)."""
        result = self._run(["log", "--format=%H", "--reverse", range_spec])
        if not result.stdout:
            return []
        return result.stdout.splitlines()

    def diff_is_empty(self, ref_a: str, ref_b: str) -> bool:
        """Check if two refs have identical trees."""
        result = self._run(["diff", "--quiet", ref_a, ref_b], check=False)
        return result.success

    def conflicts_exist(self) -> bool:
        """Check if there are unmerged paths (conflict markers)."""
        return bool(self.conflicting_files())

    def conflicting_files(self) -> list[str]:
        """Return the list of files with unresolved merge conflicts."""
        result = self._run(
            ["diff", "--name-only", "--diff-filter=U"], check=False
        )
        if result.stdout.strip():
            return result.stdout.strip().splitlines()
        return []

    def archive_to_dir(self, ref: str, target_dir: str) -> None:
        """Extract the tree at a ref into a directory via git archive.

        Streams the tar output directly into extraction so the full archive
        is never buffered in memory.
        """
        import tarfile
        import logging

        env = {**os.environ, **self._env_additions} if self._env_additions else None
        proc = subprocess.Popen(
            ["git", "archive", ref],
            cwd=self.repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|*") as tar:
                tar.extractall(path=target_dir)
        except tarfile.ReadError:
            # If git archive failed, the stream may be empty or truncated.
            # Fall through to the returncode check below.
            pass
        finally:
            proc.stdout.close()

        stderr = proc.stderr.read()
        proc.stderr.close()
        proc.wait()

        if proc.returncode != 0:
            logger = logging.getLogger(__name__)
            logger.error(
                "git archive %s failed (exit %d): %s",
                ref, proc.returncode, stderr.decode(errors="replace").strip(),
            )
            raise subprocess.CalledProcessError(
                proc.returncode, ["git", "archive", ref],
                b"", stderr,
            )

    def checkout_force_branch(
        self, branch: str, start_point: str | None = None
    ) -> None:
        """Force-create a branch (git checkout -B)."""
        args = ["checkout", "-B", branch]
        if start_point:
            args.append(start_point)
        self._run(args)

    def add_all(self) -> None:
        """Stage all changes (git add -A)."""
        self._run(["add", "-A"])

    def commit(
        self,
        message: str,
        allow_empty: bool = False,
        trailers: list[str] | None = None,
    ) -> None:
        """Create a commit with the given message."""
        args = ["commit", "-m", message]
        for trailer in (trailers or []):
            args.extend(["-m", trailer])
        if allow_empty:
            args.append("--allow-empty")
        self._run(args)

    def commit_amend_message(self, *messages: str) -> None:
        """Amend the current commit's message."""
        args = ["commit", "--amend"]
        for msg in messages:
            args.extend(["-m", msg])
        self._run(args)

    def rm_tracked_files(self) -> None:
        """Remove all tracked files (git rm -rf --quiet .)."""
        self._run(["rm", "-rf", "--quiet", "."], check=False)

    def cherry_pick(
        self, ref: str, allow_empty: bool = False, x: bool = False
    ) -> CommandResult:
        """Cherry-pick a commit.  Returns the result (may fail)."""
        args = ["cherry-pick", ref]
        if allow_empty:
            args.append("--allow-empty")
        if x:
            args.append("-x")
        return self._run(args, check=False)

    def cherry_pick_abort(self) -> None:
        """Abort a cherry-pick in progress."""
        self._run(["cherry-pick", "--abort"], check=False)

    def remote_add_or_update(self, name: str, url: str) -> None:
        """Add a remote, or update its URL if it already exists."""
        result = self._run(["remote", "get-url", name], check=False)
        if result.success:
            self._run(["remote", "set-url", name, url])
        else:
            self._run(["remote", "add", name, url])

    def log_shas(self, ref: str = "HEAD") -> list[str]:
        """Return all commit SHAs reachable from ref (newest first)."""
        result = self._run(["log", "--format=%H", ref])
        if not result.stdout:
            return []
        return result.stdout.splitlines()

    def diff_binary_patch(self, ref_a: str, ref_b: str) -> str:
        """Generate a binary diff patch between two refs."""
        return self._run(["diff", "--binary", ref_a, ref_b]).stdout
