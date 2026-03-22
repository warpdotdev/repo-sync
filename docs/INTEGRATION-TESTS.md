# integration tests

manual integration test procedures for validating repo-sync against real GitHub repositories.

## setup

### 1. create test repositories

create two repos in the `warpdotdev` org (or your test org):
- `repo-sync-test-private` -- the private repo
- `repo-sync-test-public` -- the public repo (create it empty, no README)

### 2. configure repo settings

for **both** repos:
- enable auto-merge (Settings > General > Allow auto-merge)
- set squash merge as the default (Settings > General > Allow squash merging)
- configure squash merge to use "PR title and description" as the commit message (Settings > General > Default commit message)
- add branch protection on `repo-sync/*` (Settings > Branches > Add rule):
  - pattern: `repo-sync/**`
  - restrict who can push: only the GitHub App

### 3. set up authentication

create a GitHub App (or use an existing one) with:
- `contents:write`, `pull_requests:write`, `metadata:read` permissions
- installed on both test repos

store the app installation token as a secret named `REPO_SYNC_TOKEN` in both repos.

alternatively, for quick testing, create a fine-grained PAT with the same permissions on both repos and store it as `REPO_SYNC_TOKEN`.

### 4. seed the private repo

clone `repo-sync-test-private` and add some test content:

```sh
git clone git@github.com:warpdotdev/repo-sync-test-private.git
cd repo-sync-test-private

# public file.
echo "public content" > public.txt

# file with mixed public/private content.
cat > mixed.txt << 'EOF'
public line 1
// !repo-sync: private-start
secret line
// !repo-sync: private-end
public line 2
EOF

# fully private directory.
mkdir -p private
echo "secret stuff" > private/secret.txt

# binary file (public).
echo -e "\x89PNG fake image" > logo.png

git add -A && git commit -m "Initial test content"
git push
```

### 5. install repo-sync tooling

```sh
cd /path/to/repo-sync
pip install -e .
```

### 6. add workflow files to both repos

for quick manual testing, use `workflow_dispatch` triggers instead of `push`/`pull_request`/`schedule`.  add to both repos:

```yaml
# .github/workflows/repo-sync.yml
name: repo-sync
on:
  workflow_dispatch:
    inputs:
      action:
        description: "Which action to run"
        required: true
        type: choice
        options: [sync, restack, escalation]

jobs:
  sync:
    if: inputs.action == 'sync'
    uses: warpdotdev/repo-sync/.github/workflows/sync.yml@david/integration
    with:
      peer_repo: warpdotdev/repo-sync-test-public   # adjust for the other repo
      source_is_private: true                        # false for the public repo's copy
    secrets:
      auth_token: ${{ secrets.REPO_SYNC_TOKEN }}

  restack:
    if: inputs.action == 'restack'
    uses: warpdotdev/repo-sync/.github/workflows/restack.yml@david/integration
    with:
      peer_repo: warpdotdev/repo-sync-test-public
      source_is_private: true
    secrets:
      auth_token: ${{ secrets.REPO_SYNC_TOKEN }}

  escalation:
    if: inputs.action == 'escalation'
    uses: warpdotdev/repo-sync/.github/workflows/escalation.yml@david/integration
    with:
      escalate_to: "@oncall-client-primary"
      escalate_after: "5m"
      peer_repo: warpdotdev/repo-sync-test-public
      source_is_private: true
    secrets:
      auth_token: ${{ secrets.REPO_SYNC_TOKEN }}
```

note: use `@david/integration` as the ref (not `@v1`) to test the current branch.

---

## test 1: bootstrap

**goal:** verify the bootstrap script creates the public repo correctly.

**steps:**
1. from the private repo checkout, run:
   ```sh
   cd repo-sync-test-private
   /path/to/repo-sync/scripts/bootstrap.sh \
     --private-repo warpdotdev/repo-sync-test-private \
     --public-repo warpdotdev/repo-sync-test-public \
     --token "$GITHUB_TOKEN"
   ```
2. verify the script completes without error.

**check:**
- [ ] `repo-sync-test-public` has exactly one commit
- [ ] the commit message contains `Repo-Sync-Origin: warpdotdev/repo-sync-test-private@<sha>`
- [ ] `public.txt` exists with content `public content`
- [ ] `mixed.txt` exists with only the public lines (no `secret line`, no markers)
- [ ] `private/` directory does not exist
- [ ] `logo.png` exists
- [ ] the tag `repo-sync/watermark/private-to-public` exists in the public repo
- [ ] the tag `repo-sync/watermark/public-to-private` exists in the private repo
- [ ] running bootstrap again on the same (non-empty) public repo fails with an error

