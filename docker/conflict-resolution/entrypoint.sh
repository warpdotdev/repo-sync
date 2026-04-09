#!/bin/sh
# Entrypoint for the conflict-resolution agent.
#
# Unlike the PR description entrypoint, this sets up git identity and
# credentials because the agent needs to run git operations (stage, commit,
# rebase --continue, etc.) inside the mounted repo.

set -e

# Resolve the agent binary.
AGENT_BIN_DIR="/opt/warpdotdev/oz-preview"
AGENT_BINARY="$AGENT_BIN_DIR/oz-preview"

export PATH="$PATH:$AGENT_BIN_DIR"

if [ -z "$WARP_API_KEY" ]; then
    echo "WARP_API_KEY is not set" >&2
    exit 1
fi

# Set up git identity if provided.
if [ -n "$GIT_USER_NAME" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Mark the mounted repo as safe (it's owned by a different uid on the host).
git config --global --add safe.directory /mnt/repo

exec "$AGENT_BINARY" "$@"
