"""Wrapper around git CLI operations for testability."""

from __future__ import annotations

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
        self.env = env

    def _run(self, args: list[str], check: bool = True) -> CommandResult:
        """Run a git command and return the result."""
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            env=self.env,
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
        """Check if a branch exists locally or as a remote ref."""
        result = self._run(["rev-parse", "--verify", branch], check=False)
        return result.success

    def remote_branch_exists(self, remote: str, branch: str) -> bool:
        """Check if a remote branch exists via ls-remote."""
        result = self._run(["ls-remote", "--heads", remote, branch])
        return bool(result.stdout.strip())

    def create_branch(self, branch: str, start_point: str) -> None:
        """Create a new branch at the given start point."""
        self._run(["checkout", "-b", branch, start_point])

    def checkout(self, ref: str) -> None:
        """Check out a ref."""
        self._run(["checkout", ref])

    def push(self, remote: str, refspec: str, force: bool = False) -> None:
        """Push a refspec to a remote."""
        args = ["push", remote, refspec]
        if force:
            args.insert(1, "--force")
        self._run(args)

    def commit_message(self, ref: str) -> str:
        """Get the full commit message for a ref."""
        return self._run(["log", "-1", "--format=%B", ref]).stdout

    def commit_author(self, ref: str) -> str:
        """Get the author of a commit (GitHub username-compatible format)."""
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
        result = self._run(["rev-parse", tag_name], check=False)
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
