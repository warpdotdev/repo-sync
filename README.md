# repo-sync

reusable GitHub workflows and tooling for bidirectional synchronization between a private repository and a public repository.

the private repo may contain internal-only code that must never appear in the public repo.  repo-sync handles stripping that code automatically and creating sync PRs in both directions.

## how it works

when a commit merges to the default branch of either repo, a sync PR is created in the other repo:

- **private → public:** a clean snapshot of the private repo is generated (with all internal code stripped), and the diff is applied to the public repo.
- **public → private:** the public commit is cherry-picked into the private repo as-is.

sync PRs are managed as a **stack** -- each new sync PR is based on the previous one, so conflicts queue up naturally and each PR shows only a single commit's changes.  clean sync PRs auto-merge; conflicted ones get an agent-proposed resolution and a human reviewer.

for full details, see [docs/PRD.md](docs/PRD.md) and [docs/TECH-DESIGN.md](docs/TECH-DESIGN.md).

## marking internal-only code

two mechanisms for keeping code out of the public repo:

### `private` directories

any directory named `private` (at any depth) is excluded entirely.  this is the simplest option for fully-private modules.

### `!repo-sync` markers

for inline private code within otherwise-public files:

```rust
fn my_func() {
  // !repo-sync: private-start
  println!("this code exists only in our private repo");
  // !repo-sync: private-end
  println!("this code is public");
}
```

the marker lines and everything between them are stripped.  markers must be properly paired (every `private-start` needs a `private-end` in the same file) and cannot be nested.

## integration guide

### prerequisites

before integrating, ensure the consuming repos have:

1. **a GitHub App** ("sync bot") installed on both repos (and on the `repo-sync` repo itself) with `contents:write`, `pull_requests:write`, `workflows:write`, and `metadata:read` permissions.  store the App ID and private key as repo secrets (`REPO_SYNC_APP_ID`, `REPO_SYNC_APP_PRIVATE_KEY`) in both repos.  the reusable workflows generate short-lived installation tokens internally.
2. **a second GitHub App** ("approver bot") with `contents:write`, `pull_requests:write`, and `metadata:read` permissions.  this app handles approval and conflict resolution for sync PRs — a separate identity is needed because GitHub does not allow a PR's author to approve it.  store as `REPO_SYNC_APPROVER_APP_ID` and `REPO_SYNC_APPROVER_APP_PRIVATE_KEY`.
3. **auto-merge enabled** as a repo-level setting.
4. **squash merge** as the merge strategy for PRs, configured to **preserve the PR description** in the commit message.
5. **branch protection rules** on `repo-sync/*` branches, so only the sync workflow's token can create or push to them.
6. **required PR approvals** on the default branch.  the approver bot approves clean (conflict-free) sync PRs automatically; conflict-resolved PRs require human approval.

### step 1: bootstrap the public repo

the bootstrap script creates the initial public repo from the private repo:

```sh
./scripts/bootstrap.sh \
  --private-repo warpdotdev/warp-internal \
  --public-repo warpdotdev/warp-public \
  --token "$GITHUB_TOKEN"
```

this:
- queries the private repo's default branch and uses it for the public repo (both must match)
- generates a clean snapshot of the private repo at `HEAD` (stripping all `private/` dirs and `!repo-sync` marker regions)
- pushes the snapshot as the initial commit to the public repo
- checks that the public repo has no existing commits (refuses to overwrite)
- sets watermark tags in both repos so the sync workflows know where to start

the token must have `contents:write`, `pull_requests:write`, and `workflows:write` on both repos (the `workflows` scope is needed because the repo may contain `.github/workflows/` files).  you can generate one by creating a GitHub App installation token (recommended) or using a fine-grained PAT for one-time use.

### step 2: add the CI validation action (private repo)

add marker validation to the private repo's CI so developers catch issues before merging:

```yaml
# .github/workflows/validate-markers.yml
name: validate markers
on: [pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: warpdotdev/repo-sync/actions/validate-markers@v1
```

this validates that all `!repo-sync` markers are properly paired, not nested, and that no symlinks exist in the repo.

### step 3: add the sync workflow (both repos)

add the sync workflow to both repos.  see [examples/consuming-repo-sync.yml](examples/consuming-repo-sync.yml) for a complete example.  the key pieces are:

