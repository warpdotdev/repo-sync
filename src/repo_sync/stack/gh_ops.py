"""Wrapper around gh (GitHub CLI) operations for testability."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


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
        import os

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

    def pr_exists(self, head_branch: str) -> PullRequest | None:
        """Check if a PR exists for the given head branch. Returns it or None."""
        output = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--head",
                head_branch,
                "--json",
                "number,headRefName,baseRefName,title,body,url,state,autoMergeRequest",
                "--limit",
                "1",
            ]
        )
        prs = json.loads(output)
        if not prs:
            return None
        pr = prs[0]
        return PullRequest(
            number=pr["number"],
            head_branch=pr["headRefName"],
            base_branch=pr["baseRefName"],
            title=pr["title"],
            body=pr.get("body", ""),
            url=pr["url"],
            state=pr["state"],
            auto_merge_enabled=pr.get("autoMergeRequest") is not None,
        )

    def create_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequest:
        """Create a pull request and return its metadata."""
        output = self._run(
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
            body=pr.get("body", ""),
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
        """Find the PR that merged a given commit into the default branch."""
        output = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--search",
                sha,
                "--state",
                "merged",
                "--json",
                "number,headRefName,baseRefName,title,body,url,state,mergedBy",
                "--limit",
                "1",
            ],
            check=False,
        )
        if not output:
            return None
        prs = json.loads(output)
        if not prs:
            return None
        pr = prs[0]
        return PullRequest(
            number=pr["number"],
            head_branch=pr["headRefName"],
            base_branch=pr["baseRefName"],
            title=pr["title"],
            body=pr.get("body", ""),
            url=pr["url"],
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
        """List all open PRs with repo-sync/ head branches."""
        output = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--json",
                "number,headRefName,baseRefName,title,body,url,state,autoMergeRequest",
                "--limit",
                "100",
            ]
        )
        all_prs = json.loads(output)
        result = []
        for pr in all_prs:
            if pr["headRefName"].startswith("repo-sync/"):
                result.append(
                    PullRequest(
                        number=pr["number"],
                        head_branch=pr["headRefName"],
                        base_branch=pr["baseRefName"],
                        title=pr["title"],
                        body=pr.get("body", ""),
                        url=pr["url"],
                        state=pr["state"],
                        auto_merge_enabled=pr.get("autoMergeRequest")
                        is not None,
                    )
                )
        return result

    def branch_exists_on_remote(self, branch: str) -> bool:
        """Check if a branch exists on the remote via the GitHub API."""
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{self.repo}/branches/{branch}",
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
