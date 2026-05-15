"""Tests for validating repo-sync markers in Git LFS payloads."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from repo_sync.strip.lfs import validate_lfs_payload_file, validate_lfs_payloads


def test_validate_lfs_payload_file_rejects_private_region_markers(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text(
        "public\n"
        "# !repo-sync: private-start\n"
        "secret\n"
        "# !repo-sync: private-end\n",
        encoding="utf-8",
    )

    errors = validate_lfs_payload_file(str(payload), filepath="asset.txt")

    assert len(errors) == 1
    assert "asset.txt" in errors[0]
    assert "private region markers" in errors[0]


def test_validate_lfs_payload_file_allows_binary_marker_bytes(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"\x00!repo-sync: private-file")

    assert validate_lfs_payload_file(str(payload), filepath="asset.bin") == []


def test_validate_lfs_payload_file_allows_marker_substrings(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text(
        "Docs mention !repo-sync: private-start/end as prose.\n",
        encoding="utf-8",
    )

    assert validate_lfs_payload_file(str(payload), filepath="asset.txt") == []


def test_validate_lfs_payloads_materializes_lfs_paths(
    tmp_path: Path,
) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command[:4] == ["git", "lfs", "ls-files", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"files":[{"name":"asset.txt"}]}',
                stderr="",
            )
        if command[:3] == ["git", "cat-file", "--filters"]:
            assert kwargs["env"]["GIT_ATTR_SOURCE"] == "HEAD"
            stdout = kwargs["stdout"]
            stdout.write(
                b"public\n"
                b"# !repo-sync: private-start\n"
                b"secret\n"
                b"# !repo-sync: private-end\n"
            )
            return subprocess.CompletedProcess(command, 0, stderr=b"")
        raise AssertionError(f"unexpected command: {command}")

    with patch("repo_sync.strip.lfs.subprocess.run", side_effect=run):
        result = validate_lfs_payloads(str(tmp_path))

    assert len(result.errors) == 1
    assert "asset.txt" in result.errors[0]


def test_validate_lfs_payloads_fails_when_filters_return_pointer(
    tmp_path: Path,
) -> None:
    oid = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command[:4] == ["git", "lfs", "ls-files", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"files":[{"name":"asset.bin",'
                    f'"oid":"{oid}"'
                    "}]}"
                ),
                stderr="",
            )
        if command[:3] == ["git", "cat-file", "--filters"]:
            stdout = kwargs["stdout"]
            stdout.write(
                b"version https://git-lfs.github.com/spec/v1\n"
                + f"oid sha256:{oid}\n".encode()
                + b"size 1234\n"
            )
            return subprocess.CompletedProcess(command, 0, stderr=b"")
        raise AssertionError(f"unexpected command: {command}")

    with patch("repo_sync.strip.lfs.subprocess.run", side_effect=run):
        result = validate_lfs_payloads(str(tmp_path))

    assert len(result.errors) == 1
    assert "asset.bin" in result.errors[0]
    assert "git lfs install --local" in result.errors[0]
