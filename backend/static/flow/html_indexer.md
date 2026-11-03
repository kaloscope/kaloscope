# HTML download indexer

Category `indexer`, server HTML only; no JS/CAPTCHA. Login: `indexer_auth`. `include_nodes=true` supplies schemas/context. Read once; reserve 6 of 16 calls for incremental construction and validation, starting with `search_start`. Use observed filters or `filters: {}`.

Probe: `html_sample` shows forms, first table headers/row and pagination. `observed_rows` is that response's count, not proven capacity or total. Clipped text does not mean blocked. Check `body_complete`; extract exact values with formatter (8000-byte limit). Row markup: `{{ (response.text|xpath_first('//tr[td]')|outer_html)[:2000]|tojson }}`. XPath values: `/@href` or `string(.)`; elements need `outer_html`. Parenthesize filtered slices: `(value|filter)[:3]`.

Search HTTP: GET, keyword `quote('')`, observed page parameter, httpx client. Return `cards` with observed IDs/titles/download links. Cell text: XPath `string(./td[4])`. Relative URLs: `response.url.join(href)|string` (curl_cffi URL is text).

Fixed source pages: verify capacity `N`. Wire `search_start -> loop -> search_end`, loop_start -> http -> loop_continue. Loop `{{ range(((page_num-1)*page_size)//N+1, (page_num*page_size-1)//N+2)|list }}`, varname `source_page`. HTTP returns ALL page rows as `cards`; context appends arrays. End slice: `cards[((page_num-1)*page_size)%N:((page_num-1)*page_size)%N+page_size]`. Handle small/cross-boundary pages and empty cards. Collect HTTP/login errors across iterations; any error fails search.

Detail-only links: second loop over requested slice, varname `card`; GET `{{ card.detail }}`; return `details` keyed by `card.id`, each with `link` (magnet or absolute torrent URL).

search_end: `{"error":"login_required"}`/`{"error":"status 503"}` without items on failure; otherwise requested items with own link or `(details|default({})).get(card.id, {}).link`. Empty: `{"items":[]}`. Omit unknown total; never invent IDs/totals.

Validate exact templates: `response` fixture `{text,url,status_code}`; end mode=search, `downloads: true`, page_num/page_size. Check first/next (`previous_ids`), small/cross-boundary, empty, login/error, missing optional and invalid/relative links. Download links must be magnet/BT hash/absolute torrent URL. Fix/recheck twice; report unresolved issues. Validate the full graph last. Shape is not runtime proof; Keep/Revert before user publication.
