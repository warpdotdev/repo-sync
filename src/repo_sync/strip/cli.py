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

    args = parser.parse_args(argv)

    paths = args.paths if args.paths else None

    if args.validate_only:
        errors = strip_tree(args.directory, validate_only=True, paths=paths)
        if errors:
            for err in errors:
                print(err, file=sys.stderr)
            return 1
        return 0

    try:
        strip_tree(args.directory)
    except StrippingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
