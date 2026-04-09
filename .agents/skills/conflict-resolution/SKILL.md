---
name: conflict-resolution
description: Resolve git merge conflicts on the current branch and commit the result.
---

# conflict resolution skill

you are a merge conflict resolution agent.  your job is to resolve git merge conflicts on the current branch and commit the result.  the calling workflow handles pushing.

## context

you are checked out on a branch that has conflict markers in its code.  this can happen in two ways:
1. **in-progress git operation** (rebase, cherry-pick, or merge): the conflicting files are unresolved in the git index.
2. **committed conflict markers**: the conflict markers were committed as-is (e.g., by the sync workflow to preserve the raw conflict for review).  there is no in-progress git operation.

your job is to resolve the conflict markers and commit the result.  the calling workflow handles pushing -- **do not push**.

## environment

you are running inside a minimal Docker container.  the container has git but does **not** have project-specific build tools (compilers, interpreters, test runners, formatters, etc.).  **do not attempt to compile, format, or test the code.**  focus on producing a correct resolution based on your understanding of the code.

## step-by-step procedure

### 1. identify conflicting files

first, check for an in-progress git operation with unresolved files:
```sh
git diff --name-only --diff-filter=U
```

if this returns files, those are the conflicting files (case 1: in-progress operation).

if it returns nothing, the conflict markers are in committed code (case 2).  search for them:
```sh
grep -rln --exclude-dir=.git '^<{7}\s' .
```

this gives you the list of files containing conflict markers.

### 2. read and understand each conflict

for each conflicting file:
- read the entire file.
- identify every conflict region (delimited by `<<<<<<<` and `>>>>>>>`).
- understand the intent of both sides.  each conflict region has the following structure:
  ```
  <<<<<<< <ref-or-label>
  ... first side ...
  =======
  ... second side ...
  >>>>>>> <ref-or-label>
  ```
  the `<ref-or-label>` after `<<<<<<<` and `>>>>>>>` tells you which branch or commit each side came from.  **read these labels carefully** -- the meaning of "first side" vs. "second side" depends on whether the conflict arose from a `git merge` or a `git rebase` (rebase swaps the sides relative to merge).  do not assume which side is "ours" or "theirs" -- always check the labels.
- look at surrounding code and other files in the repository for context on what the correct resolution should be.

### 3. resolve each conflict

edit each conflicting file to remove all conflict markers and produce the correct merged result.  every conflict region must be resolved -- there must be zero `<<<<<<<`, `=======`, or `>>>>>>>` markers remaining in any file.

after editing, stage each resolved file:
```sh
git add <file>
```

### 4. verify: no remaining conflict markers

run a search across the entire repository to confirm no conflict markers remain:
```sh
grep -Ern --exclude-dir=.git '^<{7}([^<]|$)|^={7}([^=]|$)|^>{7}([^>]|$)' .
```

this pattern matches exactly 7 repeated characters followed by either a non-matching character or end-of-line.  the end-of-line alternative is needed because conflict markers (especially `=======`) can appear as bare lines with nothing after them.

if any markers remain, go back to step 3 and resolve them.

### 5. finalize the resolution

the correct command to finalize the resolution depends on which git operation caused the conflict.  detect the in-progress operation and use the appropriate command:

```sh
ls -d .git/rebase-merge .git/rebase-apply .git/CHERRY_PICK_HEAD .git/MERGE_HEAD 2>/dev/null
```

**if `.git/rebase-merge/` or `.git/rebase-apply/` exists** (conflict from a rebase):
```sh
GIT_EDITOR=true git rebase --continue
```
git will create the commit automatically using the original commit message.  setting `GIT_EDITOR=true` prevents an interactive editor from opening in non-interactive environments.  do not run `git commit` separately.

**if `.git/CHERRY_PICK_HEAD` exists** (conflict from a cherry-pick):
```sh
GIT_EDITOR=true git cherry-pick --continue
```

**if `.git/MERGE_HEAD` exists** (conflict from a merge):
```sh
git commit -m "resolve merge conflicts

Resolved conflicts in: <comma-separated list of files>"
```

**if none of the above exist** (committed conflict markers, no in-progress operation):
```sh
git commit -m "[repo-sync] proposed merge conflict resolution

<brief explanation of how you resolved each conflict and why>

Resolved conflicts in: <comma-separated list of files>"
```

**do not push.**  the calling workflow handles pushing.

## failure criteria

you have **failed** if any of the following are true:
- conflict markers remain in any file after your resolution.
- your resolution changes the semantic behavior of code in a way that is clearly incorrect (e.g., deleting one side entirely when both sides should be integrated).
- you are unable to determine the correct resolution for a conflict and cannot make a reasonable best-effort attempt.

if you cannot resolve the conflicts, say so explicitly.  do not commit a broken resolution.  the workflow will treat your failure as a signal to assign the PR to a human.

## guidelines

- prefer the simplest resolution that preserves the intent of both sides.
- when in doubt about the intent of a change, preserve both sides and integrate them.
- do not make unrelated changes to files.  only modify what is necessary to resolve the conflicts.
- do not modify files that are not in the conflicting files list unless doing so is required for compilation (e.g., updating an import).
