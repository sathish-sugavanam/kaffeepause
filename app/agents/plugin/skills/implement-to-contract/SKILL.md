---
name: implement-to-contract
description: Implement a feature or fix so the tester's failing tests pass, following the interface contract and never editing tests. Dispute a test in the verdict when it is wrong. Use as the coder.
---

# Implement to contract

The tester has written failing tests and an interface contract. Make the tests pass
with a focused change that a reviewer would accept.

## Steps

1. Read the failing tests and the contract first. They are the specification.
2. Read the code around where the change belongs. Follow its structure, naming,
   error handling and style. Reuse existing helpers before writing new ones.
3. Implement the smallest change that satisfies the contract and the issue. No
   unrelated refactors, renames or formatting sweeps.
4. Run the test command. Then run every lint command and fix what it reports.
5. Before you finish, read your own diff (`git diff`). Remove debugging code, stray
   files and anything the issue didn't ask for.

## Never edit tests

Test files belong to the tester. Edits through `Write`/`Edit` are denied, and a
change made any other way (Bash, `sed`, scripts) fails the whole run with
`TESTER_FILES_CHANGED`. Don't change test configuration to skip or deselect tests
either: the harness checks that every expected test still runs.

## When a test is wrong

If a test contradicts the acceptance criteria, contradicts another test, or can't
pass without breaking documented behaviour, **dispute it** instead of working
around it. Add an entry to `disputes` with:

- `test_id`: the test,
- `reason`: what is wrong with it, concretely,
- `criterion`: the acceptance criterion it should check.

A dispute discards your implementation and sends the tests back to the tester, so
use it only when a test is really wrong, not when it is merely hard to pass.

## Verdict

- `summary`: what you changed and why, in a few sentences. It becomes the pull
  request description.
- `files_changed`: the paths you changed.
- `disputes`: empty unless you are disputing.
