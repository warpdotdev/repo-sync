"""Tests for git operation helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_lfs_fetch_paths_uses_cat_file_filters_for_exact_paths(
    tmp_git_repo: GitOps,
) -> None:
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""

    with patch("repo_sync.stack.git_ops.subprocess.run", return_value=result) as run:
        tmp_git_repo.lfs_fetch_paths("abc123", ["asset,with-comma.bin"])

    run.assert_called_once()
    assert run.call_args.args[0] == [
        "git",
        "cat-file",
        "--filters",
        "abc123:asset,with-comma.bin",
    ]
    assert run.call_args.kwargs["env"]["GIT_ATTR_SOURCE"] == "abc123"


def test_lfs_write_path_uses_attributes_from_ref(tmp_git_repo: GitOps) -> None:
    repo_dir = Path(tmp_git_repo.repo_dir)
    tmp_git_repo._run(["lfs", "install", "--local"])
    tmp_git_repo._run(["lfs", "track", "*.txt"])
    (repo_dir / "asset.txt").write_text("payload\n", encoding="utf-8")
    tmp_git_repo._run(["add", ".gitattributes", "asset.txt"])
    tmp_git_repo._run(["commit", "-m", "add lfs asset"])
    lfs_ref = tmp_git_repo.rev_parse("HEAD")

    (repo_dir / ".gitattributes").unlink()
    tmp_git_repo._run(["add", ".gitattributes"])
    tmp_git_repo._run(["commit", "-m", "stop tracking txt files"])

    output_path = repo_dir / "payload.out"
    tmp_git_repo.lfs_write_path(lfs_ref, "asset.txt", str(output_path))

    assert output_path.read_text(encoding="utf-8") == "payload\n"


def test_lfs_write_path_uses_cat_file_filters_for_exact_path(
    tmp_git_repo: GitOps,
    tmp_path: Path,
) -> None:
    result = MagicMock()
    result.returncode = 0
    result.stderr = b""
    output_path = tmp_path / "payload"

    with patch("repo_sync.stack.git_ops.subprocess.run", return_value=result) as run:
        tmp_git_repo.lfs_write_path(
            "abc123",
            "asset,with-comma.bin",
            str(output_path),
        )

    run.assert_called_once()
    assert run.call_args.args[0] == [
        "git",
        "cat-file",
        "--filters",
        "abc123:asset,with-comma.bin",
    ]
    assert run.call_args.kwargs["env"]["GIT_ATTR_SOURCE"] == "abc123"
