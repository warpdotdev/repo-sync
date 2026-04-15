#!/bin/sh
# Minimal entrypoint for the PR description agent.
#
# This intentionally skips the default warp-agent entrypoint's git/gh setup
# to maintain the isolation boundary: the agent must not have access to
# private repo history or credentials.

set -e

# Resolve the agent binary.
AGENT_BIN_DIR="/opt/warpdotdev/oz"
AGENT_BINARY="$AGENT_BIN_DIR/oz"

export PATH="$PATH:$AGENT_BIN_DIR"

if [ -z "$WARP_API_KEY" ]; then
    echo "WARP_API_KEY is not set" >&2
    exit 1
fi

exec "$AGENT_BINARY" "$@"
