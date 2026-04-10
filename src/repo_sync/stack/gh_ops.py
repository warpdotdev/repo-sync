"""Wrapper around gh (GitHub CLI) operations for testability."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from urllib.parse import quote

from repo_sync.stack.constants import SYNC_BRANCH_PREFIX

logger = logging.getLogger(__name__)


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
        if result.returncode != 0:
            if check:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    ["gh", *args],
                    result.stdout,
                    result.stderr,
                )
            # Log failures that are silently ignored so they show up in CI.
            logger.warning(
                "gh %s failed (rc=%d): %s",
                " ".join(args),
                result.returncode,
                result.stderr.strip(),
            )
        return result.stdout.strip()

    def pr_exists(
        self, head_branch: str, any_state: bool = False
    ) -> PullRequest | None:
        """Check if a PR exists for the given head branch.  Returns it or None.

        By default, only checks open PRs.  Pass any_state=True to also find
        merged PRs (used by the idempotency guard to detect PRs that were
        previously created per TECH-DESIGN.md line 37).  PRs that were closed
        without merging are intentionally excluded so that a user can close a
        sync PR and have the workflow recreate it on the next run.
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
            "100",
        ]
        output = self._run(args)
        prs = json.loads(output)
        if not prs:
            return None
        # When searching all states, ignore PRs that were closed without
        # merging.  Only open or merged PRs count as "existing" for
        # idempotency purposes.
        if any_state:
            prs = [
                p for p in prs
                if p["state"].upper() in ("OPEN", "MERGED")
            ]
            if not prs:
                return None
        pr = prs[0]
        state = pr["state"].upper()
        return PullRequest(
            number=pr["number"],
            head_branch=pr["headRefName"],
            base_branch=pr["baseRefName"],
            title=pr["title"],
            body=pr.get("body") or "",
            url=pr["url"],
            state=state,
            merged=state == "MERGED",
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
        # Use --body-file to avoid ARG_MAX limits for large PR descriptions
        # (public-to-private sync copies the source PR body verbatim).
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as f:
            f.write(body)
            body_file = f.name
        try:
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
                    "--body-file",
                    body_file,
                ]
            )
        finally:
            os.unlink(body_file)
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
        """Update a PR's description body.

        Uses --body-file to avoid ARG_MAX limits for large descriptions.
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as f:
            f.write(body)
            body_file = f.name
        try:
            self._run(
                [
                    "pr",
                    "edit",
                    str(pr_number),
                    "--repo",
                    self.repo,
                    "--body-file",
                    body_file,
                ]
            )
        finally:
            os.unlink(body_file)

    def add_pr_comment(self, pr_number: int, body: str) -> None:
        """Add a comment to a PR."""
        self._run(
            [
                "pr",
                "comment",
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
            state=pr["state"].upper(),
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
                if head_branch.startswith(SYNC_BRANCH_PREFIX):
                    all_sync_prs.append(
                        PullRequest(
                            number=pr["number"],
                            head_branch=head_branch,
                            base_branch=pr["base"]["ref"],
                            title=pr["title"],
                            body=pr.get("body") or "",
                            url=pr["html_url"],
                            state=pr["state"].upper(),
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

    def get_tag_sha(self, tag_name: str) -> str | None:
        """Get the commit SHA that a tag points to, or None if the tag doesn't exist."""
        try:
            output = self._run(
                ["api", f"repos/{self.repo}/git/ref/tags/{tag_name}",
                 "--jq", ".object.sha"],
                check=True,
            )
            return output if output else None
        except subprocess.CalledProcessError:
            return None

    def get_commit_message(self, sha: str) -> str | None:
        """Get a commit's message by SHA via the GitHub API."""
        try:
            output = self._run(
                ["api", f"repos/{self.repo}/git/commits/{sha}",
                 "--jq", ".message"],
                check=True,
            )
            return output if output else None
        except subprocess.CalledProcessError:
            return None

    def get_branch_ref_sha(self, branch: str) -> str | None:
        """Get the commit SHA a remote branch points to, or None."""
        try:
            output = self._run(
                ["api", f"repos/{self.repo}/git/ref/heads/{branch}",
                 "--jq", ".object.sha // empty"],
                check=False,
            )
            return output if output else None
        except subprocess.CalledProcessError:
            return None

    def get_pr_state_for_branch(self, head_branch: str) -> str | None:
        """Check if an OPEN or MERGED PR exists for a head branch.

        Returns 'OPEN', 'MERGED', or None.
        """
        output = self._run(
            [
                "pr", "list", "--repo", self.repo,
                "--head", head_branch, "--state", "all",
                "--json", "state",
                "--jq",
                '[.[] | select(.state == "OPEN" or .state == "MERGED")] | .[0].state // empty',
            ],
            check=False,
        )
        return output if output else None

    def create_pr_simple(
        self, head: str, base: str, title: str, body: str
    ) -> str:
        """Create a PR and return its URL.  Raises on failure."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as f:
            f.write(body)
            body_file = f.name
        try:
            return self._run(
                [
                    "pr", "create",
                    "--repo", self.repo,
                    "--head", head,
                    "--base", base,
                    "--title", title,
                    "--body-file", body_file,
                ]
            )
        finally:
            os.unlink(body_file)

    def add_label(self, pr_number: int, label: str) -> None:
        """Add a label to a PR.  Creates the label if it doesn't exist."""
        self._run(
            ["label", "create", label, "--color", "0E8A16",
             "--description", "Sync PR needs rebasing before approval",
             "--repo", self.repo],
            check=False,
        )
        self._run(
            ["pr", "edit", str(pr_number), "--repo", self.repo,
             "--add-label", label],
        )

    def remove_label(self, pr_number: int, label: str) -> None:
        """Remove a label from a PR.  Ignores errors if the label isn't present."""
        self._run(
            ["pr", "edit", str(pr_number), "--repo", self.repo,
             "--remove-label", label],
            check=False,
        )

    def get_commit_author_login(self, sha: str) -> str | None:
        """Get the GitHub login of a commit's author."""
        try:
            output = self._run(
                ["api", f"repos/{self.repo}/commits/{sha}",
                 "--jq", ".author.login"],
                check=True,
            )
            return output if output else None
        except subprocess.CalledProcessError:
            return None

    def get_pr_head_sha(self, pr_number: int) -> str | None:
        """Get the head commit SHA for a PR."""
        output = self._run(
            ["pr", "view", str(pr_number), "--repo", self.repo,
             "--json", "headRefOid", "--jq", ".headRefOid"],
            check=False,
        )
        return output if output else None

    def get_check_failures(self, sha: str) -> int:
        """Count the number of failing check runs on a commit."""
        output = self._run(
            ["api", f"repos/{self.repo}/commits/{sha}/check-runs",
             "--jq", '[.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out")] | length'],
            check=False,
        )
        try:
            return int(output)
        except (ValueError, TypeError):
            return 0
