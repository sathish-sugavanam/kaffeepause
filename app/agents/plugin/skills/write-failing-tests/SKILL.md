---
name: write-failing-tests
description: Turn a feature's acceptance criteria into failing tests of public behaviour, one per criterion, each failing on an assertion with a known message. Use as the tester before any implementation exists.
---

# Write failing tests

You are writing the tests first. The coder implements against them later, and the
harness checks your work mechanically (the "red check").

## What the red check requires

1. Every test you add **fails** on the current code, as an assertion failure
   (JUnit `<failure>`), never a crash (`<error>`).
2. Each failure message **contains** the `expected_failure` text you put in the verdict.
3. Every test that passed before still passes.
4. Every new test is listed in your verdict. Unlisted new tests fail the check.

## Steps

1. Read the issue and its acceptance criteria. Find where the behaviour belongs:
   the HTTP route, CLI command or exported function a user of the code would touch.
2. Read the existing tests next to that code. Copy their layout, fixtures, naming
   and helpers. Put new tests where the test command will collect them.
3. Write **one test per criterion**. Name it after the behaviour
   (`test_export_returns_csv_header`), not the implementation.
4. Test through the public surface. Don't assert on private helpers, internal
   call counts or log lines unless the criterion is about them.
5. Make the failure an assertion, not a crash:
   - The function or module doesn't exist yet? Import it inside the test, or look
     it up with `getattr(module, "name", None)` and `assert fn is not None, "<message>"`.
   - A route doesn't exist yet? Request it and assert on the status code: a 404 is
     an assertion failure, not a crash.
   - Give every assertion an explicit message, and use that message (or a stable
     part of it) as `expected_failure`.
6. Run the test command. Confirm each new test fails, and fails for the reason you
   expect. Fix any that error, pass or skip.
7. Write the **interface contract**: exact names, signatures, routes, request and
   response shapes, status codes and error behaviour the tests rely on. The coder
   follows it literally.

## Rules

- Write only test files. Writes elsewhere are denied.
- Don't weaken or delete existing tests.
- No sleeps, network calls to the internet, or order-dependent tests.
- If a criterion can't be tested from the outside, say so in `gaps`, and test the
  closest observable behaviour.
- Test IDs in the verdict look like `path/to/test_file.py::test_name`
  (for classes, `path::Class::test_name`).
