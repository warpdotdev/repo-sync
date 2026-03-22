"""Wrapper around gh (GitHub CLI) operations for testability."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from urllib.parse import quote


@dataclass
class PullRequest:
    """Represents a GitHub pull request."""

    number: int
    head_branch: str
    base_branch: str
    title: str
    body: str
    url: str
    state: str
    merged: bool = False
    auto_merge_enabled: bool = False


class GhOps:
    """Encapsulates gh CLI operations so they can be replaced in tests."""

    def __init__(self, repo: str, token: str | None = None) -> None:
        # Repo in owner/name format, e.g. "warpdotdev/warp-internal".
        self.repo = repo
        self.token = token

    def _run(self, args: list[str], check: bool = True) -> str:
        """Run a gh command and return stdout."""
        env_additions: dict[str, str] = {}
        if self.token:
            env_additions["GH_TOKEN"] = self.token
        env = {**os.environ, **env_additions}
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            env=env,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                ["gh", *args],
                result.stdout,
                result.stderr,
            )
        return result.stdout.strip()

    def pr_exists(
        self, head_branch: str, any_state: bool = False
    ) -> PullRequest | None:
        """Check if a PR exists for the given head branch. Returns it or None.

        By default, only checks open PRs.  Pass any_state=True to also find
        merged or closed PRs (used by the idempotency guard to detect PRs that
        were previously created per TECH-DESIGN.md line 37).
        """
        args = [
            "pr",
            "list",
            "--repo",
            self.repo,
            "--head",
            head_branch,
            "--state",
            "all" if any_state else "open",
            "--json",
            "number,headRefName,baseRefName,title,body,url,state,autoMergeRequest",
            "--limit",
            "1",
        ]
        output = self._run(args)
        prs = json.loads(output)
        if not prs:
            return None
        pr = prs[0]
        return PullRequest(
            number=pr["number"],
            head_branch=pr["headRefName"],
            base_branch=pr["baseRefName"],
            title=pr["title"],
            body=pr.get("body") or "",
            url=pr["url"],
            state=pr["state"],
            merged=pr["state"] == "MERGED",
            auto_merge_enabled=pr.get("autoMergeRequest") is not None,
        )

    def create_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequest:
        """Create a pull request and return its metadata.

        gh pr create does not support --json, so we capture the PR URL from
        stdout and then fetch structured data via gh pr view.
        """
        pr_url = self._run(
            [
                "pr",
                "create",
                "--repo",
                self.repo,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        # gh pr create prints the new PR's URL to stdout.
        output = self._run(
            [
                "pr",
                "view",
                pr_url,
                "--repo",
                self.repo,
                "--json",
                "number,headRefName,baseRefName,title,body,url,state",
            ]
        )
        pr = json.loads(output)
        return PullRequest(
            number=pr["number"],
            head_branch=pr["headRefName"],
            base_branch=pr["baseRefName"],
            title=pr["title"],
            body=pr.get("body") or "",
            url=pr["url"],
            state=pr["state"],
        )

    def enable_auto_merge(self, pr_number: int) -> None:
        """Enable auto-merge (squash) on a PR."""
        self._run(
            [
                "pr",
                "merge",
                str(pr_number),
                "--repo",
                self.repo,
                "--auto",
                "--squash",
            ]
        )

    def disable_auto_merge(self, pr_number: int) -> None:
        """Disable auto-merge on a PR."""
        self._run(
            [
                "pr",
                "merge",
                str(pr_number),
                "--repo",
                self.repo,
                "--disable-auto",
            ]
        )

    def update_pr_base(self, pr_number: int, new_base: str) -> None:
        """Update a PR's base branch."""
        self._run(
            [
                "pr",
                "edit",
                str(pr_number),
                "--repo",
                self.repo,
                "--base",
                new_base,
            ]
        )

    def update_pr_body(self, pr_number: int, body: str) -> None:
        """Update a PR's description body."""
        self._run(
            [
                "pr",
                "edit",
                str(pr_number),
                "--repo",
                self.repo,
                "--body",
                body,
            ]
        )

    def request_reviewer(
        self, pr_number: int, reviewer: str
    ) -> None:
        """Request a review from a user or team on a PR."""
        self._run(
            [
                "pr",
                "edit",
                str(pr_number),
                "--repo",
                self.repo,
                "--add-reviewer",
                reviewer,
            ]
        )

    def get_pr_for_commit(self, sha: str) -> PullRequest | None:
        """Find the merged PR that introduced a commit into the default branch.

        Uses the GitHub commits/{sha}/pulls API for an exact commit-level
        lookup, avoiding false-positive text matches from gh pr list --search.
        Filters for merged PRs to avoid picking up open/closed PRs that happen
        to include the same commit.
        """
        try:
            output = self._run(
                [
                    "api",
                    f"repos/{self.repo}/commits/{sha}/pulls",
                    "--jq",
                    '[.[] | select(.merged_at != null)] | .[0]',
                ],
            )
        except subprocess.CalledProcessError:
            return None
        if not output or output == "null":
            return None
        pr = json.loads(output)
        return PullRequest(
            number=pr["number"],
            head_branch=pr["head"]["ref"],
            base_branch=pr["base"]["ref"],
            title=pr["title"],
            body=pr.get("body") or "",
            url=pr["html_url"],
            state=pr["state"],
            merged=True,
        )

    def get_pr_merger(self, pr_number: int) -> str | None:
        """Get the login of the user who merged a PR."""
        output = self._run(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                self.repo,
                "--json",
                "mergedBy",
            ],
            check=False,
        )
        if not output:
            return None
        data = json.loads(output)
        merged_by = data.get("mergedBy")
        if merged_by:
            return merged_by.get("login")
        return None

    def list_open_sync_prs(self) -> list[PullRequest]:
        """List all open PRs with repo-sync/ head branches.

        Paginates through all open PRs to avoid missing sync PRs in repos
        with many open PRs.
        """
        page_size = 100
        all_sync_prs: list[PullRequest] = []
        page = 1

        while True:
            output = self._run(
                [
                    "api",
                    f"repos/{self.repo}/pulls",
                    "-X", "GET",
                    "-f", "state=open",
                    "-f", f"per_page={page_size}",
                    "-f", f"page={page}",
                ]
            )
            prs = json.loads(output)
            if not prs:
                break
            for pr in prs:
                head_branch = pr["head"]["ref"]
                if head_branch.startswith("repo-sync/"):
                    all_sync_prs.append(
                        PullRequest(
                            number=pr["number"],
                            head_branch=head_branch,
                            base_branch=pr["base"]["ref"],
                            title=pr["title"],
                            body=pr.get("body") or "",
                            url=pr["html_url"],
                            state=pr["state"],
                            auto_merge_enabled=pr.get("auto_merge") is not None,
                        )
                    )
            if len(prs) < page_size:
                break
            page += 1

        return all_sync_prs

    def branch_exists_on_remote(self, branch: str) -> bool:
        """Check if a branch exists on the remote via the GitHub API."""
        # URL-encode the branch name to handle slashes in sync branch names
        # (e.g. repo-sync/private-to-public/abc123).
        encoded_branch = quote(branch, safe="")
        try:
            self._run(
                ["api", f"repos/{self.repo}/branches/{encoded_branch}"],
                check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False
