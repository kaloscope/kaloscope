# Template functions

JSONC example: `{"title": {{ title|tojson }}}`. Fields render text; condition/loop expressions preserve types. Missing values may be empty or raise; AI prompts are strict.

- `jsonpath_first(expr)` / `jsonpath_all(expr)`: JSON text/object -> first match or null / list.
- `xpath_first(expr)` / `xpath_all(expr)`: HTML/XML, namespaces removed; same forms, also scalars. `/text()` or `/@href` extracts text.
- `regex_first(expr)` / `regex_all(expr)`: first capture or null / matches (tuples for multiple captures). Parentheses required for first; DOTALL/MULTILINE.
- `jsonpath(expr, limit='auto')`, `xpath`, `regex`: null/scalar/list for zero/one/many. `'first'`/`'all'` force form; integer >0 slices a list, <=0 selects first.
- `trim(chars=None)`, `ltrim`, `rtrim`: deep-strip strings. `{{ ' x '|trim }}` -> `x`.
- `json_escape`: escape contents; `tojson` also quotes values.
- `size`: bytes; falsy -> empty. `{{ 2048|size }}` -> `2 KB`.
- `duration(unit='milliseconds')`: also seconds/minutes. `{{ 90000|duration }}` -> `01:30`.
- `year(tz=None)`: date/timestamp year, else four-digit match or null. Numeric strings are timestamps: `{{ '2024'|year(tz=0) }}` -> `1970`.
- `strftime(format='%Y-%m-%d %H:%M:%S', tz=None)`: dates/timestamps; invalid text unchanged. Both helpers accept Unix seconds/milliseconds; tz offsets hours for timestamps/zoned strings. Default: local timestamps, unchanged naive dates.
- `s2t`, `t2s`: simplified/traditional Chinese.
- `b64encode`, `b64decode`: UTF-8 Base64. `{{ 'hi'|b64encode }}` -> `aGk=`.
- `prefix(value, strict=True)`, `suffix(value, strict=True)`: add text; falsy operands -> empty unless strict=false.
- `quote(safe='/')`: URL encoding; `quote('')` encodes slashes.
- `query_param('key=value')`: append absent keys, preserve existing values/fragments.
- `parent_path(levels=1, resolve=False)`: parent directory; optionally resolve.

Tests `path is file` / `path is dir` inspect server files. Standard Jinja filters apply; no URL fetching.
