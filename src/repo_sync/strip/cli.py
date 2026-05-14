"""CLI entrypoint for the repo-sync stripping tool.

Usage:
    repo-sync-strip <directory>
        Strip private content from the given directory tree in-place.

    repo-sync-strip --validate-only <directory> [paths...]
        Validate markers without modifying files.  Optionally restrict
        validation to specific relative paths.
"""

from __future__ import annotations

import argparse
import sys

from repo_sync.strip.lfs import validate_lfs_payloads
from repo_sync.strip.tree import StrippingError, strip_tree


def main(argv: list[str] | None = None) -> int:
    """Run the stripping tool.  Returns 0 on success, 1 on failure."""
    parser = argparse.ArgumentParser(
        prog="repo-sync-strip",
        description="Strip private content from a repo tree.",
    )
    parser.add_argument(
        "directory",
        help="Root directory of the tree to process.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "Optional list of relative file paths to validate "
            "(only meaningful with --validate-only)."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        default=False,
        help="Only validate markers; do not modify files.",
    )
    parser.add_argument(
        "--validate-lfs-payloads",
        action="store_true",
        default=False,
        help="Validate that Git LFS payloads do not contain repo-sync markers.",
    )

    args = parser.parse_args(argv)

    paths = args.paths if args.paths else None

    if args.validate_only:
        result = strip_tree(args.directory, validate_only=True, paths=paths)
        if args.validate_lfs_payloads:
            lfs_result = validate_lfs_payloads(args.directory, paths=paths)
            result = result._replace(
                errors=[*result.errors, *lfs_result.errors],
                warnings=[*result.warnings, *lfs_result.warnings],
            )
        for w in result.warnings:
            print(f"warning: {w}", file=sys.stderr)
        if result.errors:
            for err in result.errors:
                print(err, file=sys.stderr)
            return 1
        return 0
    if args.validate_lfs_payloads:
        print(
            "--validate-lfs-payloads requires --validate-only",
            file=sys.stderr,
        )
        return 1

    try:
        result = strip_tree(args.directory)
    except StrippingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
