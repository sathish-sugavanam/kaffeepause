# Go net/http pack

- **SQL.** `fmt.Sprintf` into `db.Query` or `db.Exec`. Use `?`/`$1` placeholders.
- **Commands.** `exec.Command("sh", "-c", userInput)`. Pass arguments separately, and never through a shell.
- **Paths.** `filepath.Join(base, r.URL.Query().Get("f"))` without `filepath.Clean` and a prefix check. `http.ServeFile` with user paths. Prefer `os.Root` or `http.FS` with a fixed root.
- **Auth.** New `HandleFunc` routes registered on a mux that isn't wrapped by the auth middleware. Missing ownership checks on IDs from the path.
- **Templates.** `text/template` used for HTML (use `html/template`). `template.HTML(userInput)` casts bypass escaping.
- **Limits.** Request bodies read without `http.MaxBytesReader`. Servers without `ReadHeaderTimeout` (slowloris). Unbounded goroutines per request.
- **SSRF.** `http.Get` to user URLs without an allowlist; also check redirects (`CheckRedirect`).
- **Crypto.** `math/rand` for tokens (use `crypto/rand`). `==` on secrets (use `subtle.ConstantTimeCompare`). `InsecureSkipVerify: true`.
- **Errors.** `err.Error()` written to the response can leak internals. Ignored errors on auth or permission checks.
- **Concurrency.** Maps shared across requests without a lock, and check-then-act races on balances or counters.
