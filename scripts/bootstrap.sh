#!/usr/bin/env bash
# Bootstrap script for repo-sync.
#
# Generates a clean snapshot of the private repo at HEAD, pushes it as the
# initial commit to the public repo, and sets watermark tags in both repos.
#
# This is a one-time setup step.  After bootstrap, the regular sync workflows
# take over.
#
# Usage:
#   ./scripts/bootstrap.sh \
#     --private-repo <owner/repo> \
#     --public-repo <owner/repo> \
#     --public-default-branch <branch> \
#     --token <github-token>
#
# Prerequisites:
#   - The private repo must be checked out locally (script runs from its root).
#   - The public repo must exist (can be empty or newly created).
#   - The stripping tool must be installed (pip install -e . from the repo-sync repo).
#   - gh CLI must be authenticated with the provided token.
#   - git must be configured with user.name and user.email.

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments.
# ---------------------------------------------------------------------------
PRIVATE_REPO=""
PUBLIC_REPO=""
PUBLIC_DEFAULT_BRANCH="main"
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
    --public-default-branch)
      PUBLIC_DEFAULT_BRANCH="$2"
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
  echo "Optional: --public-default-branch <branch> (default: main)" >&2
  exit 1
fi

export GH_TOKEN="$TOKEN"
HEAD_SHA=$(git rev-parse HEAD)
SHORT_SHA="${HEAD_SHA:0:7}"

echo "=== repo-sync bootstrap ==="
echo "Private repo: ${PRIVATE_REPO}"
echo "Public repo:  ${PUBLIC_REPO}"
echo "HEAD:         ${HEAD_SHA}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Generate clean snapshot of the private repo at HEAD.
# ---------------------------------------------------------------------------
echo "Step 1: Generating clean snapshot..."
SNAPSHOT_DIR=$(mktemp -d)

# Invoke the stripping tool.
# Expected interface: python -m repo_sync.strip <commit-sha> <output-dir>.
python -m repo_sync.strip "${HEAD_SHA}" "${SNAPSHOT_DIR}"
echo "Clean snapshot generated at ${SNAPSHOT_DIR}."

# ---------------------------------------------------------------------------
# Step 2: Push the snapshot as the initial commit to the public repo.
# ---------------------------------------------------------------------------
echo "Step 2: Pushing initial commit to public repo..."
WORK_DIR=$(mktemp -d)

pushd "$WORK_DIR" > /dev/null

# Initialize a new git repo and create the initial commit.
git init -b "${PUBLIC_DEFAULT_BRANCH}"
cp -a "${SNAPSHOT_DIR}/." .
git add -A

# Check if there's anything to commit.
if git diff --cached --quiet; then
  echo "Error: clean snapshot is empty.  Nothing to push." >&2
  popd > /dev/null
  rm -rf "$SNAPSHOT_DIR" "$WORK_DIR"
  exit 1
fi

git commit -m "repo-sync: initial sync from private repo

Repo-Sync-Origin: ${PRIVATE_REPO}@${HEAD_SHA}"

INITIAL_COMMIT_SHA=$(git rev-parse HEAD)
echo "Initial commit: ${INITIAL_COMMIT_SHA}"

# Push to the public repo.
git remote add origin "https://x-access-token:${TOKEN}@github.com/${PUBLIC_REPO}.git"
git push -u origin "${PUBLIC_DEFAULT_BRANCH}" --force
echo "Pushed to ${PUBLIC_REPO}/${PUBLIC_DEFAULT_BRANCH}."

popd > /dev/null

# ---------------------------------------------------------------------------
# Step 3: Set watermark tag in the public repo.
# The watermark points to the initial commit in the public repo.
# ---------------------------------------------------------------------------
echo "Step 3: Setting watermark in public repo..."
WATERMARK_TAG="repo-sync/watermark/private-to-public"

# Get the actual commit SHA on the public repo (after push).
PUBLIC_HEAD_SHA=$(gh api "/repos/${PUBLIC_REPO}/git/ref/heads/${PUBLIC_DEFAULT_BRANCH}" \
  --jq '.object.sha')

# Create or update the watermark tag.
EXISTING_TAG=$(gh api "/repos/${PUBLIC_REPO}/git/ref/tags/${WATERMARK_TAG}" \
  --jq '.ref' 2>/dev/null || true)

if [ -n "$EXISTING_TAG" ]; then
  gh api -X PATCH "/repos/${PUBLIC_REPO}/git/refs/tags/${WATERMARK_TAG}" \
    -f sha="${PUBLIC_HEAD_SHA}" \
    -F force=true
