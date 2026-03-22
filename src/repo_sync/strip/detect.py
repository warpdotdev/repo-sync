"""Text vs. binary detection for repo-sync stripping.

Uses the same heuristic as git: a file is binary if its first 8192 bytes
contain a null byte (\\x00).
"""

from __future__ import annotations

# Number of leading bytes to inspect for the null-byte heuristic.
_SNIFF_SIZE = 8192


def is_binary(path: str) -> bool:
    """Return True if *path* appears to be a binary file."""
    with open(path, "rb") as f:
        chunk = f.read(_SNIFF_SIZE)
    return b"\x00" in chunk
