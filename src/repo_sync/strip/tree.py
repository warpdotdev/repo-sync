"""Full-tree stripping logic for repo-sync.

Walks a directory tree, removes ``private/`` directories, deletes files
marked with ``!repo-sync: private-file``, detects symlinks, classifies
files as text or binary, and strips private marker regions from text
files.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import NamedTuple

from repo_sync.strip.detect import is_binary
from repo_sync.strip.markers import (
    MarkerError,
    has_private_file_marker,
    strip_private_regions,
    validate_markers,
)


class StripResult(NamedTuple):
    """Result of a :func:`strip_tree` call."""

    errors: list[str]
    warnings: list[str]


class StrippingError(Exception):
    """Raised when the stripping process encounters a fatal error."""


def remove_private_directories(root: str) -> None:
    """Remove all directories named exactly ``private`` under *root*.

    Walks bottom-up so that nested ``private/`` dirs are handled
    naturally.  Does **not** follow symlinks during the walk.
    """
    for dirpath, dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
        for dname in dirnames:
            if dname == "private":
                full = os.path.join(dirpath, dname)
                # Only remove actual directories, not symlinks.
                # Symlinks named ``private`` are left for the symlink
                # check to catch later.
                if os.path.islink(full):
                    continue
                shutil.rmtree(full)


def _check_symlinks(root: str) -> list[str]:
    """Return errors for symlinks whose targets escape the repo or point into ``private/``.

    Symlinks whose resolved target is within the repo and does not pass
    through a directory named ``private`` are allowed through unchanged.
    """
    errors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                continue

            rel = os.path.relpath(full, root)

            # Skip symlinks that live inside a private/ directory.  In
            # strip mode these are already removed; in validate-only mode
            # they survive but would be removed during real stripping.
            if _path_has_private_component(os.path.dirname(rel)):
                continue

            raw_target = os.readlink(full)

            # Resolve the target relative to the symlink's parent directory.
            if os.path.isabs(raw_target):
                resolved = os.path.normpath(raw_target)
            else:
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(full), raw_target)
                )

            # Check whether the resolved target stays within the repo.
            target_rel = os.path.relpath(resolved, root)
            if target_rel.startswith(".."):
                errors.append(
                    f"{rel}: symlink target escapes the repository root"
                )
                continue

            # Check whether any component of the target path is ``private``.
            if _path_has_private_component(target_rel):
                errors.append(
                    f"{rel}: symlink target resolves into a private/ directory"
                )
    return errors


def _path_has_private_component(relpath: str) -> bool:
    """Return True if any component of *relpath* is exactly ``private``."""
    return "private" in Path(relpath).parts


def strip_tree(
    root: str,
    *,
    validate_only: bool = False,
    paths: list[str] | None = None,
) -> StripResult:
    """Strip private content from the tree rooted at *root*.

    When *validate_only* is True, no files are modified; only validation
    errors are collected and returned.

    When *paths* is provided, only those relative paths (or glob
    patterns) are checked for marker validity.  Symlink checks are
    always performed on the full tree.  Directory removal is skipped
    in validate-only mode.

    Returns a :class:`StripResult` containing errors and warnings.
    Raises ``StrippingError`` wrapping all errors when not in
    validate-only mode and errors are found.

    **Important:** in strip mode, files that are successfully stripped
    are written to disk even if other files have errors.  The caller
    must discard the tree on ``StrippingError``.
    """
    if paths is not None and not validate_only:
        raise ValueError("paths can only be used with validate_only=True")

    errors: list[str] = []
    warnings: list[str] = []

    if not validate_only:
        # Step 1: remove private/ directories before anything else.
        remove_private_directories(root)

    # Step 2: check for symlinks.
    errors.extend(_check_symlinks(root))

    # Step 3: process remaining files.
    if paths is not None:
        file_list = _expand_paths(root, paths)
        if not file_list:
            errors.append(
                f"paths filter matched no files: {paths}"
            )
    else:
        file_list = _collect_files(root)

    for filepath in sorted(file_list):
        if not os.path.isfile(filepath) or os.path.islink(filepath):
            # Symlinks already reported above; skip non-files.
            continue

        rel = os.path.relpath(filepath, root)

        if is_binary(filepath):
            # Binary files are left as-is; no marker stripping.
            continue

        # Attempt UTF-8 decode.  Files that cannot be decoded are treated
        # as binary (skipped), but we log a warning so the user can review.
        try:
            raw = Path(filepath).read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"{rel}: not valid UTF-8, treating as binary")
            continue

        lines = text.splitlines(keepends=True)

        if validate_only:
            errs = validate_markers(lines, filepath=rel)
            errors.extend(errs)
        else:
            if has_private_file_marker(lines):
                # The entire file is private.  Validate markers to catch
                # conflicts with region markers, then delete the file.
                errs = validate_markers(lines, filepath=rel)
                if errs:
                    errors.extend(errs)
                    continue
                os.remove(filepath)
                continue
            try:
                stripped = strip_private_regions(lines, filepath=rel)
            except MarkerError as exc:
                errors.append(str(exc))
                continue
            Path(filepath).write_text("".join(stripped), encoding="utf-8")

    if errors and not validate_only:
        raise StrippingError("\n".join(errors))

    return StripResult(errors, warnings)


def _expand_paths(root: str, patterns: list[str]) -> list[str]:
    """Expand *patterns* relative to *root* using glob matching.

    Each pattern is first tried as a literal path; if that does not
    exist, it is expanded as a glob via ``pathlib.Path.glob()``.
    Returns deduplicated absolute paths.
    """
    root_path = Path(root)
    result: set[str] = set()
    for pattern in patterns:
        literal = root_path / pattern
        if literal.exists() and not literal.is_dir():
            result.add(str(literal))
        else:
            matches = list(root_path.glob(pattern))
            for m in matches:
                if m.is_file() or m.is_symlink():
                    result.add(str(m))
    return sorted(result)


def _collect_files(root: str) -> list[str]:
    """Collect all file paths under *root*, not following symlinks."""
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            files.append(os.path.join(dirpath, fname))
    return files
