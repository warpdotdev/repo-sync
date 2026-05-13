"""Helpers for detecting Git LFS pointer files in synced trees."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_POINTER_VERSION = "version https://git-lfs.github.com/spec/v1"
_OID_RE = re.compile(r"^oid sha256:([0-9a-f]{64})$")
_SIZE_RE = re.compile(r"^size ([0-9]+)$")
_MAX_POINTER_BYTES = 4096


@dataclass(frozen=True)
class LfsPointer:
    """A Git LFS pointer found in a synced tree."""

    path: str
    oid: str
    size: int


def parse_lfs_pointer(data: bytes, path: str) -> LfsPointer | None:
    """Parse a Git LFS pointer file, returning None for ordinary files."""
    if len(data) > _MAX_POINTER_BYTES:
        return None

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None

    lines = text.splitlines()
    if not lines or lines[0] != _POINTER_VERSION:
        return None

    oid: str | None = None
    size: int | None = None
    for line in lines[1:]:
        oid_match = _OID_RE.match(line)
        if oid_match:
            oid = oid_match.group(1)
            continue

        size_match = _SIZE_RE.match(line)
        if size_match:
            size = int(size_match.group(1))

    if oid is None or size is None:
        return None

    return LfsPointer(path=path, oid=oid, size=size)


def collect_lfs_pointers(
    root: str,
    paths: Iterable[str] | None = None,
) -> list[LfsPointer]:
    """Collect LFS pointers under root, optionally restricted to relative paths."""
    candidates = _candidate_paths(root, paths)
    pointers: list[LfsPointer] = []

    for relpath, fullpath in candidates:
        try:
            if not os.path.isfile(fullpath) or os.path.islink(fullpath):
                continue
            if os.path.getsize(fullpath) > _MAX_POINTER_BYTES:
                continue
            data = Path(fullpath).read_bytes()
        except OSError:
            continue

        pointer = parse_lfs_pointer(data, relpath)
        if pointer is not None:
            pointers.append(pointer)

    return pointers


def _candidate_paths(
    root: str,
    paths: Iterable[str] | None,
) -> list[tuple[str, str]]:
    """Return normalized relative and absolute paths to inspect."""
    root_path = Path(root)
    if paths is not None:
        result: list[tuple[str, str]] = []
        for path in paths:
            relpath = os.path.normpath(path)
            if os.path.isabs(relpath) or relpath == ".." or relpath.startswith("../"):
                continue
            result.append((relpath, str(root_path / relpath)))
        return result

    result = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if not os.path.islink(os.path.join(dirpath, name))
        ]
        for filename in filenames:
            fullpath = os.path.join(dirpath, filename)
            relpath = os.path.relpath(fullpath, root)
            result.append((relpath, fullpath))
    return result
