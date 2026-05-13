"""Tests for Git LFS pointer detection helpers."""

from __future__ import annotations

from pathlib import Path

from repo_sync.stack.lfs import LfsPointer, collect_lfs_pointers, parse_lfs_pointer


OID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _pointer(oid: str = OID, size: int = 1234) -> bytes:
    """Return valid Git LFS pointer file content."""
    return (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {size}\n"
    ).encode("utf-8")


def test_parse_lfs_pointer() -> None:
    pointer = parse_lfs_pointer(_pointer(), "asset.bin")

    assert pointer == LfsPointer(path="asset.bin", oid=OID, size=1234)


def test_parse_lfs_pointer_rejects_ordinary_file() -> None:
    assert parse_lfs_pointer(b"hello\n", "hello.txt") is None


def test_parse_lfs_pointer_rejects_invalid_oid() -> None:
    data = (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:not-a-sha\n"
        "size 1234\n"
    ).encode("utf-8")

    assert parse_lfs_pointer(data, "asset.bin") is None


def test_collect_lfs_pointers_filters_to_changed_paths(tmp_path: Path) -> None:
    (tmp_path / "asset.bin").write_bytes(_pointer())
    (tmp_path / "unchanged.bin").write_bytes(_pointer("f" * 64))
    (tmp_path / "ordinary.txt").write_text("hello\n", encoding="utf-8")

    pointers = collect_lfs_pointers(
        str(tmp_path),
        ["asset.bin", "ordinary.txt", "deleted.bin"],
    )

    assert pointers == [LfsPointer(path="asset.bin", oid=OID, size=1234)]
