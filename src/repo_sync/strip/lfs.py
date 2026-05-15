"""Git LFS payload validation for repo-sync markers."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

from repo_sync.strip.detect import is_binary
from repo_sync.strip.markers import (
    MarkerError,
    has_private_file_marker,
    strip_private_regions,
    validate_markers,
)
from repo_sync.strip.tree import StripResult, _expand_paths


def validate_lfs_payload_file(payload_path: str, *, filepath: str) -> list[str]:
    """Return errors if an LFS payload contains repo-sync private markers."""
    if is_binary(payload_path):
        return []

    try:
        text = Path(payload_path).read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return []

    lines = text.splitlines(keepends=True)
    errors = validate_markers(lines, filepath=filepath)
    if errors:
        return [
            f"{error} (Git LFS payloads cannot contain repo-sync private markers)"
            for error in errors
        ]

    if has_private_file_marker(lines):
        return [
            f"{filepath}: Git LFS payload contains a private-file marker; "
            "LFS payloads cannot be stripped during repo sync"
        ]

    try:
        stripped = strip_private_regions(lines, filepath=filepath)
    except MarkerError as exc:
        return [str(exc)]

    if stripped != lines:
        return [
            f"{filepath}: Git LFS payload contains private region markers; "
            "LFS payloads cannot be stripped during repo sync"
        ]

    return []


def validate_lfs_payloads(
    root: str,
    *,
    paths: list[str] | None = None,
    ref: str = "HEAD",
) -> StripResult:
    """Validate repo-sync marker invariants for Git LFS payloads in a repo."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        lfs_paths = _git_lfs_paths(root, ref)
    except subprocess.CalledProcessError as exc:
        stderr = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode("utf-8", errors="replace")
        )
        return StripResult(
            [f"failed to list Git LFS files: {stderr.strip() or exc}"],
            warnings,
        )

    selected_paths = _select_lfs_paths(root, lfs_paths, paths)
    if not selected_paths:
        return StripResult(errors, warnings)

    with tempfile.TemporaryDirectory(prefix="repo-sync-lfs-validate-") as temp_dir:
        for index, relpath in enumerate(selected_paths):
            payload_path = os.path.join(temp_dir, f"payload-{index}")
            try:
                _write_lfs_payload(root, ref, relpath, payload_path)
            except subprocess.CalledProcessError as exc:
                stderr = (
                    exc.stderr
                    if isinstance(exc.stderr, str)
                    else (exc.stderr or b"").decode("utf-8", errors="replace")
                )
                errors.append(
                    f"{relpath}: failed to materialize Git LFS payload: "
                    f"{stderr.strip() or exc}"
                )
                continue
            errors.extend(
                validate_lfs_payload_file(payload_path, filepath=relpath)
            )

    return StripResult(errors, warnings)


def _git_lfs_paths(root: str, ref: str) -> list[str]:
    """Return Git LFS paths present at a ref."""
    result = subprocess.run(
        ["git", "lfs", "ls-files", "--json", ref],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout or "{}")
    files = data.get("files") or []
    paths: list[str] = []
    for entry in files:
        path = entry.get("name") or entry.get("path")
        if path:
            paths.append(path)
    return sorted(paths)


def _select_lfs_paths(
    root: str,
    lfs_paths: list[str],
    paths: list[str] | None,
) -> list[str]:
    """Return the LFS paths selected by the action's path filters."""
    if paths is None:
        return lfs_paths
    if any(
        path == ".gitattributes" or path.endswith("/.gitattributes")
        for path in paths
    ):
        return lfs_paths

    expanded = {os.path.relpath(path, root) for path in _expand_paths(root, paths)}
    selected: list[str] = []
    for lfs_path in lfs_paths:
        if lfs_path in expanded or _matches_any_pattern(lfs_path, paths):
            selected.append(lfs_path)
    return selected


def _matches_any_pattern(path: str, patterns: list[str]) -> bool:
    """Return True if a repo-relative path matches any glob pattern."""
    pure_path = PurePosixPath(path)
    return any(pure_path.match(pattern) for pattern in patterns)


def _write_lfs_payload(root: str, ref: str, relpath: str, output_path: str) -> None:
    """Write the LFS-smudged payload for a path to a temporary file."""
    command = ["git", "cat-file", "--filters", f"{ref}:{relpath}"]
    env = {**os.environ, "GIT_ATTR_SOURCE": ref}
    with open(output_path, "wb") as output:
        subprocess.run(
            command,
            cwd=root,
            stdout=output,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
