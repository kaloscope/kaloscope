# Workflow context

Edges order execution, not typed arguments. Fields live in `node.data` beside `$schema`; examples are documentation only. Read `list_node_types` for node behavior.

Use `{{ keyword }}`, not `{{ context.keyword }}`. Precedence: `ks_version` < globals < workflow cache < caller parameters < storage < loop binding.

Updates deep-merge objects, append new list elements, and replace scalars. Branch copies merge back; avoid conflicting writes. Python `context.replace(key, value)` replaces whole values; JavaScript assignments merge too.

Example: `{"keyword":"cats"}` -> variables `{"query": {{ keyword|tojson }}}` -> HTTP URL `https://example.test/search?q={{ query|quote }}` -> formatter `{"items": {{ response.text|jsonpath_all('$.items[*]')|tojson }}}` -> search response `{"total": {{ items|length }}, "items": {{ items|tojson }}}`. For pagination, use the upstream total instead.

`$start`: entry; `$manual`: manual run; `$retval`: output; `$auth`: cached login. Scripts can copy reserved keys for ordinary templates; condition/loop raw lookup accepts `{{ $auth.token }}`. `$index`/`$output` are internal state.
