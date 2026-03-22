# conflict resolution skill

you are a merge conflict resolution agent.  your job is to resolve git merge conflicts on the current branch, produce code that compiles and passes tests, and push the result.

## context

you are checked out on a branch that has unresolved merge conflicts.  the conflicting files contain standard git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`).

## step-by-step procedure

### 1. identify conflicting files

run:
```sh
git diff --name-only --diff-filter=U
```

this gives you the list of files with unresolved conflicts.

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

### 5. verify: code compiles

determine the project's build system and run the appropriate compile/check command.  examples:
- rust: `cargo check`
- python: `python -m py_compile <file>` for each changed `.py` file, or the project's lint command
- typescript: `npx tsc --noEmit`
- go: `go build ./...`

check the project's README, Makefile, or build configuration to determine the correct command.

if compilation fails, revisit your conflict resolution and fix the issues.  do not proceed until the code compiles successfully.

### 6. verify: code is formatted

run the project's formatter on the conflicting files (and any files you modified for compilation).  examples:
- rust: `cargo fmt -- <file1> <file2> ...`
- python: `ruff format <file1> <file2> ...` or `black <file1> <file2> ...`
- typescript: `npx prettier --write <file1> <file2> ...`

only format files that you modified as part of the resolution.  do not run the formatter on the entire repository.

stage any formatting changes on those files:
```sh
git add <file1> <file2> ...
```

### 7. run affected tests

identify tests that are likely affected by the files you changed.  run those tests using the project's test runner.  check the project's README, Makefile, or build configuration for the correct test command.

if tests fail and the failure is caused by your resolution, revisit and fix the resolution.  if the failure appears to be a pre-existing issue unrelated to the conflict, note it but proceed.

### 8. finalize the resolution

the correct command to finalize the resolution depends on which git operation caused the conflict.  detect the in-progress operation and use the appropriate command:

**if `.git/rebase-merge/` or `.git/rebase-apply/` exists** (conflict from a rebase):
```sh
GIT_EDITOR=true git rebase --continue
```
git will create the commit automatically using the original commit message.  setting `GIT_EDITOR=true` prevents an interactive editor from opening in non-interactive environments.  do not run `git commit` separately.

**if `.git/CHERRY_PICK_HEAD` exists** (conflict from a cherry-pick):
```sh
GIT_EDITOR=true git cherry-pick --continue
```

**if `.git/MERGE_HEAD` exists** (conflict from a merge), or if none of the above apply:
```sh
git commit -m "resolve merge conflicts

Resolved conflicts in: <comma-separated list of files>"
```

you can check which case applies by running:
```sh
ls -d .git/rebase-merge .git/rebase-apply .git/CHERRY_PICK_HEAD .git/MERGE_HEAD 2>/dev/null
```

### 9. push

push the resolution to the remote branch:
```sh
git push
```

## failure criteria

you have **failed** if any of the following are true:
- conflict markers remain in any file after your resolution.
- the code does not compile after your resolution.
- tests fail as a result of your resolution and you cannot fix the failures.
- your resolution changes the semantic behavior of code in a way that is clearly incorrect (e.g., deleting one side entirely when both sides should be integrated).
- you are unable to determine the correct resolution for a conflict and cannot make a reasonable best-effort attempt.

if you cannot resolve the conflicts, say so explicitly.  do not push a broken resolution.  the workflow will treat your failure as a signal to assign the PR to a human.

## guidelines

- prefer the simplest resolution that preserves the intent of both sides.
- when in doubt about the intent of a change, preserve both sides and integrate them.
- do not make unrelated changes to files.  only modify what is necessary to resolve the conflicts.
- do not modify files that are not in the conflicting files list unless doing so is required for compilation (e.g., updating an import).
