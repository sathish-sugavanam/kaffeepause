# Express / Node pack

- **Injection.** String-built SQL in `query()` calls; `child_process.exec` or `execSync` with user input (use `execFile` with an args array); `eval`, `new Function`, `vm.runInThisContext`.
- **NoSQL injection.** `req.body` passed straight into a Mongo filter lets `{"$ne": null}` through. Cast or validate the types.
- **Prototype pollution.** Deep merges (`lodash.merge`, `Object.assign` over parsed JSON) of user objects into config or defaults. `__proto__` and `constructor` keys.
- **Auth middleware.** New routes registered before the auth middleware, or on a router that doesn't use it. Missing ownership checks on `/:id` routes.
- **Path traversal.** `res.sendFile`, `fs.readFile` or `path.join(base, req.params.x)` without normalising and checking the prefix.
- **XSS.** Template engines with unescaped output (`<%- %>` in EJS, `{{{ }}}` in Handlebars, `!=` in Pug). `res.send` of user HTML.
- **CORS and headers.** `cors({ origin: true, credentials: true })` or a reflected origin with credentials. Missing `helmet` on new apps.
- **SSRF.** `fetch`/`axios` to a URL taken from the request without an allowlist.
- **Secrets.** Tokens in `console.log`, error responses that return `err.stack`.
- **JWT.** `jwt.verify` without `algorithms`, `jwt.decode` used as if it verified, secrets from code.
