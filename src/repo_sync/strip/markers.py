"""Shared marker library for repo-sync private region handling.

Provides parsing, validation (pairing, nesting detection), and region
stripping for !repo-sync markers.  Used by both the stripping tool and
the CI validation action.
"""

from __future__ import annotations

PRIVATE_START = "!repo-sync: private-start"
PRIVATE_END = "!repo-sync: private-end"
PRIVATE_FILE = "!repo-sync: private-file"


class MarkerError(Exception):
    """Raised when marker validation fails."""


def has_private_file_marker(lines: list[str]) -> bool:
    """Return True if any line contains the whole-file private marker."""
    return any(PRIVATE_FILE in line for line in lines)


def validate_markers(lines: list[str], *, filepath: str = "<unknown>") -> list[str]:
    """Validate and return errors for marker pairing/nesting issues.

    Returns a list of human-readable error strings.  An empty list means
    the markers are valid.
    """
    errors: list[str] = []
    in_private = False
    start_line: int | None = None

    for i, line in enumerate(lines, start=1):
        has_start = PRIVATE_START in line
        has_end = PRIVATE_END in line

        if has_start and has_end:
            # A line containing both markers is ambiguous; treat as an error.
            errors.append(
                f"{filepath}:{i}: line contains both private-start and private-end"
            )
            continue

        if has_start:
            if in_private:
                errors.append(
                    f"{filepath}:{i}: nested private-start "
                    f"(already open at line {start_line})"
                )
            else:
                in_private = True
                start_line = i

        if has_end:
            if not in_private:
                errors.append(
                    f"{filepath}:{i}: private-end without matching private-start"
                )
            else:
                in_private = False
                start_line = None

    if in_private:
        errors.append(
            f"{filepath}: unterminated private-start opened at line {start_line}"
        )

    # A file with the private-file marker must not also have region markers.
    if has_private_file_marker(lines):
        if any(PRIVATE_START in line or PRIVATE_END in line for line in lines):
            errors.append(
                f"{filepath}: private-file marker cannot be combined with "
                "private-start/private-end region markers"
            )

    return errors


def strip_private_regions(
    lines: list[str], *, filepath: str = "<unknown>"
) -> list[str]:
    """Strip private regions from *lines* and return the remaining lines.

    Raises ``MarkerError`` if validation fails.
    """
    errors = validate_markers(lines, filepath=filepath)
    if errors:
        raise MarkerError("\n".join(errors))

    result: list[str] = []
    in_private = False

    for line in lines:
        has_start = PRIVATE_START in line
        has_end = PRIVATE_END in line

        if has_start:
            in_private = True
            continue
        if has_end:
            in_private = False
            continue
        if not in_private:
            result.append(line)

    return result
