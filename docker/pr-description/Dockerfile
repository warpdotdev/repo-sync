# Dockerfile for the PR description agent isolation container.
#
# This container provides a hard isolation boundary for the PR description
# agent: it has access to the clean (stripped) codebase snapshot and the
# public diff, but cannot access the private repo's git history, commit
# messages, private files, or PR metadata.
#
# Expected read-only volume mounts, provided by the workflow at runtime:
#   /mnt/snapshot     - The clean snapshot of the codebase (stripped of all
#                       private code).  The agent uses this as its working
#                       directory for codebase context.
#   /mnt/diff/public.diff
#                     - The unified diff of changes being synced.
#
# The container does NOT have access to the following:
#   - The private repo's .git directory or git history.
#   - Private files, commit messages, or PR metadata.
#   - The git CLI (not installed, as defense-in-depth).
#
# The isolation boundary is filesystem-only: the agent can reach the
# network (required for the LLM API), but has no access to private repo
# data on disk.
#
# The image is built locally by the sync workflow (not pushed to a
# registry).  It is invoked by _run_pr_description_agent() in
# create_sync_prs.py, which passes the volume mounts and CLI flags:
#
#   docker run --rm \
#     -e WARP_API_KEY \
#     -v "$CLEAN_SNAPSHOT:/mnt/snapshot:ro" \
#     -v "$DIFF_FILE:/mnt/diff/public.diff:ro" \
#     repo-sync-pr-description \
#     agent run \
#       --skill pr-description \
#       --cwd /mnt/snapshot \
#       --output-format json

FROM warpdotdev/warp-agent:latest

# Install the PR description skill where the --skill flag can discover it.
# The oz CLI searches .agents/skills/ (among others) in the home directory
# and the working directory.  We place it in the home directory because the
# working directory is a read-only volume mount that would overlay any files
# placed here at build time.
COPY .agents/skills/pr-description/SKILL.md /home/warp-agent/.agents/skills/pr-description/SKILL.md

# Use a minimal entrypoint that skips git/gh setup (isolation: no access
# to private repo history or credentials).
COPY docker/pr-description/entrypoint.sh /pr-description-entrypoint.sh
ENTRYPOINT ["/pr-description-entrypoint.sh"]
