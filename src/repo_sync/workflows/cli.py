"""CLI entrypoints for the workflow YAML to call.

Each subcommand corresponds to a logical step in the YAML workflow.  The YAML
passes arguments (from GitHub Actions context) and reads structured output
(JSON to stdout or GitHub Actions output files).

Usage from YAML:
    python -m repo_sync.workflows.cli sync-plan --source-repo ... --trigger-sha ...
    python -m repo_sync.workflows.cli build-description --source-repo ... --source-sha ...
    python -m repo_sync.workflows.cli escalation-check --escalate-after 1h ...
    python -m repo_sync.workflows.cli detect-direction --head-branch ... --source-is-private ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.loop_detection import is_sync_originated
from repo_sync.stack.trailers import SyncOrigin, parse_origin
from repo_sync.stack.watermark import read_watermark

from repo_sync.workflows.descriptions import (
    private_to_public_fallback,
    public_to_private_from_commit,
    public_to_private_from_pr,
)
from repo_sync.workflows.escalation import (
    EscalationAction,
    check_ci_failure,
    check_stuck_stack,
    check_timeout_escalation,
    evaluate_pr,
    parse_duration,
)
from repo_sync.workflows.restack_workflow import determine_direction
from repo_sync.workflows.sync import (
    SyncConfig,
    build_public_to_private_description,
    determine_sync_reviewer,
    enumerate_unsynced_commits,
    find_existing_stack_top,
)


def _write_github_output(key: str, value: str) -> None:
    """Write a key=value pair to $GITHUB_OUTPUT if available."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")


def cmd_loop_check(args: argparse.Namespace) -> None:
    """Check if the triggering commit is sync-originated."""
    git = GitOps(args.repo_dir)
    gh = GhOps(args.gh_repo, token=os.environ.get("GH_TOKEN"))

    result = is_sync_originated(git, gh, args.trigger_sha)
    output = {"is_sync": result}
    json.dump(output, sys.stdout)
    print()
    _write_github_output("is_sync", str(result).lower())


def cmd_read_watermark(args: argparse.Namespace) -> None:
    """Read the watermark and output the last-synced source SHA."""
    git = GitOps(args.repo_dir)
    direction = args.direction

    watermark = read_watermark(git, direction)
    if watermark is None:
        print(
            json.dumps({"error": f"No watermark for direction '{direction}'."}),
        )
        sys.exit(1)

    output = {"repo": watermark.repo, "sha": watermark.sha}
    json.dump(output, sys.stdout)
    print()
    _write_github_output("last_synced_sha", watermark.sha)


def cmd_list_unsynced(args: argparse.Namespace) -> None:
    """List unsynced commits after the watermark."""
    git = GitOps(args.repo_dir)
    gh = GhOps(args.gh_repo, token=os.environ.get("GH_TOKEN"))

    watermark_origin = SyncOrigin(repo=args.watermark_repo, sha=args.watermark_sha)
    commits = enumerate_unsynced_commits(
        git, gh, args.direction, args.default_branch, watermark_origin
    )

    output = {"commits": commits, "count": len(commits)}
    json.dump(output, sys.stdout)
    print()
    _write_github_output("count", str(len(commits)))

    # Write commits to a file for the next step to consume.
    if commits:
        with open("/tmp/unsynced_commits.txt", "w") as f:
            for sha in commits:
                f.write(f"{sha}\n")


def cmd_find_stack_top(args: argparse.Namespace) -> None:
    """Find the top of the existing sync PR stack."""
    gh = GhOps(args.peer_repo, token=os.environ.get("GH_TOKEN"))
    top = find_existing_stack_top(gh, args.direction)

    output = {"stack_top": top or ""}
    json.dump(output, sys.stdout)
    print()
    _write_github_output("stack_top", top or "")
    _write_github_output("has_stack", str(top is not None).lower())


def cmd_build_description(args: argparse.Namespace) -> None:
    """Build a PR description for public-to-private sync."""
    gh = GhOps(args.source_repo, token=os.environ.get("GH_TOKEN"))
    desc = build_public_to_private_description(
        source_gh=gh,
        source_repo=args.source_repo,
        source_sha=args.source_sha,
        commit_subject=args.commit_subject,
        commit_body=args.commit_body or "",
    )

    output = {"title": desc.title, "body": desc.body}
    json.dump(output, sys.stdout)
    print()