---

## test 2: private-to-public sync (public-only change)

**goal:** a commit that modifies only public code produces a sync PR.

**steps:**
1. in `repo-sync-test-private`, commit and push a change to `public.txt`:
   ```sh
   echo "updated public content" > public.txt
   git add public.txt && git commit -m "Update public content" && git push
   ```
2. trigger the sync workflow (Actions > repo-sync > Run workflow > action: sync).

**check:**
- [ ] a sync PR appears in `repo-sync-test-public`
- [ ] the PR branch is named `repo-sync/private-to-public/<short-sha>`
- [ ] the PR diff shows only the change to `public.txt`
- [ ] the PR description contains the `Repo-Sync-Origin` trailer
- [ ] the PR's commit message is generic (not "Update public content")
- [ ] auto-merge is enabled on the PR (since it's the bottom of the stack)

---

## test 3: private-to-public sync (private-only change)

**goal:** a commit that modifies only private code does NOT produce a sync PR.

**steps:**
1. in `repo-sync-test-private`, commit and push a change inside `private/`:
   ```sh
   echo "more secrets" >> private/secret.txt
   git add -A && git commit -m "Update private content" && git push
   ```
2. trigger the sync workflow.

**check:**
- [ ] no new sync PR is created in `repo-sync-test-public`
- [ ] the workflow completes successfully (not an error)

---

## test 4: private-to-public sync (mixed change)

**goal:** a commit that modifies both public and private code produces a sync PR with only the public changes.

**steps:**
1. in `repo-sync-test-private`, modify both public and private content:
   ```sh
   echo "new public line" >> public.txt
   cat >> mixed.txt << 'EOF'
   // !repo-sync: private-start
   another secret
   // !repo-sync: private-end
   another public line
   EOF
   git add -A && git commit -m "Mixed public and private changes" && git push
   ```
2. trigger the sync workflow.

**check:**
- [ ] a sync PR appears in `repo-sync-test-public`
- [ ] the PR diff includes the change to `public.txt`
- [ ] the PR diff includes "another public line" in `mixed.txt`
- [ ] the PR diff does NOT include "another secret" or any marker lines
- [ ] `private/` directory changes are not in the diff

---

## test 5: public-to-private sync

**goal:** a commit on the public repo produces a sync PR to the private repo.

**steps:**
1. first, merge any open sync PRs in the public repo so it's caught up.
2. in `repo-sync-test-public`, commit and push a change:
   ```sh
   echo "contributed by the community" > community.txt
   git add community.txt && git commit -m "Add community contribution" && git push
   ```
3. trigger the sync workflow in the **public** repo (action: sync, with `source_is_private: false`).

**check:**
- [ ] a sync PR appears in `repo-sync-test-private`
- [ ] the PR branch is named `repo-sync/public-to-private/<short-sha>`
- [ ] the PR preserves the original commit author
- [ ] the PR title matches or references the source commit
- [ ] the PR description includes a "Synced from" header with a link to the source
- [ ] the `Repo-Sync-Origin` trailer is present

---

## test 6: infinite loop prevention

**goal:** merging a sync PR does not trigger a reverse sync.

**steps:**
1. merge the sync PR from test 2 (or any sync PR) in the public repo.
2. trigger the sync workflow in the **public** repo.

**check:**
- [ ] the workflow detects the merged sync commit as sync-originated (via trailer + branch check)
- [ ] no reverse sync PR is created in the private repo for that commit

---

## test 7: stacked PRs (multiple unsynced commits)

**goal:** multiple unsynced commits produce a correctly ordered stack of sync PRs.

**steps:**
1. in `repo-sync-test-private`, create 3 commits:
   ```sh
   echo "commit A" > a.txt && git add a.txt && git commit -m "Commit A" && git push
   echo "commit B" > b.txt && git add b.txt && git commit -m "Commit B" && git push
   echo "commit C" > c.txt && git add c.txt && git commit -m "Commit C" && git push
   ```
2. trigger the sync workflow once.

**check:**
- [ ] 3 sync PRs are created in the public repo
- [ ] PR for commit A is based on `main` (bottom of stack)
- [ ] PR for commit B is based on PR A's branch
- [ ] PR for commit C is based on PR B's branch
- [ ] each PR's diff shows only its own commit's changes
- [ ] only PR A has auto-merge enabled

---

## test 8: restack after merge

**goal:** merging the bottom PR restacks the next PR correctly.

**prerequisite:** test 7 completed (3 stacked PRs exist).

**steps:**
1. merge PR A (the bottom of the stack) in the public repo.
2. trigger the restack workflow in the public repo (action: restack).

**check:**
- [ ] the watermark tag is updated to point to the merge commit
- [ ] PR B is rebased onto `main` (its base branch updated from PR A's branch to `main`)
- [ ] PR B's diff still shows only commit B's changes (no duplicate content from A)
- [ ] auto-merge is enabled on PR B (it's now the bottom)
- [ ] PR C is unchanged (still based on PR B's branch)

---

## test 9: full stack drain

**goal:** verify the entire stack merges correctly in sequence.

**prerequisite:** test 8 completed (PR B is now bottom, PR C on top).

**steps:**
1. let PR B auto-merge (or merge it manually).
2. trigger restack again.
3. let PR C auto-merge (or merge it manually).

**check:**
- [ ] PR B merges cleanly
- [ ] after restack, PR C is rebased onto `main`
- [ ] PR C merges cleanly
- [ ] the watermark has advanced through all three commits
- [ ] the public repo contains `a.txt`, `b.txt`, `c.txt`

---

## test 10: idempotency (re-run after partial completion)

**goal:** re-running the sync workflow doesn't create duplicate PRs.

**steps:**
1. in `repo-sync-test-private`, create a commit and push.
2. trigger the sync workflow -- let it create the sync PR.
3. trigger the sync workflow again (without merging the PR).

**check:**
- [ ] the second run detects the existing sync branch/PR and skips the commit
- [ ] no duplicate PR is created

---

## test 11: conflict resolution

**goal:** a cherry-pick conflict invokes the agent and requests a reviewer.

**steps:**
1. create a divergence: modify the same file in both repos.
   - in the private repo: `echo "private version" > shared.txt && git add -A && git commit -m "Private change to shared.txt" && git push`
   - in the public repo: `echo "public version" > shared.txt && git add -A && git commit -m "Public change to shared.txt" && git push`
2. trigger the sync workflow in the public repo (to sync the public change to private).

**check:**
- [ ] the cherry-pick fails with a conflict
- [ ] the conflict resolution agent is invoked (check workflow logs)
- [ ] if the agent succeeds: a resolution commit is added, a reviewer is requested, auto-merge is NOT enabled
- [ ] if the agent fails: the PR is created with a conflict marker commit, a reviewer is requested
- [ ] the `Repo-Sync-Assigned` trailer is appended to the PR description

---

## test 12: escalation timeout

**goal:** the escalation cron detects stale PRs and escalates.

**prerequisite:** a sync PR with a `Repo-Sync-Assigned` trailer exists (from test 11 or similar).

**steps:**
1. wait for the `Repo-Sync-Assigned` trailer's timestamp to exceed the `escalate_after` threshold (set to `5m` in the test workflow).
2. trigger the escalation workflow.

**check:**
- [ ] the escalation workflow detects the stale PR
- [ ] a review is requested from the configured `escalate_to` team

---

## test 13: watermark recovery after branch deletion

**goal:** verify that branch auto-deletion doesn't break the system.

**steps:**
1. enable "automatically delete head branches" on the public repo (Settings > General).
2. merge a sync PR in the public repo.
3. verify the sync branch is auto-deleted.
4. trigger the sync workflow in the private repo (to create a new sync).

**check:**
- [ ] the workflow reads the watermark tag correctly despite the branch being deleted
- [ ] the new sync PR is created correctly (no already-synced commits are reprocessed)

---

## test 14: bootstrap guard (non-empty public repo)

**goal:** the bootstrap script refuses to run on a non-empty public repo.

**steps:**
1. run the bootstrap script against the already-bootstrapped `repo-sync-test-public`.

**check:**
- [ ] the script exits with an error message about the repo already having commits
- [ ] no changes are made to the public repo

---

## test 15: CI validation action

**goal:** the marker validation action catches errors in CI.

**steps:**
1. in the private repo, add the validation action to CI:
   ```yaml
   # .github/workflows/validate-markers.yml
   name: validate markers
   on: [pull_request]
   jobs:
     validate:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: warpdotdev/repo-sync/actions/validate-markers@david/integration
   ```
2. open a PR with a malformed marker (e.g., `private-start` without `private-end`).
3. open a PR with correctly paired markers.

**check:**
- [ ] the malformed-marker PR fails CI with a clear error message
- [ ] the correctly-paired PR passes CI