```yaml
# .github/workflows/repo-sync.yml
name: repo-sync
on:
  push:
    branches: [main]            # triggers sync creation
  pull_request:
    types: [closed, opened, synchronize, edited]
    branches: [main]            # triggers restack + approve
  schedule:
    - cron: "*/15 * * * *"      # triggers escalation checks

# store REPO_SYNC_APP_ID and REPO_SYNC_APPROVER_APP_ID as variables,
# and their private keys as secrets.

jobs:
  sync:
    if: github.event_name == 'push'
    uses: warpdotdev/repo-sync/.github/workflows/sync.yml@v1
    with:
      public_repo: warpdotdev/warp-public
      private_repo: warpdotdev/warp-internal
      app_id: ${{ vars.REPO_SYNC_APP_ID }}
    secrets:
      app_private_key: ${{ secrets.REPO_SYNC_APP_PRIVATE_KEY }}

  restack:
    if: github.event_name == 'pull_request' && github.event.pull_request.merged == true && startsWith(github.event.pull_request.head.ref, 'repo-sync/')
    uses: warpdotdev/repo-sync/.github/workflows/restack.yml@v1
    with:
      public_repo: warpdotdev/warp-public
      private_repo: warpdotdev/warp-internal
      app_id: ${{ vars.REPO_SYNC_APP_ID }}
    secrets:
      app_private_key: ${{ secrets.REPO_SYNC_APP_PRIVATE_KEY }}

  approve:
    if: github.event_name == 'pull_request' && github.event.action != 'closed' && startsWith(github.event.pull_request.head.ref, 'repo-sync/')
    uses: warpdotdev/repo-sync/.github/workflows/approve.yml@v1
    with:
      public_repo: warpdotdev/warp-public
      private_repo: warpdotdev/warp-internal
      approver_app_id: ${{ vars.REPO_SYNC_APPROVER_APP_ID }}
    secrets:
      approver_app_private_key: ${{ secrets.REPO_SYNC_APPROVER_APP_PRIVATE_KEY }}

  escalation:
    if: github.event_name == 'schedule'
    uses: warpdotdev/repo-sync/.github/workflows/escalation.yml@v1
    with:
      escalate_to: "@oncall-client-primary"
      escalate_after: "1h"
      public_repo: warpdotdev/warp-public
      private_repo: warpdotdev/warp-internal
      app_id: ${{ vars.REPO_SYNC_APP_ID }}
    secrets:
      app_private_key: ${{ secrets.REPO_SYNC_APP_PRIVATE_KEY }}
```

the same workflow file works in both repos -- the workflows derive which repo is which by comparing `github.repository` against the `private_repo` input.

### step 4: verify

after setup, test by merging a small change in each direction:

1. **private → public:** merge a commit to the private repo that modifies public code.  a sync PR should appear in the public repo within a few minutes.
2. **public → private:** merge a commit to the public repo.  a sync PR should appear in the private repo.
3. **internal-only change:** merge a commit that only modifies code inside `private/` dirs or `!repo-sync` markers.  no sync PR should be created.

## conflict resolution

when a sync PR reaches the bottom of the stack and has merge conflicts, the approval workflow invokes an Oz agent to propose a resolution.  a human reviewer is always requested for sign-off on conflict-resolved PRs.  if the agent fails, the PR is assigned to a human without a proposed resolution.

if the reviewer doesn't respond within the configured timeout (default: 1 hour), the PR is escalated to the configured team (default: `@oncall-client-primary`).

for operational procedures and failure remediation, see [docs/RUNBOOK.md](docs/RUNBOOK.md).

## project structure

```
.agents/skills/           # oz agent skill definitions
  conflict-resolution/    # generic merge conflict resolution
  pr-description/         # PR description generation (private→public)
.github/workflows/        # reusable GitHub Actions workflows
  sync.yml                # sync PR creation
  restack.yml             # post-merge restacking
  approve.yml             # approval + conflict resolution
  escalation.yml          # cron: timeout, CI failure, stuck stack
actions/
  validate-markers/       # CI validation composite action
docker/
  pr-description/         # Dockerfile for agent isolation
docs/                     # design documents
  PRD.md                  # product requirements
  TECH-DESIGN.md          # technical design
  DECISIONS.md            # decision log
  VALIDATION.md           # test cases
examples/
  consuming-repo-sync.yml # example consuming repo workflow
scripts/
  bootstrap.sh            # one-time bootstrap script
src/repo_sync/            # python package
  strip/                  # stripping tool + shared marker library
  stack/                  # stack management + trailer parsing
  workflows/              # workflow orchestration logic
tests/                    # pytest test suite
```

## known limitations

- **both repos must use the same default branch** (e.g., both use `main`).  the bootstrap script enforces this, and the sync workflows assume it.
- **symlinks are not supported.**  any symlink in the repo will cause the stripping tool to error.  this is a fail-closed safety measure -- symlinks could potentially bypass `private/` directory exclusion.  the CI validation action also checks for this.

## documentation

- [docs/PRD.md](docs/PRD.md) -- product requirements and high-level behavior
- [docs/TECH-DESIGN.md](docs/TECH-DESIGN.md) -- technical design and implementation details
- [docs/DECISIONS.md](docs/DECISIONS.md) -- decision log with alternatives and justifications
- [docs/VALIDATION.md](docs/VALIDATION.md) -- comprehensive test cases
- [docs/RUNBOOK.md](docs/RUNBOOK.md) -- operational procedures for failure scenarios
