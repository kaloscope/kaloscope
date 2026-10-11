# ComicInfo schema fixtures

These MIT-licensed schemas were downloaded from the Anansi Project on 2026-10-11.
The original license is preserved in `LICENSE`. Tests validate generated metadata
offline; the application does not load these schemas at runtime.

| Fixture    | Upstream source                                                                               | SHA-256                                                            |
| ---------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `v2.0.xsd` | [Stable 2.0](https://github.com/anansi-project/comicinfo/blob/main/schema/v2.0/ComicInfo.xsd) | `3d9109effff705014f5f6076d92b0ad8c8dad90d993f2592d744c10bad45311c` |
| `v2.1.xsd` | [Draft 2.1](https://github.com/anansi-project/comicinfo/blob/main/drafts/v2.1/ComicInfo.xsd)  | `c7a925b75a5297ec66b19898f59597077326a6b812d5ce77b3035fc179d75ed9` |

`Tags` and `GTIN` require the 2.1 draft. Both schemas restrict `CommunityRating`
to one decimal place on a five-point scale. The application deliberately preserves
the original rating precision, so schema checks use exactly representable scores.
