"""Tests for the post-strip fixup script feature."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from repo_sync.stack.git_ops import GitOps
from repo_sync.workflows.create_sync_prs import (
    PermanentSyncError,
    _create_diff_repo,
    _run_fixup_script,
)


def _write(root: Path, relpath: str, content: str) -> Path:
    """Create a file under *root* at *relpath* with the given content."""
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _make_script(path: Path, body: str) -> str:
    """Write a shell script and make it executable.  Returns the path as a string."""
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# ---------------------------------------------------------------------------
# _run_fixup_script unit tests
# ---------------------------------------------------------------------------


class TestRunFixupScript:
    """Direct tests for the _run_fixup_script helper."""

    def test_success(self, tmp_path: Path) -> None:
        """A script that exits 0 returns True."""
        script = _make_script(tmp_path / "ok.sh", "exit 0")
        assert _run_fixup_script(script, str(tmp_path)) is True

    def test_failure(self, tmp_path: Path) -> None:
        """A script that exits non-zero returns False."""
        script = _make_script(tmp_path / "fail.sh", "exit 1")
        assert _run_fixup_script(script, str(tmp_path)) is False

    def test_missing_script(self, tmp_path: Path) -> None:
        """A non-existent script path returns False."""
        assert _run_fixup_script("/no/such/script.sh", str(tmp_path)) is False

    def test_receives_working_dir_as_argument(self, tmp_path: Path) -> None:
        """The script receives the working directory as its first argument."""
        marker = tmp_path / "marker.txt"
        script = _make_script(
            tmp_path / "check_arg.sh",
            f'echo "$1" > {marker}',
        )
        work_dir = str(tmp_path / "work")
        os.makedirs(work_dir)
        _run_fixup_script(script, work_dir)
        assert marker.read_text().strip() == work_dir

    def test_script_can_modify_working_dir(self, tmp_path: Path) -> None:
        """The script can create/modify files in the working directory."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        script = _make_script(
            tmp_path / "modify.sh",
            'echo "modified" > "$1/new_file.txt"',
        )
        assert _run_fixup_script(script, str(work_dir)) is True
        assert (work_dir / "new_file.txt").read_text().strip() == "modified"


# ---------------------------------------------------------------------------
# _create_diff_repo integration tests
# ---------------------------------------------------------------------------


def _init_repo(path: str) -> GitOps:
    """Initialise a git repo with user config and return a GitOps instance."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", path], check=True, capture_output=True)
    git = GitOps(path)
    git._run(["config", "user.name", "Test User"])
    git._run(["config", "user.email", "test@example.com"])
    return git


def _commit_file(git: GitOps, relpath: str, content: str, message: str) -> str:
    """Write a file, commit it, and return the SHA."""
    filepath = os.path.join(git.repo_dir, relpath)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    git._run(["add", relpath])
    git._run(["commit", "-m", message])
    return git.rev_parse("HEAD")


class TestCreateDiffRepoWithFixup:
    """Integration tests: fixup script modifying snapshots affects the diff."""

    def test_fixup_modifies_diff(self, tmp_path: Path) -> None:
        """A fixup script that adds a file to both snapshots produces a consistent diff."""
        source_git = _init_repo(str(tmp_path / "source"))

        # Parent commit: one file.
        _commit_file(source_git, "hello.txt", "hello\n", "parent")
        # Child commit: add a second file.
        child_sha = _commit_file(source_git, "world.txt", "world\n", "child")
        short = child_sha[:7]

        # Fixup script: append a line to hello.txt.
        script = _make_script(
            tmp_path / "fixup.sh",
            'echo "fixup-ran" >> "$1/hello.txt"',
        )

        result = _create_diff_repo(
            source_git, child_sha, short,
            slack_webhook_url="",
            source_repo="test/repo",
            fixup_script=script,
        )
        assert result is not None
        snapshot_dir, prev_snapshot_dir, diff_repo, patch_file, diff_commit = result

        try:
            # Both snapshots should have the fixup modification.
            assert "fixup-ran" in Path(snapshot_dir).joinpath("hello.txt").read_text()
            assert "fixup-ran" in Path(prev_snapshot_dir).joinpath("hello.txt").read_text()

            # The diff should only contain the world.txt addition (not the
            # fixup change to hello.txt, since it was applied to both sides).
            diff_git = GitOps(diff_repo)
            patch = diff_git.diff_patch("HEAD~1", "HEAD")
            assert "world.txt" in patch
            assert "fixup-ran" not in patch
        finally:
            import shutil
            for d in (snapshot_dir, prev_snapshot_dir, diff_repo):
                shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(patch_file):
                os.remove(patch_file)

    def test_fixup_failure_raises_permanent_error(self, tmp_path: Path) -> None:
        """A fixup script that fails raises PermanentSyncError."""
        source_git = _init_repo(str(tmp_path / "source"))
        _commit_file(source_git, "hello.txt", "hello\n", "parent")
        child_sha = _commit_file(source_git, "world.txt", "world\n", "child")
        short = child_sha[:7]

        script = _make_script(tmp_path / "bad.sh", "exit 1")

        with pytest.raises(PermanentSyncError, match="Fixup script failed"):
            _create_diff_repo(
                source_git, child_sha, short,
                slack_webhook_url="",
                source_repo="test/repo",
                fixup_script=script,
            )

    def test_no_fixup_script_works_as_before(self, tmp_path: Path) -> None:
        """Omitting the fixup script produces the same behavior as before."""
        source_git = _init_repo(str(tmp_path / "source"))
        _commit_file(source_git, "hello.txt", "hello\n", "parent")
        child_sha = _commit_file(source_git, "world.txt", "world\n", "child")
        short = child_sha[:7]

        result = _create_diff_repo(
            source_git, child_sha, short,
            slack_webhook_url="",
            source_repo="test/repo",
        )
        assert result is not None
        snapshot_dir, prev_snapshot_dir, diff_repo, patch_file, _ = result

        try:
            diff_git = GitOps(diff_repo)
            patch = diff_git.diff_patch("HEAD~1", "HEAD")
            assert "world.txt" in patch
        finally:
            import shutil
            for d in (snapshot_dir, prev_snapshot_dir, diff_repo):
                shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(patch_file):
                os.remove(patch_file)
