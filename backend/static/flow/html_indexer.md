# HTML download indexer

Category `indexer`, server HTML only; no JS/CAPTCHA. Login: `indexer_auth`. Self-contained: read no other reference or node schema; every field used is a plain `data` key named here, defaults fill the rest. Earlier reasoning may be dropped between calls; the canvas and results stay, so decide once and never re-derive.

Rounds; put every call of a round in one reply:

1. Probe the search URL with a broad sample keyword or an empty one, so results span pages. `html_sample` shows forms, the first row and pagination; `observed_rows` counts that response only and is the capacity only when pagination exists. Otherwise retry once with a broader keyword. Clipped text does not mean blocked.
2. Preview GRAPH with every node and edge (N=observed_rows, URL from the observed form and page parameter; omit formatter/response). Probe page 2 with formatter `{"rows": {{ response.text|xpath_all(ROW)|length }}, "row": {{ (response.text|xpath_first(ROW)|outer_html)[:2000]|tojson }}}`. No rows there means too few results, not a broken selector: use a broader keyword.
3. Preview set_field `fetch.formatter`=FORMATTER and `end.response`=RESPONSE, plus `pages.expression` only if page 2 rows differ from N. Probe page 2 with the exact FORMATTER; a long list returns samples plus `omitted_items`.
4. Check ids, titles and links in that probe. validate_workflow_template RESPONSE, mode=search, context `{cards: two real cards from that probe, errors: [], downloads: true, page_num: 1, page_size: 2}`; never invent fixture links. validate_workflow on the latest snapshot. Then reply.

Preview results outline the current canvas: do not reread it. Fix/recheck failures twice with set_field; report unresolved issues. Shape is not runtime proof; Keep/Revert before user publication. Run only on request; otherwise say it was not run.

GRAPH (id, $schema, x,y):
start, search_start, 0,0: config `display:\n  page_size: 20\n  view_modes:\n    - table\nkeyword:\n  global: true\n  required: false\nfilters: {}\n` (add observed filters only if requested).
pages, loop, 800,0: expression `{{ range(((page_num-1)*page_size)//N+1, (page_num*page_size-1)//N+2)|list }}`, varname `source_page`.
fetch, http, 800,600: url `URL?q={{ keyword|default('',true)|quote('') }}&p={{ source_page }}`.
end, search_end, 1600,0.
Edges source.sourceHandle -> target.targetHandle: start.output -> pages.input; pages.loop_start -> fetch.input; fetch.output -> pages.loop_continue; pages.output -> end.input.

FORMATTER, with ROW, HREF, TITLE, MAGNET, TORRENT and SIZE as quoted XPaths. Rows are real elements, so XPath applies to them. It returns ALL rows; context appends arrays across pages:
`{"errors": [{% if response.status_code != 200 %}"status {{ response.status_code }}"{% endif %}], "cards": [{% for r in response.text|xpath_all(ROW) %}{% set m = r|xpath_first(MAGNET) %}{% set t = r|xpath_first(TORRENT) %}{% if not loop.first %},{% endif %}{"id": {{ r|xpath_first(HREF)|regex_first('([0-9]+)')|tojson }}, "title": {{ r|xpath_first(TITLE)|tojson }}, "link": {{ (m or (response.url.join(t)|string if t else none))|tojson }}, "size": {{ r|xpath_first(SIZE)|tojson }}}{% endfor %}]}`
Rows differ, so select links by attribute, not position. MAGNET `.//a[starts-with(@href,'magnet:')]/@href`; TORRENT `.//a[contains(@href,'.torrent')]/@href`; HREF and TITLE `.//a[contains(@href,'PATH')][not(contains(@href,'#'))]/@href` and `/@title`, PATH being the observed detail path; SIZE `normalize-space(./td[K])`. A bare element is not text: use `/@attr`, `string(.)` or `normalize-space(.)`. Add a login error only for an observed login page.

RESPONSE:
`{% if errors %}{"error": {{ errors[0]|tojson }}}{% else %}{% set o = ((page_num-1)*page_size)%N %}{"items": {{ (cards|default([]))[o:o+page_size]|tojson }}}{% endif %}`
Empty is `{"items":[]}`. Links must be magnet/BT hash/absolute torrent URL. Omit unknown total; never invent IDs/totals.

No fixed capacity: fetch one page without the loop. Detail-only links: second loop after pages over the requested slice, varname `card`; GET `{{ card.detail }}`; return `details` keyed by `card.id`, each with `link`; end uses `(details|default({})).get(card.id, {}).link`.
