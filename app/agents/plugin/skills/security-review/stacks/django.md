# Django pack

- **ORM bypass.** `raw()`, `extra()`, `RawSQL`, `cursor.execute` with f-strings or `%` formatting. Parameters must go in the `params` argument.
- **Object scoping.** `Model.objects.get(pk=...)` in a view without filtering by `request.user` (or a permission check) is an IDOR. Prefer `get_object_or_404(Model, pk=pk, owner=request.user)`.
- **Auth on views.** New views need `@login_required`, `LoginRequiredMixin`, `@permission_required`, or DRF `permission_classes`. A DRF `AllowAny` on a write endpoint is a finding.
- **CSRF.** `@csrf_exempt` on a cookie-authenticated, state-changing view is blocking.
- **Templates.** `|safe`, `mark_safe()`, `format_html` with unescaped input, and `{% autoescape off %}` are XSS sinks.
- **Mass assignment.** A `ModelForm` or DRF serializer with `fields = "__all__"` exposes fields such as `is_staff` or `owner`.
- **Redirects.** `redirect(request.GET["next"])` without `url_has_allowed_host_and_scheme` is an open redirect.
- **Settings.** `DEBUG = True`, `ALLOWED_HOSTS = ["*"]`, a hard-coded `SECRET_KEY`, or `SESSION_COOKIE_SECURE = False` in production settings.
- **Files.** User-supplied names in `open()` or `FileSystemStorage` paths, and uploads served from the app domain without a content-type check.
- **Deserialisation.** `pickle.loads`, and `yaml.load` without `SafeLoader`, on request data or cache values.
