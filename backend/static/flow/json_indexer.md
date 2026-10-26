# Paginated JSON indexer

Category `indexer`; two separate paths: `search_start -> http -> search_end` and `details_start -> http -> details_end`. Read their schemas/examples; use `output`/`input` handles. A1 creates an empty draft first; build on its canvas snapshot. Login: read `indexer_auth`.

Probe only supplied endpoints. Encode `keyword`/remote `id` with `quote('')`; map 1-based `page_num`/`page_size` to the API's pagination. Inspect status and completeness before extraction. HTTP formatter returns an object (e.g. `{"payload": {{ response.text|tojson }}}`); its keys enter downstream context. The end template reads those keys, not `response`. Use `tojson` for strings/collections. Use small sample pages; extract needed fields in the formatter if raw JSON exceeds the 8000-byte result cap.

Validate the exact search template with `mode=search` on first, next and empty samples, supplying page_num/page_size. IDs/titles must be nonblank strings; total counts all matches, never this page. Use observed totalPages or omit unknown totals. Check IDs change across pages. Validate details with `mode=details`, including not-found (`null`) and chapter lookup if applicable. Supply media_type defaults explicitly; playback `url` differs from download `link`.

Fix field issues and recheck, at most twice. Unknown URLs/credentials/fields stay unresolved. Preview both paths with the tested templates; explain sample coverage and remaining gaps. No save/publish/execute or download-plan creation. Keep/Revert uses the normal canvas flow.
