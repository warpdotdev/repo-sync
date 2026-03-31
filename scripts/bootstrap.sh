#!/usr/bin/env bash
# Bootstrap script for repo-sync.
#
# Generates a clean snapshot of the private repo at HEAD, prepares a commit
# in the public repo that replaces its entire tree with the clean snapshot,
# and prints instructions for the user to review, push, and set watermarks.
#
# The script stops before pushing so you can review the change.
#
# This is a one-time setup step.  After bootstrap + watermark setup, the
# regular sync workflows take over.
#
# Usage:
#   ./scripts/bootstrap.sh \
#     --private-repo <owner/repo> \
#     --public-repo <owner/repo> \
#     --token <github-token>
#
# The token must have contents:write, pull_requests:write, and workflows:write
# on both repos.  The workflows:write scope is needed because the private repo
# may contain .github/workflows/ files that get synced to the public repo.
# You can generate one by:
#   - Creating a GitHub App installation token (recommended for production).
#   - Using a fine-grained PAT with the required permissions (simpler for one-time use).
#
# Prerequisites:
#   - The private repo must be checked out locally (script runs from its root).
#   - The public repo must exist on GitHub (can be empty or have existing content).
#   - The stripping tool must be installed (pip install -e . from the repo-sync repo).
#   - gh CLI must be installed.
#   - git must be configured with user.name and user.email.

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments.
# ---------------------------------------------------------------------------
PRIVATE_REPO=""
PUBLIC_REPO=""
TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --private-repo)
      PRIVATE_REPO="$2"
      shift 2
      ;;
    --public-repo)
      PUBLIC_REPO="$2"
      shift 2
      ;;
    --token)
      TOKEN="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ -z "$PRIVATE_REPO" ] || [ -z "$PUBLIC_REPO" ] || [ -z "$TOKEN" ]; then
  echo "Usage: $0 --private-repo <owner/repo> --public-repo <owner/repo> --token <token>" >&2
  exit 1
fi

export GH_TOKEN="$TOKEN"
export GH_PAGER="cat"
HEAD_SHA=$(git rev-parse HEAD)
SHORT_SHA="${HEAD_SHA:0:7}"

# ---------------------------------------------------------------------------
# Pre-flight: validate that the token can access both repos.
# ---------------------------------------------------------------------------

# Detect token type for diagnostic context.
TOKEN_PREFIX="${TOKEN:0:4}"
case "$TOKEN_PREFIX" in
  ghs_) TOKEN_TYPE="GitHub App installation token" ;;
  ghp_) TOKEN_TYPE="classic personal access token" ;;
  gith) TOKEN_TYPE="fine-grained personal access token" ;;
  *)    TOKEN_TYPE="unknown token type (prefix: ${TOKEN_PREFIX})" ;;
esac

echo "Validating access to repositories (using ${TOKEN_TYPE})..."

for REPO_SLUG in "$PRIVATE_REPO" "$PUBLIC_REPO"; do
  # The 'if' suppresses set -e so we can capture the error.
  if ! REPO_RESPONSE=$(gh api "/repos/${REPO_SLUG}" 2>&1); then
    echo "" >&2
    echo "ERROR: Cannot access '${REPO_SLUG}'." >&2
    echo "" >&2
    echo "  gh error: ${REPO_RESPONSE}" >&2
    echo "" >&2
    echo "Possible causes:" >&2
    echo "  - The repository '${REPO_SLUG}' does not exist." >&2
    echo "  - The token does not have access to this repository." >&2
    case "$TOKEN_TYPE" in
      "GitHub App installation token")
        echo "  - The GitHub App may not be installed on the '${REPO_SLUG%%/*}' org/user." >&2
        echo "  - The installation may not have '${REPO_SLUG#*/}' in its selected repositories." >&2
        ;;
      "fine-grained personal access token")
        echo "  - The PAT may not include '${REPO_SLUG}' in its repository scope." >&2
        ;;
    esac
    echo "" >&2
    echo "Required token permissions: contents:write, pull_requests:write, workflows:write" >&2
    exit 1
  fi
done

# Use the private repo's default branch for both repos.
DEFAULT_BRANCH=$(gh api "/repos/${PRIVATE_REPO}" --jq '.default_branch')

echo "=== repo-sync bootstrap ==="
echo "Private repo:   ${PRIVATE_REPO}"
echo "Public repo:    ${PUBLIC_REPO}"
echo "Default branch: ${DEFAULT_BRANCH}"
echo "HEAD:           ${HEAD_SHA}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Generate clean snapshot of the private repo at HEAD.
# ---------------------------------------------------------------------------
echo "Step 1: Generating clean snapshot..."
SNAPSHOT_DIR=$(mktemp -d)

# Extract the tree at HEAD and strip it in-place.
git archive "${HEAD_SHA}" | tar -x -C "${SNAPSHOT_DIR}"
python -m repo_sync.strip.cli "${SNAPSHOT_DIR}"
echo "Clean snapshot generated at ${SNAPSHOT_DIR}."

# ---------------------------------------------------------------------------
# Step 2: Prepare the bootstrap commit in the public repo.
# ---------------------------------------------------------------------------
echo "Step 2: Preparing bootstrap commit in public repo..."
WORK_DIR=$(mktemp -d)

# Check if the public repo is empty or has existing content.
IS_EMPTY="false"
EXISTING_COMMITS_RAW=$(gh api "/repos/${PUBLIC_REPO}/commits?per_page=1" 2>/dev/null || true)
if ! echo "$EXISTING_COMMITS_RAW" | jq -e 'type == "array" and length > 0' > /dev/null 2>&1; then
  IS_EMPTY="true"
