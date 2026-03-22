"""Shared test fixtures and helpers."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from repo_sync.stack.git_ops import GitOps


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> GitOps:
    """Create a temporary git repo with an initial commit and return a GitOps instance."""
    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir)
    _run_git(repo_dir, ["init", "--initial-branch=main"])
    _run_git(repo_dir, ["config", "user.email", "test@example.com"])
    _run_git(repo_dir, ["config", "user.name", "Test User"])

    # Create an initial commit so main exists.
    init_file = os.path.join(repo_dir, "README.md")
    with open(init_file, "w") as f:
        f.write("# test repo\n")
    _run_git(repo_dir, ["add", "."])
    _run_git(repo_dir, ["commit", "-m", "initial commit"])

    return GitOps(repo_dir)


@pytest.fixture
def tmp_git_repo_pair(tmp_path: Path) -> tuple[GitOps, GitOps]:
    """Create a pair of git repos (source + target) where target is a bare clone.

    Returns (source_git, target_git) where target acts as the remote.
    The source repo has 'target' configured as a remote pointing to the bare repo.
    """
    # Create the bare "remote" repo.
    bare_dir = str(tmp_path / "target.git")
    os.makedirs(bare_dir)
    _run_git(bare_dir, ["init", "--bare", "--initial-branch=main"])

    # Create the source repo.
    source_dir = str(tmp_path / "source")
    os.makedirs(source_dir)
    _run_git(source_dir, ["init", "--initial-branch=main"])
    _run_git(source_dir, ["config", "user.email", "test@example.com"])
    _run_git(source_dir, ["config", "user.name", "Test User"])
    _run_git(source_dir, ["remote", "add", "origin", bare_dir])

    # Initial commit and push.
    init_file = os.path.join(source_dir, "README.md")
    with open(init_file, "w") as f:
        f.write("# test repo\n")
    _run_git(source_dir, ["add", "."])
    _run_git(source_dir, ["commit", "-m", "initial commit"])
    _run_git(source_dir, ["push", "origin", "main"])

    return GitOps(source_dir), GitOps(bare_dir)


def make_commit(git: GitOps, filename: str, content: str, message: str) -> str:
    """Create a file, commit it, and return the commit SHA."""
    filepath = os.path.join(git.repo_dir, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    git._run(["add", filename])
    git._run(["commit", "-m", message])
    return git.rev_parse("HEAD")


def _run_git(cwd: str, args: list[str]) -> None:
    """Helper to run a git command in a directory."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
