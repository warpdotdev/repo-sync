"""Wrapper around git CLI operations for testability."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from repo_sync.errors import VerboseCalledProcessError


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
            raise VerboseCalledProcessError(
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

    def rebase_continue(self) -> CommandResult:
        """Continue an in-progress rebase after conflicts have been staged.

        Sets GIT_EDITOR=true to prevent an interactive editor from opening
        in non-interactive environments (CI).  The original commit message
        is preserved.
        """
        env = {**os.environ, **self._env_additions, "GIT_EDITOR": "true"}
        result = subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
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

    def get_modify_delete_conflicts(self) -> list[dict[str, str]]:
        """Return modify/delete conflicts detected from the unmerged index.

        Must be called while the cherry-pick (or merge/rebase) is still
        in-progress — i.e., before ``git add -A`` resolves the index.

        Each returned dict has:
          - ``path``: the conflicting file path.
          - ``deleted_by``: ``"ours"`` if our side deleted the file
            (porcelain ``DU``), or ``"theirs"`` if their side deleted it
            (porcelain ``UD``).
        """
        result = self._run(["status", "--porcelain"], check=False)
        conflicts: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]
            path = line[3:]
            if xy == "DU":
                conflicts.append({"path": path, "deleted_by": "ours"})
            elif xy == "UD":
                conflicts.append({"path": path, "deleted_by": "theirs"})
        return conflicts

    def archive_to_dir(self, ref: str, target_dir: str) -> None:
        """Extract the tree at a ref into a directory via git archive.

        Streams the tar output directly into extraction so the full archive
        is never buffered in memory.
        """
        import tarfile
        import logging

        # Skip the LFS smudge filter.  git archive operates on the object
        # store, so LFS pointer files are all we need (and all we want —
        # downloading the real blobs is wasteful and may fail without auth).
        archive_env = {**os.environ, **self._env_additions, "GIT_LFS_SKIP_SMUDGE": "1"}
        proc = subprocess.Popen(
            ["git", "archive", ref],
            cwd=self.repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=archive_env,
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
            raise VerboseCalledProcessError(
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

    def commit_amend_message(
        self, *messages: str, allow_empty: bool = False
    ) -> None:
        """Amend the current commit's message."""
        args = ["commit", "--amend"]
        if allow_empty:
            args.append("--allow-empty")
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

    def remote_remove(self, name: str) -> None:
        """Remove a remote if it exists."""
        self._run(["remote", "remove", name], check=False)

    def remote_url(self, name: str) -> str:
        """Return the configured URL for a remote."""
        return self._run(["remote", "get-url", name]).stdout

    def log_shas(self, ref: str = "HEAD") -> list[str]:
        """Return all commit SHAs reachable from ref (newest first)."""
        result = self._run(["log", "--format=%H", ref])
        if not result.stdout:
            return []
        return result.stdout.splitlines()

    def diff_patch(self, ref_a: str, ref_b: str) -> str:
        """Generate a text diff between two refs.

        The output is decoded with ``errors="replace"`` because diffed
        files may contain non-UTF-8 content.  This is acceptable since
        the patch is only used for human-readable description generation,
        not for applying.
        """
        env = {**os.environ, **self._env_additions} if self._env_additions else None
        result = subprocess.run(
            ["git", "diff", ref_a, ref_b],
            cwd=self.repo_dir,
            capture_output=True,
            env=env,
        )
        if result.returncode != 0:
            raise VerboseCalledProcessError(
                result.returncode,
                ["git", "diff", ref_a, ref_b],
                result.stdout,
                result.stderr,
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def diff_name_only(self, ref_a: str, ref_b: str) -> list[str]:
        """Return changed paths between two refs."""
        env = {**os.environ, **self._env_additions} if self._env_additions else None
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", ref_a, ref_b],
            cwd=self.repo_dir,
            capture_output=True,
            env=env,
        )
        if result.returncode != 0:
            raise VerboseCalledProcessError(
                result.returncode,
                ["git", "diff", "--name-only", "-z", ref_a, ref_b],
                result.stdout,
                result.stderr,
            )
        if not result.stdout:
            return []
        return [
            path
            for path in result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
            if path
        ]

    def lfs_tracked_paths(
        self,
        paths: list[str],
        source_ref: str | None = None,
    ) -> set[str]:
        """Return paths that are configured with the Git LFS filter."""
        if not paths:
            return set()

        args = ["check-attr", "-z"]
        if source_ref is not None:
            args.extend(["--source", source_ref])
        args.extend(["--stdin", "filter"])

        env = {**os.environ, **self._env_additions} if self._env_additions else None
        input_text = "".join(f"{path}\0" for path in paths)
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            input=input_text,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise VerboseCalledProcessError(
                result.returncode,
                ["git", *args],
                result.stdout,
                result.stderr,
            )

        tracked_paths: set[str] = set()
        fields = [field for field in result.stdout.split("\0") if field]
        for index in range(0, len(fields), 3):
            if index + 2 >= len(fields):
                break
            path, attr, value = fields[index:index + 3]
            if attr == "filter" and value == "lfs":
                tracked_paths.add(path)
        return tracked_paths

    def lfs_fetch_paths(self, ref: str, paths: list[str]) -> None:
        """Fetch LFS objects for exact paths at a ref."""
        env = {**os.environ, **self._env_additions} if self._env_additions else None
        for path in paths:
            result = subprocess.run(
                ["git", "cat-file", "--filters", f"{ref}:{path}"],
                cwd=self.repo_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            if result.returncode != 0:
                raise VerboseCalledProcessError(
                    result.returncode,
                    ["git", "cat-file", "--filters", f"{ref}:{path}"],
                    "",
                    result.stderr,
                )

    def lfs_write_path(self, ref: str, path: str, output_path: str) -> None:
        """Write the LFS-smudged content for an exact path at a ref."""
        env = {**os.environ, **self._env_additions} if self._env_additions else None
        command = ["git", "cat-file", "--filters", f"{ref}:{path}"]
        with open(output_path, "wb") as output:
            result = subprocess.run(
                command,
                cwd=self.repo_dir,
                stdout=output,
                stderr=subprocess.PIPE,
                env=env,
            )
        if result.returncode != 0:
            raise VerboseCalledProcessError(
                result.returncode,
                command,
                b"",
                result.stderr,
            )

    def lfs_missing_oids(self, oids: list[str]) -> list[str]:
        """Return LFS object IDs that are not present in the local LFS store."""
        if not oids:
            return []

        git_common_dir = self._run(["rev-parse", "--git-common-dir"]).stdout
        git_common_path = Path(git_common_dir)
        if not git_common_path.is_absolute():
            git_common_path = Path(self.repo_dir) / git_common_path

        missing: list[str] = []
        for oid in oids:
            object_path = (
                git_common_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
            )
            if not object_path.is_file():
                missing.append(oid)
        return missing

    def lfs_push_oids(self, remote: str, oids: list[str]) -> None:
        """Push specific LFS object IDs to a remote."""
        if not oids:
            return

        env = {**os.environ, **self._env_additions} if self._env_additions else None
        input_text = "".join(f"{oid}\n" for oid in oids)
        result = subprocess.run(
            ["git", "lfs", "push", "--object-id", remote, "--stdin"],
            cwd=self.repo_dir,
            input=input_text,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise VerboseCalledProcessError(
                result.returncode,
                ["git", "lfs", "push", "--object-id", remote, "--stdin"],
                result.stdout,
                result.stderr,
            )
