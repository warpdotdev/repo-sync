# Dockerfile for the conflict-resolution agent container.
#
# Unlike the PR description agent (which is read-only and isolated from
# private repo data), this container needs read-write access to the repo
# to edit files, run git commands, compile, and test.
#
# Expected read-write volume mount, provided by the workflow at runtime:
#   /mnt/repo          - The git repo with conflict markers to resolve.
#
# Expected environment variables:
#   WARP_API_KEY       - Required.  API key for the LLM backend.
#   GIT_USER_NAME      - Optional.  Git identity for commits.
#   GIT_USER_EMAIL     - Optional.  Git identity for commits.
#   GH_TOKEN           - Optional.  GitHub token for API access.
#
# The image is built locally by the sync/approve workflows (not pushed
# to a registry).  It is invoked by _run_conflict_resolution_agent() in
# agent.py:
#
#   docker run --rm \
#     -e WARP_API_KEY \
#     -e GH_TOKEN \
#     -e GIT_USER_NAME \
#     -e GIT_USER_EMAIL \
#     -v "$REPO_DIR:/mnt/repo" \
#     repo-sync-conflict-resolution \
#     agent run \
#       --skill conflict-resolution \
#       --cwd /mnt/repo \
#       --output-format json

FROM warpdotdev/warp-agent:latest-preview

# Install git.  The base image intentionally omits it (for isolation in
# the PR description agent), but the conflict-resolution agent needs it
# to stage files, finalize rebases/cherry-picks, and commit resolutions.
USER root
RUN apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1 && rm -rf /var/lib/apt/lists/*
USER warp-agent

# Install the conflict-resolution skill where the --skill flag can
# discover it.
COPY .agents/skills/conflict-resolution/SKILL.md /home/warp-agent/.warp/skills/conflict-resolution/SKILL.md

# Use an entrypoint that sets up git identity and credentials (unlike
# pr-description, which skips git setup for isolation).
COPY docker/conflict-resolution/entrypoint.sh /conflict-resolution-entrypoint.sh
ENTRYPOINT ["/conflict-resolution-entrypoint.sh"]
