---
name: test-gap-review
description: Review the coder's diff against the existing tests and add tests for the cases they miss, such as error paths, permissions, and empty or boundary inputs. Use as the tester after the coder's change passes.
---

# Test gap review

The coder's change passes your first tests. Now look for behaviour the change
added or touched that no test pins down.

## Read the diff for

- **Error paths.** Each new `raise`, error response, early return or `except`:
  is there a test that triggers it and checks the result?
- **Permissions.** New routes or actions: is an anonymous or unauthorised caller
  refused? Can one user reach another user's data?
- **Inputs.** Empty strings, empty lists, `None`/null, zero, negative numbers,
  very long values, unicode, duplicates, and the exact edges of any limit
  (`n`, `n - 1`, `n + 1`).
- **State.** Behaviour on a second call, on missing records, and on concurrent
  updates if the code locks or counts.
- **Contract edges.** Every status code and error shape the contract promises.

## Then

1. Add a test only for a gap that matters to a user of the code. Don't pad
   coverage, and don't duplicate an existing test.
2. Each test asserts what the **criteria and contract** say should happen, not
   just what the code does today. If the code is wrong, your test fails, and the
   harness sends it back to the coder. That's the point.
3. Run the tests. Look at every failure: a failing test must point at a real
   defect in the coder's change, not at a mistake in your test.
4. In the verdict, list only the tests you added in this review, with an empty
   `expected_failure`. Put anything you couldn't test in `gaps`.

Adding nothing is a valid result when the coverage is good.

## Rules

Write only test files. Never edit or delete the earlier tests.
