# Rails pack

- **SQL.** `where("name = '#{params[:name]}'")`, `order(params[:sort])`, `find_by_sql` and `pluck` with interpolation. Use placeholders, or allowlist the sort columns.
- **Strong parameters.** `params.permit!`, or permitting `:role`, `:admin` or `:user_id` in a user-facing controller.
- **Scoping.** `Model.find(params[:id])` instead of `current_user.models.find(params[:id])` is an IDOR.
- **Auth filters.** New controllers or actions skipped by `skip_before_action :authenticate_user!`, or missing an authorisation call (Pundit `authorize`, CanCan `authorize!`).
- **CSRF.** `skip_forgery_protection` or `protect_from_forgery with: :null_session` on cookie-authenticated controllers.
- **XSS.** `html_safe`, `raw()` and `<%==` on user data. `link_to` with a user URL can be a `javascript:` link.
- **Commands and files.** `system`, backticks, `%x()`, `Open3` with interpolation. `send_file` or `File.read` with user paths.
- **Deserialisation.** `Marshal.load`, `YAML.load` (use `safe_load`), and `constantize` or `send` on user strings.
- **Redirects.** `redirect_to params[:return_to]` without `allow_other_host: false`.
