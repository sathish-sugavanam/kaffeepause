---
name: security-review
description: Review a code change for security problems with an OWASP Top 10 checklist, per-stack packs, and triage of semgrep, gitleaks and osv-scanner output. Reports findings with severity, file and line, and a blocking flag. Use as the security reviewer.
---

# Security review

You review one change: the diff from the base branch. You report findings. You
never edit files.

## 1. Read the scanner reports

The harness already ran the scanners and gave you the report paths. Read each one.

| Scanner | Report | How to triage |
| --- | --- | --- |
| `semgrep` | JSON, `results[]` with `check_id`, `path`, `start.line`, `extra.message` | Keep a result only if the flagged code is in the diff and the pattern is really reachable with attacker input. Rules fire on shapes, not data flow, so check the flow yourself. |
| `gitleaks` | JSON array with `RuleID`, `File`, `StartLine`, `Match` (redacted) | A real secret in the diff is always **blocking**, whatever its type. Test fixtures with obviously fake values (`test`, `example`, `xxx`) are not. |
| `osv-scanner` | JSON, `results[].packages[].vulnerabilities[]` | Report only packages this change added or upgraded. A known-vulnerable version newly introduced is blocking when the vulnerable function is used, or severity is high or critical. |

A report that says "not installed" or "skipped" means you rely on your own review.
You may re-run a scanner on a narrower target if you need to.

## 2. Review the diff against the checklist

Go through every changed file. For each category, ask whether this change
introduces or exposes the problem.

1. **Broken access control.** New routes, views or handlers check authentication
   and authorisation. Object lookups are scoped to the caller (no IDOR). No
   trusting client-supplied IDs, roles or prices.
2. **Cryptographic failures.** No hand-rolled crypto, MD5/SHA-1 for passwords, ECB,
   static IVs, `random` for tokens, or secrets in code, logs or URLs.
3. **Injection.** SQL, shell, LDAP, template, header and log injection. Look for
   string-built queries, `shell=True`, `eval`/`exec`, unescaped template output,
   user data in file paths (path traversal).
4. **Insecure design.** Missing rate limits on auth or costly actions, missing
   limits on size or count, business logic a user can skip.
5. **Security misconfiguration.** Debug on, permissive CORS, missing CSRF protection
   on state-changing routes, verbose error pages, default credentials.
6. **Vulnerable components.** See osv-scanner above.
7. **Authentication failures.** Weak session handling, missing expiry, user
   enumeration through different error messages, tokens compared without
   constant time.
8. **Integrity failures.** Unsafe deserialisation (`pickle`, `yaml.load`,
   `ObjectInputStream`, `Marshal.load`), unsigned webhooks, unverified downloads.
9. **Logging failures.** Secrets or personal data written to logs. Security events
   with no log at all.
10. **SSRF.** Server-side requests to URLs a user controls without an allowlist.

Then open the stack pack for the repo's `stack` field, if there is one:
`stacks/django.md`, `stacks/express.md`, `stacks/rails.md`, `stacks/spring.md`,
`stacks/go-net-http.md`. If the stack is not set or has no pack, use the checklist
alone.

## 3. Report

For each real issue, add a finding:

- `severity`: `critical` (remote code execution, auth bypass, leaked live secret),
  `high` (injection, IDOR, stored XSS), `medium` (reflected XSS, CSRF, missing rate
  limit), `low` (hardening), `info` (a note).
- `file`, `line`: where it is, in the change.
- `title`: one line.
- `blocking`: `true` only when the problem is real, exploitable, and introduced or
  exposed by this change. Pre-existing issues and hardening ideas are not blocking.
- `fix_hint`: a concrete fix the coder can apply.

A blocking finding goes back to the coder, so make it precise enough to fix without
asking. In `summary`, say what you reviewed and which scanner results you dismissed
and why. No findings is a valid result.
