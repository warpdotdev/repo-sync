"""PR description construction for sync PRs.

Private-to-public: uses a generic fallback title/body (the agent-generated
description is handled externally via Docker invocation in the YAML workflow).

Public-to-private: copies the source PR title/description with a "Synced from"
header, or falls back to the commit message for direct pushes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PRDescription:
    """A constructed PR title and body."""

    title: str
    body: str


def parse_agent_output(raw_text: str) -> PRDescription | None:
    """Parse the structured output from the PR description agent.

    Expects the text to contain a TITLE: line followed by a DESCRIPTION:
    section.  Returns a PRDescription if both sections are found, or None
    otherwise.
    """
    title_match = re.search(r"^TITLE:[ \t]*(.+)$", raw_text, re.MULTILINE)
    desc_match = re.search(
        r"^DESCRIPTION:[ \t]*(.*)", raw_text, re.MULTILINE | re.DOTALL
    )
    if not title_match or not desc_match:
        return None
    title = title_match.group(1).strip()
    body = desc_match.group(1).strip()
    if not title or not body:
        return None
    return PRDescription(title=title, body=body)


def private_to_public_fallback(short_sha: str) -> PRDescription:
    """Generate a fallback PR description for private-to-public sync.

    Used when the Oz agent fails to generate a description.
    """
    return PRDescription(
        title=f"repo-sync: sync from private ({short_sha})",
        body=f"repo-sync: sync from private (source: `{short_sha}`)",
    )


def private_to_public_default_title(short_sha: str) -> str:
    """Generate the default PR title for private-to-public sync.

    This is overridden if the agent produces a title.
    """
    return f"repo-sync: sync from private ({short_sha})"


def public_to_private_from_pr(
    source_repo_name: str,
    source_pr_title: str,
    source_pr_body: str,
    source_pr_url: str,
) -> PRDescription:
    """Construct a PR description for public-to-private sync from a source PR.

    Copies the source PR title and prepends a "Synced from" header to the body.
    """
    body = f"Synced from {source_repo_name}: {source_pr_url}\n\n{source_pr_body}"
    return PRDescription(title=source_pr_title, body=body)


def public_to_private_from_commit(
    source_repo_name: str,
    commit_subject: str,
    commit_body: str,
    commit_url: str,
) -> PRDescription:
    """Construct a PR description for public-to-private sync from a direct push.

    Uses the commit message as the title and prepends a "Synced from" header.
    """
    body = f"Synced from {source_repo_name}: {commit_url}"
    if commit_body:
        body = f"{body}\n\n{commit_body}"
    return PRDescription(title=commit_subject, body=body)
