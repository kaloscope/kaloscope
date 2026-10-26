# Indexer password login

Adds `auth_start -> http -> auth_end` to `json_indexer`; context is tight, so read only the auth_start/auth_end/http schemas (mode=search/details enforce the other contracts). Supports username/password login returning a token or setting a session cookie; not CAPTCHA, QR, OAuth, browser login or refresh. Need login URL/method/body fields, a success condition beyond HTTP 200, name/token paths and a failure sample; cookie logins also need Set-Cookie name/Domain/Path. Never guess; list missing items.

Config: `login: {mode: password, required: true}`; cookie logins add `cookie: {domain, path, name}` as stored: `Domain=d` -> `.d`, else the lowercase login host (httpx: dotless `h` -> `h.local`); no Path -> the login URL path before its last `/` (`/a/login` -> `/a`). Pick a cookie set only after login.

Login http: POST with header `Content-Type: application/json` or `application/x-www-form-urlencoded` and a JSONC object body, e.g. `{"user": {{ username|tojson }}, "pass": {{ password|tojson }}}`. Formatter: `{"login": {{ response.text|tojson }}, "status": {{ response.status_code }}}`.

auth_end maps both outcomes: `{% set b = login|jsonpath_first('$') %}{% if status == 200 and b.code == 0 %}{"name": {{ b.data.name|tojson }}, "token": {{ b.data.token|tojson }}}{% else %}{"name": ""}{% endif %}`. Add integer `expires_in` only if stated. Validate with mode=auth and config (the exact auth_start config) on success and failure samples; checks are stricter than runtime.

Search/details: header `Authorization: Bearer {{ var('$auth').token }}` (optional login: `{{ var('$auth', {}).token|prefix('Bearer ') }}`); cookie sessions need none. Never write tokens, cookies or passwords into fields; test with a placeholder `$auth`.

Probes never log in: workflow_id only borrows another indexer's saved login for hosts in its HTTP URLs (redirects are returned, not followed), and drafts never inherit `$auth`. No real credentials in tool arguments. Fix and recheck at most twice; preview all paths via Keep/Revert. The user publishes, then logs in on the search page.
