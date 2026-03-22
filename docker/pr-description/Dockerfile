# Dockerfile for the PR description agent isolation container.
#
# This container provides a hard isolation boundary for the PR description
# agent: it has access to the clean (stripped) codebase snapshot and the
# public diff, but cannot access the private repo's git history, commit
# messages, private files, or PR metadata.
#
# Expected read-only volume mounts (provided by the workflow at runtime):
#   /workspace        - The clean snapshot of the codebase (stripped of all
#                       private code).  This is the agent's working directory.
#   /mnt/diff         - Directory containing public.diff, the unified diff
#                       of changes being synced.
#   /mnt/skill        - Directory containing SKILL.md, the PR description
#                       skill file.
#
# The container does NOT have:
#   - Access to the private repo's .git directory or git history.
#   - Access to private files, commit messages, or PR metadata.
#
# The isolation boundary is filesystem-only: the agent can reach the
# network (required for the oz CLI to call the LLM API), but has no
# access to private repo data on disk.
#
# Example invocation from the workflow:
#   docker run --rm \
#     --read-only \
#     --tmpfs /tmp \
#     -v "$CLEAN_SNAPSHOT:/workspace:ro" \
#     -v "$DIFF_DIR:/mnt/diff:ro" \
#     -v "$SKILL_DIR:/mnt/skill:ro" \
#     -e OZ_API_KEY="$OZ_API_KEY" \
#     repo-sync/pr-description \
#     agent run \
#       --skill /mnt/skill/SKILL.md \
#       --output-format json

FROM python:3.12-slim

# Install the oz CLI.  Pin the version for reproducible builds.
RUN pip install --no-cache-dir oz-sdk==0.1.0

# Do not run as root.
RUN useradd --create-home --shell /bin/bash agent
USER agent

WORKDIR /workspace

# Default entrypoint is the oz CLI.  The workflow overrides the command
# arguments to pass the skill file and output format.
ENTRYPOINT ["oz"]
