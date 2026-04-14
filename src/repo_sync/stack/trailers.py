"""Parse and write Repo-Sync-Origin and Repo-Sync-Assigned trailers.

Trailers appear in PR descriptions and commit messages.  Since public-to-private
sync copies the source PR description verbatim (untrusted input), the source
description could contain spoofed trailers.  To handle this, all parsing uses
the **last occurrence** of each trailer type -- the workflow always appends its
trailers at the end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Trailer prefixes.
_ORIGIN_PREFIX = "Repo-Sync-Origin:"
_ASSIGNED_PREFIX = "Repo-Sync-Assigned:"
_CONFLICT_PREFIX = "Repo-Sync-Conflict:"


@dataclass(frozen=True)
class SyncOrigin:
    """Parsed Repo-Sync-Origin trailer value."""

    repo: str
    sha: str

    def __str__(self) -> str:
        """Format as the trailer value string."""
        return f"{self.repo}@{self.sha}"


@dataclass(frozen=True)
class SyncAssignment:
    """Parsed Repo-Sync-Assigned trailer value."""

    username: str
    timestamp: datetime

    def __str__(self) -> str:
        """Format as the trailer value string."""
        utc_ts = self.timestamp.astimezone(timezone.utc)
        ts = utc_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{self.username}@{ts}"


def _try_parse_origin_value(value: str) -> SyncOrigin | None:
    """Try to parse a <repo>@<sha> value string into a SyncOrigin."""
    value = value.strip()
    # Value format: <repo>@<sha>.  The repo may contain slashes
    # (e.g. "warpdotdev/warp-internal"), so we split on the last "@".
    at_idx = value.rfind("@")
    if at_idx > 0 and at_idx < len(value) - 1:
        return SyncOrigin(repo=value[:at_idx], sha=value[at_idx + 1 :])
    return None


def parse_origin(text: str) -> SyncOrigin | None:
    """Extract the last Repo-Sync-Origin trailer from text.

    Handles line-wrapped trailer values: GitHub's squash merge may wrap long
    trailer lines, placing the value on the line after the key.  When the key
    line has no value, the next non-empty line is checked.

    Returns None if no trailer is found.
    """
    last_match: SyncOrigin | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith(_ORIGIN_PREFIX):
            value = stripped[len(_ORIGIN_PREFIX) :].strip()
            parsed = _try_parse_origin_value(value)
            if parsed is not None:
                last_match = parsed
            elif not value:
                # Key line has no value — check the next non-empty line
                # (GitHub squash merge may have wrapped the value).
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines):
                    parsed = _try_parse_origin_value(lines[j].strip())
                    if parsed is not None:
                        last_match = parsed
                        i = j  # Skip past the continuation line.
        i += 1
    return last_match


def parse_assigned(text: str) -> SyncAssignment | None:
    """Extract the last Repo-Sync-Assigned trailer from text.

    Returns None if no trailer is found.
    """
    last_match: SyncAssignment | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_ASSIGNED_PREFIX):
            value = stripped[len(_ASSIGNED_PREFIX) :].strip()
            # Value format: <username>@<ISO-8601-timestamp>.  Username does not
            # contain "@", so we split on the last "@".
            at_idx = value.rfind("@")
            if at_idx > 0:
                username = value[:at_idx]
                ts_str = value[at_idx + 1 :]
                try:
                    # Accept both with and without trailing Z.
                    ts_str_clean = ts_str.replace("Z", "+00:00")
                    ts = datetime.fromisoformat(ts_str_clean)
                    # Ensure timezone-aware (UTC).
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    last_match = SyncAssignment(
                        username=username, timestamp=ts
                    )
                except ValueError:
                    # Malformed timestamp -- skip this occurrence.
                    pass
    return last_match


def format_origin_trailer(repo: str, sha: str) -> str:
    """Format a Repo-Sync-Origin trailer line."""
    return f"{_ORIGIN_PREFIX} {repo}@{sha}"


def parse_conflict(text: str) -> bool:
    """Check if the text contains a Repo-Sync-Conflict trailer.

    Returns True if the trailer is present.
    """
    for line in text.splitlines():
        if line.strip().startswith(_CONFLICT_PREFIX):
            return True
    return False


def format_conflict_trailer(conflict_type: str = "cherry-pick") -> str:
    """Format a Repo-Sync-Conflict trailer line."""
    return f"{_CONFLICT_PREFIX} {conflict_type}"


def format_assigned_trailer(username: str, timestamp: datetime) -> str:
    """Format a Repo-Sync-Assigned trailer line.

    Converts the timestamp to UTC before formatting to ensure the trailing
    'Z' is always correct.
    """
    utc_ts = timestamp.astimezone(timezone.utc)
    ts = utc_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{_ASSIGNED_PREFIX} {username}@{ts}"


def append_trailer(body: str, trailer_line: str) -> str:
    """Append a trailer line to a PR description body.

    Ensures there is a blank line separator before the trailer if the body
    does not already end with one.
    """
    if body and not body.endswith("\n"):
        body += "\n"
    if body and not body.endswith("\n\n"):
        body += "\n"
    return body + trailer_line + "\n"