else
  gh api -X POST "/repos/${PUBLIC_REPO}/git/refs" \
    -f ref="refs/tags/${WATERMARK_TAG}" \
    -f sha="${PUBLIC_HEAD_SHA}"
fi

echo "Watermark '${WATERMARK_TAG}' set in ${PUBLIC_REPO} -> ${PUBLIC_HEAD_SHA}."

# ---------------------------------------------------------------------------
# Step 4: Set watermark tag in the private repo.
# Use git's empty tree SHA as a sentinel — there are no public commits to
# sync back yet.  The public-to-private workflow will see this and know that
# the initial public commit (which has a Repo-Sync-Origin trailer) should be
# skipped.
# ---------------------------------------------------------------------------
echo "Step 4: Setting watermark in private repo..."
PRIVATE_WATERMARK_TAG="repo-sync/watermark/public-to-private"

# The sentinel: we create a lightweight tag pointing to the public repo's
# initial commit.  Since that commit has a Repo-Sync-Origin trailer, the
# public-to-private workflow will recognize it and skip it.
#
# We need to set this in the private repo.  The watermark points to the
# public repo's initial commit, and the Repo-Sync-Origin trailer on that
# commit tells the workflow that everything up to HEAD_SHA has been synced.
#
# Since the watermark tag for public-to-private lives in the private repo
# (the target repo for that direction), we need to create a commit object
# in the private repo that has the right trailer.  We'll create a tag
# pointing to a specially-crafted commit.
#
# Simplest approach: create a lightweight tag pointing to the current HEAD
# of the private repo.  The initial public commit already has
# Repo-Sync-Origin: <private-repo>@<HEAD>, so the public-to-private
# workflow will look at the public repo's HEAD and see that the last commit
# is sync-originated.  But the watermark is read differently for the reverse
# direction — it needs to point to the last synced PUBLIC commit.
#
# For the initial bootstrap, we tag the public repo's initial commit in the
# private repo's watermark.  We use the GitHub API to create a commit object
# in the private repo that carries the right trailer.
BOOTSTRAP_MSG="repo-sync: bootstrap sentinel

Repo-Sync-Origin: ${PUBLIC_REPO}@${PUBLIC_HEAD_SHA}"

# Create a commit object in the private repo using the tree of the current HEAD.
PRIVATE_HEAD_SHA=$(gh api "/repos/${PRIVATE_REPO}/git/ref/heads/$(gh api "/repos/${PRIVATE_REPO}" --jq '.default_branch')" \
  --jq '.object.sha')
PRIVATE_TREE=$(gh api "/repos/${PRIVATE_REPO}/git/commits/${PRIVATE_HEAD_SHA}" \
  --jq '.tree.sha')

SENTINEL_COMMIT=$(gh api -X POST "/repos/${PRIVATE_REPO}/git/commits" \
  -f message="${BOOTSTRAP_MSG}" \
  -f tree="${PRIVATE_TREE}" \
  -f "parents[]=${PRIVATE_HEAD_SHA}" \
  --jq '.sha')

# Create or update the watermark tag.
EXISTING_PRIVATE_TAG=$(gh api "/repos/${PRIVATE_REPO}/git/ref/tags/${PRIVATE_WATERMARK_TAG}" \
  --jq '.ref' 2>/dev/null || true)

if [ -n "$EXISTING_PRIVATE_TAG" ]; then
  gh api -X PATCH "/repos/${PRIVATE_REPO}/git/refs/tags/${PRIVATE_WATERMARK_TAG}" \
    -f sha="${SENTINEL_COMMIT}" \
    -F force=true
else
  gh api -X POST "/repos/${PRIVATE_REPO}/git/refs" \
    -f ref="refs/tags/${PRIVATE_WATERMARK_TAG}" \
    -f sha="${SENTINEL_COMMIT}"
fi

echo "Watermark '${PRIVATE_WATERMARK_TAG}' set in ${PRIVATE_REPO} -> ${SENTINEL_COMMIT}."

# ---------------------------------------------------------------------------
# Cleanup.
# ---------------------------------------------------------------------------
rm -rf "$SNAPSHOT_DIR" "$WORK_DIR"

echo ""
echo "=== Bootstrap complete ==="
echo "Public repo ${PUBLIC_REPO} has been initialized with a clean snapshot."
echo "Watermarks have been set in both repos."
echo "The regular sync workflows can now take over."