def cmd_determine_reviewer(args: argparse.Namespace) -> None:
    """Determine who should review a sync PR."""
    gh = GhOps(args.source_repo, token=os.environ.get("GH_TOKEN"))
    reviewer = determine_sync_reviewer(
        source_gh=gh,
        source_sha=args.source_sha,
        fallback_team=args.fallback_team,
    )

    output = {"reviewer": reviewer}
    json.dump(output, sys.stdout)
    print()
    _write_github_output("reviewer", reviewer)


def cmd_detect_direction(args: argparse.Namespace) -> None:
    """Detect the sync direction for a restack operation."""
    direction = determine_direction(
        merged_head_branch=args.head_branch or None,
        source_is_private=args.source_is_private,
    )

    output = {
        "direction": direction,
        "branch_prefix": f"repo-sync/{direction}",
    }
    json.dump(output, sys.stdout)
    print()
    _write_github_output("direction", direction)
    _write_github_output("branch_prefix", f"repo-sync/{direction}")


def cmd_escalation_check(args: argparse.Namespace) -> None:
    """Run escalation checks on all open sync PRs."""
    gh = GhOps(args.gh_repo, token=os.environ.get("GH_TOKEN"))
    escalate_after = parse_duration(args.escalate_after)
    now = datetime.now(timezone.utc)

    open_prs = gh.list_open_sync_prs()
    results = []

    for pr in open_prs:
        # Check if base branch exists (for stuck stack detection).
        base_exists = True
        if pr.base_branch != args.default_branch:
            base_exists = gh.branch_exists_on_remote(pr.base_branch)

        # Check CI status (the caller provides this via the GH API).
        # For now, we assume the YAML layer checks CI and passes the result.
        # In the CLI, we can check it directly.
        ci_failed = False
        # TODO: Add CI status check via GH API when integrated.

        check = evaluate_pr(
            pr=pr,
            escalate_after=escalate_after,
            default_branch=args.default_branch,
            ci_has_failed=ci_failed,
            base_branch_exists=base_exists,
            now=now,
        )

        if check.actions:
            results.append(
                {
                    "pr_number": check.pr_number,
                    "head_branch": check.head_branch,
                    "actions": [a.value for a in check.actions],
                }
            )

    json.dump({"checks": results, "total_prs": len(open_prs)}, sys.stdout)
    print()


def main() -> None:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="repo-sync-workflow",
        description="CLI entrypoints for repo-sync workflow orchestration.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # loop-check.
    p = subparsers.add_parser("loop-check", help="Check if commit is sync-originated.")
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--gh-repo", required=True)
    p.add_argument("--trigger-sha", required=True)
    p.set_defaults(func=cmd_loop_check)

    # read-watermark.
    p = subparsers.add_parser("read-watermark", help="Read the watermark tag.")
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--direction", required=True)
    p.set_defaults(func=cmd_read_watermark)

    # list-unsynced.
    p = subparsers.add_parser("list-unsynced", help="List unsynced commits.")
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--gh-repo", required=True)
    p.add_argument("--direction", required=True)
    p.add_argument("--default-branch", required=True)
    p.add_argument("--watermark-repo", required=True)
    p.add_argument("--watermark-sha", required=True)
    p.set_defaults(func=cmd_list_unsynced)

    # find-stack-top.
    p = subparsers.add_parser("find-stack-top", help="Find existing stack top.")
    p.add_argument("--peer-repo", required=True)
    p.add_argument("--direction", required=True)
    p.set_defaults(func=cmd_find_stack_top)

    # build-description.
    p = subparsers.add_parser(
        "build-description", help="Build public-to-private PR description."
    )
    p.add_argument("--source-repo", required=True)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--commit-subject", required=True)
    p.add_argument("--commit-body", default="")
    p.set_defaults(func=cmd_build_description)

    # determine-reviewer.
    p = subparsers.add_parser("determine-reviewer", help="Determine PR reviewer.")
    p.add_argument("--source-repo", required=True)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--fallback-team", default="oncall-client-primary")
    p.set_defaults(func=cmd_determine_reviewer)

    # detect-direction.
    p = subparsers.add_parser("detect-direction", help="Detect sync direction.")
    p.add_argument("--head-branch", default="")
    p.add_argument(
        "--source-is-private", action="store_true", default=False
    )
    p.set_defaults(func=cmd_detect_direction)

    # escalation-check.
    p = subparsers.add_parser(
        "escalation-check", help="Run escalation checks on sync PRs."
    )
    p.add_argument("--gh-repo", required=True)
    p.add_argument("--default-branch", required=True)
    p.add_argument("--escalate-after", required=True)
    p.set_defaults(func=cmd_escalation_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
