# HTML movie metadata scraper

Category `ingest`; start with `ingest_start -> http -> movie`, using `output`/`input` handles. Read their schemas/examples. Search inputs include title/year/page_num/page_size; use supplied URLs and `quote('')`. A1 creates the empty draft, then generation uses its canvas snapshot.

Probe the requested HTML. Check status/completeness; do not parse an error/login page as success. HTTP formatter can return `{"html": {{ response.text|tojson }}}`. Downstream templates read html, not response. For large HTML, extract needed fields in the formatter instead of returning raw HTML past the 8000-byte result cap. Extract strings with XPath `/text()`/`/@...`, or `string(...)`; select repeated cards then loop. Serialize values with `tojson`. If search cards only contain links, inspect detail HTML before claiming complete metadata.

Validate the exact movie template with `mode=movie`: array of candidates, [] for no match. title is required; year is an integer, rating 0..10, list fields are arrays. Omit unknown optional data; do not invent IDs, years or ratings. Use the movie node example's NFO field names. Test matching, missing-optional and no-match HTML samples. The checker also uses the real movie NFO render/read-back logic, without writing files.

Fix reported fields and recheck, at most twice. Preview the tested templates through Keep/Revert. Automatic ingestion writes only the first candidate, so ordering matters; validation cannot prove a correct match. Do not bind a library, write metadata, publish or execute. List unresolved selectors, credentials and source coverage.
