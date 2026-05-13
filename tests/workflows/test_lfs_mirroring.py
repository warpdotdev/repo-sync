"""Tests for Git LFS object mirroring during sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from repo_sync.stack.git_ops import GitOps
from repo_sync.workflows.create_sync_prs import _mirror_lfs_objects


OID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def test_mirror_lfs_objects_pushes_changed_pointer_oids(tmp_path: Path) -> None:
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{OID}\n"
        "size 1234\n"
    )
    (tmp_path / "asset.bin").write_text(pointer, encoding="utf-8")

    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    peer_git.remote_url.return_value = "https://github.com/org/peer.git"

    with patch("repo_sync.workflows.create_sync_prs.os.getpid", return_value=42):
        _mirror_lfs_objects(
            source_git=source_git,
            peer_git=peer_git,
            source_ref="abc123",
            snapshot_dir=str(tmp_path),
            changed_paths=["asset.bin"],
        )

    remote = "repo_sync_lfs_target_42"
    source_git.lfs_fetch_ref.assert_called_once_with("origin", "abc123")
    peer_git.remote_url.assert_called_once_with("origin")
    source_git.remote_add_or_update.assert_called_once_with(
        remote,
        "https://github.com/org/peer.git",
    )
    source_git.lfs_push_oids.assert_called_once_with(remote, [OID])
    source_git.remote_remove.assert_called_once_with(remote)


def test_mirror_lfs_objects_skips_when_no_changed_pointer(tmp_path: Path) -> None:
    (tmp_path / "ordinary.txt").write_text("hello\n", encoding="utf-8")
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)

    _mirror_lfs_objects(
        source_git=source_git,
        peer_git=peer_git,
        source_ref="abc123",
        snapshot_dir=str(tmp_path),
        changed_paths=["ordinary.txt"],
    )

    source_git.lfs_fetch_ref.assert_not_called()
    peer_git.remote_url.assert_not_called()
    source_git.remote_add_or_update.assert_not_called()
    source_git.lfs_push_oids.assert_not_called()
    source_git.remote_remove.assert_not_called()
