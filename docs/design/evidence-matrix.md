# Evidence and provider matrix

Status: working design, verified 2026-07-10

Support levels used below:

- **Documented**: a supported public contract is documented by the provider.
- **Provisional**: publicly reachable but undocumented or schema-derived.
- **Contractual third party**: documented API whose terms and attribution apply.
- **Local heuristic**: useful local state without a stable public schema.
- **Human only**: linkable reference, prohibited or unsuitable for automation.

| Capability | Preferred source | Level | Important limitations |
| --- | --- | --- | --- |
| Owned games/playtime | Steam `IPlayerService/GetOwnedGames` | Documented | Requires key and visible game details; free played games need an option; private games can be omitted |
| Recent play | Steam `GetRecentlyPlayedGames` | Documented | Visibility applies; a short window is not a preference verdict |
| Friends/profile | Steam `ISteamUser` | Documented | Private friend lists fail; no consent should be inferred from public visibility |
| Achievements/stats | Steam `ISteamUserStats` | Documented | Per-game support varies; hidden achievements need care |
| Store catalog identity | Steam `IStoreService/GetAppList` | Documented | Returns catalog/type/change signals, not rich metadata or price history |
| Wishlist | Valve `IWishlistService` | Provisional | Missing from Valve's supported reference; share-token/auth behavior varies |
| Rich store metadata/current price | Storefront `api/appdetails` | Provisional | Undocumented shape/rate limits; country and language affect results |
| Store reviews | Steam store review API | Documented | Review windows and filters must be recorded |
| Installed games | `libraryfolders.vdf` + `appmanifest_*.acf` | Local heuristic | Installed is not owned; file formats are not stable public APIs |
| System profile | OS-native probes | Local observation | Minimize collection and redact device identifiers by default |
| Price snapshots | This CLI | Local observation | Starts at first sync; region/currency/package identity are mandatory |
| Full timestamped historical/cross-store price | ITAD, if permission is obtained | Contractual third party | `games/history/v2`; private apps must contact ITAD; attribution/link/competition terms apply |
| Exact App/Sub/Bundle historical-low summary | GG.deals, if terms are agreed | Contractual third party | API exists; scraping prohibited; full price graph not exposed publicly |
| Commercial multi-currency history | Sensor Tower/VGI | Contractual third party | Enterprise/custom contract and likely cost |
| Limited USD deal fallback | CheapShark | Contractual third party | USD only, not a full history; use redirect links and avoid bulk catalog caching |
| Euro-oriented low/bundle/free/subscription history | pepe.deals | Provisional third party | Public API, but license/rates/redistribution need clarification |
| Default artwork | Steam CDN references | Provisional/public asset | Adequate for core display; respect origin and avoid treating art as game facts |
| Custom artwork | SteamGridDB, opt in | Contractual third party | Personal/non-commercial and attribution/licensing questions; not canonical metadata |
| Deck status | Valve compatibility review | Documented concept, provisional retrieval | Strong for Deck/SteamOS target only; machine retrieval is undocumented |
| SteamDB history | SteamDB page link | Human only | No public API; scraping/crawling is prohibited |
| License-aware private data | SteamKit/SteamCMD, later high-trust mode | Higher-trust integration | Session secrets, account risk, and complexity exceed core mode |

## Corrections to the initial handoff

1. `IStoreService/GetAppList` is an officially documented, paginated catalog API.
   It includes `last_modified` and `price_change_number`, enabling incremental
   catalog/change detection. It does not itself provide the price.
2. Wishlist access is more fragile than the handoff implies. Treat it as a
   capability-probed provisional adapter, not a supported contract.
3. `appdetails` is an undocumented storefront endpoint, not a supported Steam
   Web API. It needs isolation, caching, pacing, and contract tests.
4. ITAD cannot be assumed available for a private application; current terms say
   private apps should contact ITAD. Local snapshots must work without it.
5. SteamGridDB is optional presentation enrichment. Steam CDN art is enough for
   the core, with fewer terms and attribution surfaces.
6. A local manifest proves local install state, not ownership. Conversely, the
   owned-games API can omit individually private titles.

## Provider policy

- Every observation stores provider, retrieval time, effective time when known,
  region/language context, support level, and the raw-cache reference.
- Normalized facts never erase conflicting observations.
- Adapters expose capabilities at runtime; missing auth, privacy, rate limits,
  and unsupported fields are typed states.
- Provisional adapters are contract-tested and can be disabled independently.
- Provider failures do not corrupt the last known-good normalized snapshot.
- API keys are bring-your-own, read from environment/keychain/private config,
  and never accepted on the command line.
- Reference URLs carry an access mode; manual viewing never implies permission
  for agent/browser extraction.

## Primary references

- [Steam IPlayerService](https://partner.steamgames.com/doc/webapi/iplayerservice)
- [Steam IStoreService](https://partner.steamgames.com/doc/webapi/IStoreService)
- [Steam Web API authentication](https://partner.steamgames.com/doc/webapi_overview/auth)
- [Steam Web API terms](https://steamcommunity.com/dev/apiterms)
- [Steam store reviews API](https://partner.steamgames.com/doc/store/getreviews)
- [Steam tags](https://partner.steamgames.com/doc/store/tags)
- [Steam accessibility features](https://partner.steamgames.com/doc/accessibility_features)
- [Steam hardware compatibility review](https://partner.steamgames.com/doc/steamhardware/compat)
- [Steam Families FAQ](https://help.steampowered.com/en/faqs/view/054C-3167-DD7F-49D4)
- [xPaw wishlist service reference](https://steamapi.xpaw.me/IWishlistService)
- [IsThereAnyDeal API](https://docs.isthereanydeal.com/)
- [CheapShark API](https://apidocs.cheapshark.com/)
- [SteamGridDB API](https://www.steamgriddb.com/api/v2)
- [SteamDB FAQ](https://steamdb.info/faq/)
