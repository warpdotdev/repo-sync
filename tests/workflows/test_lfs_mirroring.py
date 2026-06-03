"""Tests for Git LFS object mirroring during sync."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pytest import LogCaptureFixture

from repo_sync.stack.git_ops import GitOps
from repo_sync.workflows.create_sync_prs import PermanentSyncError, _mirror_lfs_objects


OID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PRIVATE_OID = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def _pointer(oid: str) -> str:
    """Return valid Git LFS pointer file content."""
    return (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        "size 1234\n"
    )


def test_mirror_lfs_objects_pushes_changed_pointer_oids(
    tmp_path: Path,
    caplog: LogCaptureFixture,
) -> None:
    (tmp_path / "asset.bin").write_text(_pointer(OID), encoding="utf-8")

    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)
    peer_git.remote_url.return_value = "https://github.com/org/peer.git"
    attributes_git.lfs_tracked_paths.return_value = {"asset.bin"}
    source_git.lfs_missing_oids.return_value = []
    caplog.set_level(logging.INFO, logger="repo_sync.workflows.create_sync_prs")

    with patch("repo_sync.workflows.create_sync_prs.os.getpid", return_value=42):
        _mirror_lfs_objects(
            source_git=source_git,
            peer_git=peer_git,
            source_ref="abc123",
            snapshot_dir=str(tmp_path),
            changed_paths=["asset.bin"],
            attributes_git=attributes_git,
            attributes_ref="attrs-ref",
        )

    remote = "repo_sync_lfs_target_42"
    attributes_git.lfs_tracked_paths.assert_called_once_with(
        ["asset.bin"],
        source_ref="attrs-ref",
    )
    source_git.lfs_fetch_paths.assert_called_once_with(
        "abc123",
        ["asset.bin"],
        expected_oids={"asset.bin": OID},
    )
    source_git.lfs_missing_oids.assert_called_once_with([OID])
    peer_git.remote_url.assert_called_once_with("origin")
    source_git.remote_add_or_update.assert_called_once_with(
        remote,
        "https://github.com/org/peer.git",
    )
    source_git.lfs_push_oids.assert_called_once_with(remote, [OID])
    source_git.remote_remove.assert_called_once_with(remote)
    assert OID in caplog.text
    assert "asset.bin" in caplog.text


def test_mirror_lfs_objects_skips_when_no_changed_pointer(tmp_path: Path) -> None:
    (tmp_path / "ordinary.txt").write_text("hello\n", encoding="utf-8")
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)

    _mirror_lfs_objects(
        source_git=source_git,
        peer_git=peer_git,
        source_ref="abc123",
        snapshot_dir=str(tmp_path),
        changed_paths=["ordinary.txt"],
        attributes_git=attributes_git,
        attributes_ref="attrs-ref",
    )

    attributes_git.lfs_tracked_paths.assert_not_called()
    source_git.lfs_fetch_paths.assert_not_called()
    source_git.lfs_missing_oids.assert_not_called()
    peer_git.remote_url.assert_not_called()
    source_git.remote_add_or_update.assert_not_called()
    source_git.lfs_push_oids.assert_not_called()
    source_git.remote_remove.assert_not_called()


def test_mirror_lfs_objects_does_not_push_private_stripped_pointer(
    tmp_path: Path,
) -> None:
    source_tree = tmp_path / "source"
    source_tree.joinpath("private").mkdir(parents=True)
    source_tree.joinpath("private/secret.bin").write_text(
        _pointer(PRIVATE_OID),
        encoding="utf-8",
    )

    snapshot = tmp_path / "stripped-snapshot"
    snapshot.mkdir()
    snapshot.joinpath("asset.bin").write_text(_pointer(OID), encoding="utf-8")
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)
    peer_git.remote_url.return_value = "https://github.com/org/public.git"
    attributes_git.lfs_tracked_paths.return_value = {"asset.bin"}
    source_git.lfs_missing_oids.return_value = []

    with patch("repo_sync.workflows.create_sync_prs.os.getpid", return_value=42):
        _mirror_lfs_objects(
            source_git=source_git,
            peer_git=peer_git,
            source_ref="abc123",
            snapshot_dir=str(snapshot),
            changed_paths=["asset.bin", "private/secret.bin"],
            attributes_git=attributes_git,
            attributes_ref="attrs-ref",
        )

    remote = "repo_sync_lfs_target_42"
    source_git.lfs_fetch_paths.assert_called_once_with(
        "abc123",
        ["asset.bin"],
        expected_oids={"asset.bin": OID},
    )
    source_git.lfs_push_oids.assert_called_once_with(remote, [OID])
    assert PRIVATE_OID not in source_git.lfs_push_oids.call_args.args[1]


def test_mirror_lfs_objects_skips_pointer_shaped_non_lfs_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "looks-like-pointer.txt").write_text(
        _pointer(PRIVATE_OID),
        encoding="utf-8",
    )
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)
    attributes_git.lfs_tracked_paths.return_value = set()

    _mirror_lfs_objects(
        source_git=source_git,
        peer_git=peer_git,
        source_ref="abc123",
        snapshot_dir=str(tmp_path),
        changed_paths=["looks-like-pointer.txt"],
        attributes_git=attributes_git,
        attributes_ref="attrs-ref",
    )

    attributes_git.lfs_tracked_paths.assert_called_once_with(
        ["looks-like-pointer.txt"],
        source_ref="attrs-ref",
    )
    source_git.lfs_fetch_paths.assert_not_called()
    source_git.lfs_missing_oids.assert_not_called()
    source_git.lfs_push_oids.assert_not_called()


def test_mirror_lfs_objects_fails_when_fetched_object_is_missing(
    tmp_path: Path,
) -> None:
    (tmp_path / "asset.bin").write_text(_pointer(OID), encoding="utf-8")
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)
    attributes_git.lfs_tracked_paths.return_value = {"asset.bin"}
    source_git.lfs_missing_oids.return_value = [OID]

    with pytest.raises(PermanentSyncError):
        _mirror_lfs_objects(
            source_git=source_git,
            peer_git=peer_git,
            source_ref="abc123",
            snapshot_dir=str(tmp_path),
            changed_paths=["asset.bin"],
            attributes_git=attributes_git,
            attributes_ref="attrs-ref",
        )

    source_git.lfs_fetch_paths.assert_called_once_with(
        "abc123",
        ["asset.bin"],
        expected_oids={"asset.bin": OID},
    )
    source_git.lfs_push_oids.assert_not_called()


def test_mirror_lfs_objects_fails_before_push_for_lfs_payload_markers(
    tmp_path: Path,
) -> None:
    (tmp_path / "asset.txt").write_text(_pointer(OID), encoding="utf-8")
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)
    attributes_git.lfs_tracked_paths.return_value = {"asset.txt"}
    source_git.lfs_missing_oids.return_value = []

    def write_payload(
        _ref: str,
        _path: str,
        output_path: str,
        expected_oid: str | None = None,
    ) -> None:
        Path(output_path).write_text(
            "public\n"
            "# !repo-sync: private-start\n"
            "secret\n"
            "# !repo-sync: private-end\n",
            encoding="utf-8",
        )

    source_git.lfs_write_path.side_effect = write_payload

    with pytest.raises(PermanentSyncError, match="asset.txt"):
        _mirror_lfs_objects(
            source_git=source_git,
            peer_git=peer_git,
            source_ref="abc123",
            snapshot_dir=str(tmp_path),
            changed_paths=["asset.txt"],
            attributes_git=attributes_git,
            attributes_ref="attrs-ref",
            validate_payload_markers=True,
        )

    source_git.lfs_fetch_paths.assert_called_once_with(
        "abc123",
        ["asset.txt"],
        expected_oids={"asset.txt": OID},
    )
    source_git.lfs_write_path.assert_called_once()
    peer_git.remote_url.assert_not_called()
    source_git.remote_add_or_update.assert_not_called()
    source_git.lfs_push_oids.assert_not_called()


def test_mirror_lfs_objects_fetches_exact_comma_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "asset,with-comma.bin").write_text(
        _pointer(OID),
        encoding="utf-8",
    )
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)
    peer_git.remote_url.return_value = "https://github.com/org/peer.git"
    attributes_git.lfs_tracked_paths.return_value = {"asset,with-comma.bin"}
    source_git.lfs_missing_oids.return_value = []

    with patch("repo_sync.workflows.create_sync_prs.os.getpid", return_value=42):
        _mirror_lfs_objects(
            source_git=source_git,
            peer_git=peer_git,
            source_ref="abc123",
            snapshot_dir=str(tmp_path),
            changed_paths=["asset,with-comma.bin"],
            attributes_git=attributes_git,
            attributes_ref="attrs-ref",
        )

    source_git.lfs_fetch_paths.assert_called_once_with(
        "abc123",
        ["asset,with-comma.bin"],
        expected_oids={"asset,with-comma.bin": OID},
    )


def test_mirror_lfs_objects_scans_all_pointers_when_attributes_change(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitattributes").write_text(
        "*.bin filter=lfs\n",
        encoding="utf-8",
    )
    (tmp_path / "existing.bin").write_text(_pointer(OID), encoding="utf-8")
    (tmp_path / "ordinary.txt").write_text("hello\n", encoding="utf-8")
    source_git = MagicMock(spec=GitOps)
    peer_git = MagicMock(spec=GitOps)
    attributes_git = MagicMock(spec=GitOps)
    peer_git.remote_url.return_value = "https://github.com/org/peer.git"
    attributes_git.lfs_tracked_paths.return_value = {"existing.bin"}
    source_git.lfs_missing_oids.return_value = []

    with patch("repo_sync.workflows.create_sync_prs.os.getpid", return_value=42):
        _mirror_lfs_objects(
            source_git=source_git,
            peer_git=peer_git,
            source_ref="abc123",
            snapshot_dir=str(tmp_path),
            changed_paths=[".gitattributes"],
            attributes_git=attributes_git,
            attributes_ref="attrs-ref",
        )

    attributes_git.lfs_tracked_paths.assert_called_once_with(
        ["existing.bin"],
        source_ref="attrs-ref",
    )
    source_git.lfs_fetch_paths.assert_called_once_with(
        "abc123",
        ["existing.bin"],
        expected_oids={"existing.bin": OID},
    )
