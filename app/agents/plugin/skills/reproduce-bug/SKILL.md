---
name: reproduce-bug
description: Write a regression test that reproduces a reported bug on the base branch and fails on an assertion. Use as the tester for bug issues. Ends with cannot_reproduce when the bug can't be reproduced.
---

# Reproduce a bug

A bug fix starts with a test that shows the bug. The harness runs it on the
unfixed code and requires it to fail on an assertion (the "red check").

## Steps

1. Read the report: the steps to reproduce, the expected behaviour and the actual
   behaviour. Each expected behaviour is a criterion.
2. Find the code path. Read it until you can say why the actual behaviour happens.
3. Write the smallest test that drives the public entry point (route, CLI command,
   exported function) with the reported input, and asserts the **expected**
   behaviour. Give the assertion an explicit message, and put that message in
   `expected_failure`.
4. Run it. It must fail now, and the failure must show the reported wrong behaviour,
   not an unrelated error.
5. Add a second test for a nearby case only if the report implies one (another
   input that hits the same faulty branch).
6. In `contract`, state the fix's observable result: what the code must return or
   do for the reported input. Don't prescribe the implementation.

## When you can't reproduce it

If the test passes on the current code after an honest attempt (the right input,
the right code path, the environment the report describes), stop:

- set `cannot_reproduce` to `true`,
- explain in `notes` what you tried and what you saw,
- list the test you wrote, if any, in `tests`.

The run then ends with `CANNOT_REPRODUCE` and a human picks it up. Don't change a
passing test until it fails for a different reason.

## Rules

Write only test files. Don't weaken existing tests. Test IDs look like
`path/to/test_file.py::test_name`.