fi

if [ "$IS_EMPTY" = "true" ]; then
  echo "Public repo is empty.  Creating initial commit."
  pushd "$WORK_DIR" > /dev/null
  git init -b "${DEFAULT_BRANCH}"
  git remote add origin "https://x-access-token:${TOKEN}@github.com/${PUBLIC_REPO}.git"
else
  echo "Public repo has existing content.  Cloning and replacing tree."
  git clone "https://x-access-token:${TOKEN}@github.com/${PUBLIC_REPO}.git" "$WORK_DIR"
  pushd "$WORK_DIR" > /dev/null
  git checkout "${DEFAULT_BRANCH}"
  # Remove all existing content (but keep .git/).
  git rm -rf --quiet . 2>/dev/null || true
fi

# Copy the clean snapshot into the working tree.
cp -a "${SNAPSHOT_DIR}/." .
git add -A

# Check if there are any changes to commit.
if git diff --cached --quiet; then
  echo "No changes -- the public repo already matches the clean snapshot."
  popd > /dev/null
  rm -rf "$SNAPSHOT_DIR" "$WORK_DIR"
  exit 0
fi

git commit -m "repo-sync: initial sync from private repo

Repo-Sync-Origin: ${PRIVATE_REPO}@${HEAD_SHA}"

BOOTSTRAP_COMMIT_SHA=$(git rev-parse HEAD)
echo "Bootstrap commit: ${BOOTSTRAP_COMMIT_SHA}"

popd > /dev/null

# ---------------------------------------------------------------------------
# Step 3: Print review and next-steps instructions.
# ---------------------------------------------------------------------------
echo ""
echo "=========================================================================="
echo "  Bootstrap commit is ready for review."
echo "=========================================================================="
echo ""
echo "The commit has been prepared locally in:"
echo "  ${WORK_DIR}"
echo ""
echo "--- Review the changes: ---"
echo ""
if [ "$IS_EMPTY" = "true" ]; then
  echo "  cd ${WORK_DIR} && git log -1"
  echo "  cd ${WORK_DIR} && git diff --stat HEAD"
else
  echo "  cd ${WORK_DIR} && git log -1"
  echo "  cd ${WORK_DIR} && git diff HEAD~1 --stat"
  echo "  cd ${WORK_DIR} && git diff HEAD~1"
fi
echo ""
echo "--- Push (after reviewing): ---"
echo ""
if [ "$IS_EMPTY" = "true" ]; then
  echo "  cd ${WORK_DIR} && git push -u origin ${DEFAULT_BRANCH}"
else
  echo "  cd ${WORK_DIR} && git push origin ${DEFAULT_BRANCH}"
fi
echo ""
echo "--- Set watermark tags (after pushing): ---"
echo ""
echo "Run these commands to set the watermark tags that the sync workflows need."
echo "You can use the same token you used for bootstrap."
echo ""

# Public repo watermark: points to the bootstrap commit.
echo "  # Set watermark in public repo (private-to-public direction)."
echo "  PUBLIC_HEAD=\$(gh api /repos/${PUBLIC_REPO}/git/ref/heads/${DEFAULT_BRANCH} --jq '.object.sha')"
echo "  gh api -X POST /repos/${PUBLIC_REPO}/git/refs -f ref=refs/tags/repo-sync/watermark/private-to-public -f sha=\${PUBLIC_HEAD} 2>/dev/null \\"
echo "    || gh api -X PATCH /repos/${PUBLIC_REPO}/git/refs/tags/repo-sync/watermark/private-to-public -f sha=\${PUBLIC_HEAD} -F force=true"
echo ""

# Private repo watermark: a sentinel commit with the Repo-Sync-Origin trailer
# so the public-to-private workflow knows the bootstrap commit is already synced.
echo "  # Set watermark in private repo (public-to-private direction)."
echo "  PRIVATE_HEAD=\$(gh api /repos/${PRIVATE_REPO}/git/ref/heads/${DEFAULT_BRANCH} --jq '.object.sha')"
echo "  PRIVATE_TREE=\$(gh api /repos/${PRIVATE_REPO}/git/commits/\${PRIVATE_HEAD} --jq '.tree.sha')"
echo "  PUBLIC_HEAD=\$(gh api /repos/${PUBLIC_REPO}/git/ref/heads/${DEFAULT_BRANCH} --jq '.object.sha')"
echo "  SENTINEL=\$(gh api -X POST /repos/${PRIVATE_REPO}/git/commits \\"
echo "    -f message='repo-sync: bootstrap sentinel"
echo ""
echo "Repo-Sync-Origin: ${PUBLIC_REPO}@'\${PUBLIC_HEAD} \\"
echo "    -f tree=\${PRIVATE_TREE} \\"
echo "    -f \"parents[]=\${PRIVATE_HEAD}\" \\"
echo "    --jq '.sha')"
echo "  gh api -X POST /repos/${PRIVATE_REPO}/git/refs -f ref=refs/tags/repo-sync/watermark/public-to-private -f sha=\${SENTINEL} 2>/dev/null \\"
echo "    || gh api -X PATCH /repos/${PRIVATE_REPO}/git/refs/tags/repo-sync/watermark/public-to-private -f sha=\${SENTINEL} -F force=true"
echo ""
echo "--- Cleanup (after setting watermarks): ---"
echo ""
echo "  rm -rf ${WORK_DIR} ${SNAPSHOT_DIR}"
echo ""
echo "NOTE: Do not delete the directories above until you have pushed and set watermarks."
