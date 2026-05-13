"""Tests for git operation helpers."""

from __future__ import annotations

from pathlib import Path

from repo_sync.stack.git_ops import GitOps


def test_lfs_tracked_paths_uses_source_ref(tmp_git_repo: GitOps) -> None:
    repo_dir = Path(tmp_git_repo.repo_dir)
    (repo_dir / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    tmp_git_repo._run(["add", ".gitattributes"])
    tmp_git_repo._run(["commit", "-m", "track bin files"])
    bin_attrs_ref = tmp_git_repo.rev_parse("HEAD")

    (repo_dir / ".gitattributes").write_text("*.dat filter=lfs\n", encoding="utf-8")
    tmp_git_repo._run(["add", ".gitattributes"])
    tmp_git_repo._run(["commit", "-m", "track dat files"])

    assert tmp_git_repo.lfs_tracked_paths(
        ["asset.bin", "asset.dat"],
        source_ref=bin_attrs_ref,
    ) == {"asset.bin"}
    assert tmp_git_repo.lfs_tracked_paths(["asset.bin", "asset.dat"]) == {
        "asset.dat"
    }
