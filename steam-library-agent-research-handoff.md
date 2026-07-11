# Research handoff: Agent-readable Steam library / wishlist / deal-intelligence CLI

Generated: 2026-07-10 15:42 MDT  
Purpose: one giant copy-paste research brief / prompt for a model to continue design or implementation after the next model release.

---

## Copy-paste prompt

You are being handed off research for a project: design and/or implement a local CLI/data layer that lets AI agents answer useful questions about a user's Steam library, wishlist, installed games, pricing, historical deals, and game artwork. Treat this as a research-backed product/design task first; do not jump straight into code before validating data sources, access constraints, and the desired user questions.

The user wants a deterministic, reusable local interface for agents rather than having every agent rediscover Steam docs, scrape websites, or operate an authenticated browser session. The intended final architecture is:

```text
Steam Web API / Steam Store APIs / local Steam files / third-party APIs
        ↓
small local CLI or wrapper around existing exporter/MCP projects
        ↓
local cache: JSON and/or SQLite
        ↓
Hermes skill documents commands, cache paths, privacy caveats, and workflows
        ↓
agents query CLI/cache/MCP to answer high-level gaming questions
```

The chosen future direction is: **make a CLI**, but evaluate existing tools first and reuse/fork/wrap where sensible. MCP can be added later if multiple agent platforms need typed live access, but the first stable deliverable should be an inspectable CLI + cache.

Frame the work around real agent questions people will ask, especially:

> “Go through my wishlist — what are the best deals right now?”

To answer that well, the system must join wishlist access, owned library access, current pricing, historical price/deal context, installed status, preference signals, reviews/metadata, and artwork.

---

## Primary research conclusions

### 1. Canonical owned-library source

Primary source for owned Steam games:

```text
Steam Web API: IPlayerService/GetOwnedGames
```

Use this as the canonical source for the user’s owned library and playtime when Steam profile/game-details visibility allows it.

Known caveat: `IPlayerService/GetOwnedGames` depends on the user's Steam profile/game-details/library privacy. If game details are private, owned-library sync may be incomplete or fail. The CLI should detect this and report a clear visibility/privacy error rather than silently returning misleading results.

### 2. Wishlist access exists, but with caveats

Steam wishlist access appears to be available via:

```text
Steam Web API: IWishlistService/GetWishlist
Endpoint pattern: https://api.steampowered.com/IWishlistService/GetWishlist/v1/?steamid=STEAMID64
```

Research source: xPaw’s Steam Web API reference page for `IWishlistService` (`https://steamapi.xpaw.me/IWishlistService`) lists `GetWishlist`, `GetWishlistItemCount`, and other wishlist endpoints. Some endpoints are marked undocumented or require keys/share tokens/auth. `GetWishlist` is listed as a GET endpoint that takes `steamid`.

Design implication:

- Support public/shared wishlist access first.
- Treat private/authenticated wishlist support as a later, explicit, higher-trust mode.
- Do not default to browser cookies or authenticated browser automation.
- The CLI should expose wishlist sync separately, e.g. `steamlib sync-wishlist`.

### 3. Current Steam pricing is available, historical Steam app price history is not

Steam Store API can provide current price data:

```text
https://store.steampowered.com/api/appdetails?appids=APPID&filters=price_overview
```

Useful fields include:

- currency
- initial price
- final/current price
- discount_percent
- formatted prices

But Steam/Valve does **not** provide a full historical price API for game/store app prices. The Steam Community Market `pricehistory` endpoint is for market items, not store game prices.

Design implication:

- Use Steam Store APIs for current pricing/discounts.
- Maintain local price snapshots going forward if we want our own Steam-specific history.
- Use third-party deal APIs for historical lows/backfill.

### 4. SteamDB is valuable but should not be a machine dependency

SteamDB is valuable mainly for human-readable historical price information, app history, charts, and reference pages.

However:

- SteamDB says it has **no public API**.
- SteamDB discourages scraping/crawling and says automated access may get banned.
- Its FAQ recommends getting Steam data directly from Steam Web API / SteamKit / related tools.

Design implication:

- Do **not** build the CLI around scraping SteamDB.
- Store SteamDB URLs as reference links only, e.g. `https://steamdb.info/app/APPID/`.
- If a user asks for SteamDB-specific historical price information, label it as not available through supported machine access unless they manually inspect the link.

### 5. IsThereAnyDeal is the best legitimate historical/deal API candidate

IsThereAnyDeal (ITAD) has an API:

```text
Docs: https://docs.isthereanydeal.com/
Base URL: https://api.isthereanydeal.com
```

It can provide:

- current best prices
- historical lows
- deals
- price/deal history
- shops/country parameters
- game lookup/mapping

Research notes:

- API key / OAuth depending on endpoint.
- Default verified-account rate limit found in docs summary: 1000 requests per 5 minutes.
- Respect terms: do not imply affiliation, provide attribution/link when appropriate, do not alter provided data improperly, do not build a direct competitor to ITAD.

Design implication:

- Use ITAD for historical low/deal context when acceptable.
- Keep provider attribution and URLs in result objects.
- Cache ITAD mappings and responses.
- Separate “Steam-only current price” from “cross-store best deal.”

### 6. SteamGridDB has an official API and should be first-class for artwork

SteamGridDB API v2 exists:

```text
Docs/base: https://www.steamgriddb.com/api/v2
Auth: Authorization: Bearer <STEAMGRIDDB_API_KEY>
```

It supports:

- game lookup/search
- resolving games by external platform IDs, including Steam AppIDs
- fetching artwork assets:
  - grids
  - heroes
  - logos
  - icons
- filters:
  - static / animated
  - dimensions
  - styles
  - NSFW / humor / epilepsy filtering
  - pagination

Design implication:

- Integrate SteamGridDB as the primary art/presentation provider.
- Store SGDB IDs and selected asset URLs in the local cache.
- Provide safe defaults: static assets, exclude NSFW/humor/epilepsy unless explicitly requested.

### 7. Local Steam files only answer installed-game questions

Local Steam `steamapps/appmanifest_*.acf` files can tell what is installed on a specific machine and some install metadata.

Design implication:

- Use local appmanifest scans for `installed=true`, install path/library folder, disk usage if available.
- Do not treat local manifests as the full owned library.
- Provide `steamlib scan-installed` as a separate command.

### 8. Browser automation should be fallback-only

Authenticated browser automation could access private Steam pages, but it is brittle and high-trust.

Design implication:

- Avoid browser automation as a primary path.
- If ever added, make it explicit, interactive, and scoped.
- Prefer API keys, public/shared data, and local files.

---

## Existing GitHub options found

Evaluate these before building everything from scratch.

### `jkiley129/steam-mcp`

URL: https://github.com/jkiley129/steam-mcp

Research summary:

- TypeScript MCP server.
- MIT license found in repo metadata/package inspection.
- Focus: exposing Steam library to agents.
- README lists tools:
  - `get_library`
  - `get_recently_played`
  - `search_library`
  - `get_game_details`
  - `refresh_library`
- Requires Node.js 18+, Steam Web API key, public Steam profile/library.
- Good first MCP evaluation candidate.

Recommendation:

- Use as first reference for agent-facing interface shape.
- Potentially wrap/fork if CLI wants similar behavior but deterministic JSON output.

### `FunnyEntity/steam_library_exporter`

URL: https://github.com/FunnyEntity/steam_library_exporter

Research summary:

- Python exporter.
- MIT license found.
- Exports CSV, JSON, SQLite.
- Enriches via multiple APIs:
  - Steam Web API
  - Steam Store API
  - Reviews API
  - SteamSpy
- Has GUI/web bits too, but export/cache functionality is highly relevant.
- Good cache/export base.

Recommendation:

- Evaluate as the closest match for our durable local cache approach.
- Could be used as a base, dependency, or inspiration for schema/enrichment.

### `obrien-matthew/mcp-steam`

URL: https://github.com/obrien-matthew/mcp-steam

Research summary:

- Python MCP server.
- MIT license found.
- Broader focus: library, achievements, stats, store discovery.
- Requires Python 3.14+ and uv.
- User’s macOS has uv installed, Python 3.11 reliable; uv-managed Python could handle 3.14 if needed.

Recommendation:

- Good richer MCP candidate.
- Potential friction from Python 3.14 requirement.

### `imnotStealthy/steam-mcp`

URL: https://github.com/imnotStealthy/steam-mcp

Research summary:

- TypeScript MCP server.
- Exposes Steam Web API tools to Claude Code, Claude Desktop, Gemini CLI.
- Covers profiles, game library, achievements, VAC bans, store search, and slash commands.
- Install scripts available.
- License was not clearly surfaced in quick inspection; verify before reusing/forking code.

Recommendation:

- Useful reference for broader Steam API surface.
- Do not reuse code until license is clear.

### `dsp/mcp-server-steam`

URL: https://github.com/dsp/mcp-server-steam

Research summary:

- Java/Docker MCP server.
- MIT license found.
- Docker recommended.
- More heavyweight than needed for a small personal CLI.

Recommendation:

- Consider only if a containerized always-on service becomes desirable.

### Smaller/simple scripts

Useful examples but probably not ideal bases:

- `phoenixweiss/steamgames-exporter`
- `legendsciber/steam_library_lister`
- `landonrobinson/steam-library-exporter`
- `davidmalko87/steam-library-exporter` — simpler/stable exporter, likely related to/preceded FunnyEntity version.

---

## Target product question: wishlist best deals

The canonical demo question should be:

> “Go through my wishlist — what are the best deals right now?”

A good answer should combine:

- wishlist items and metadata
- current Steam price and discount
- historical low / all-time low / store low
- whether this is a good time to buy compared with historical pricing
- whether the user already owns the game
- whether it is installed
- whether the user tends to like similar games, inferred from owned-library playtime
- reviews/ratings and store metadata
- Steam Deck/controller support if available from a chosen metadata source
- whether better non-Steam deals exist, if cross-store deals are allowed
- user preference for Steam-only vs any authorized store
- visual presentation using SteamGridDB artwork
- links to Steam, SteamDB, ITAD, SteamGridDB as appropriate

Potential ranking features:

```text
score =
  current_discount_strength
  + historical_low_nearness
  + wishlist_priority
  + wishlist_age
  + affinity_to_played_genres_or_tags
  + review_quality
  + deck/controller fit
  - already_owned_penalty
  - too_expensive_penalty
  - weak_discount_penalty
```

The CLI should return raw evidence and not only a final recommendation, so agents can explain why a deal is good.

---

## Proposed CLI commands

Initial sync/cache commands:

```bash
steamlib sync-owned             # Steam Web API IPlayerService/GetOwnedGames
steamlib sync-wishlist          # Steam Web API IWishlistService/GetWishlist
steamlib scan-installed         # local Steam appmanifest scan
steamlib enrich-store           # Steam Store appdetails, reviews, tags-ish metadata
steamlib enrich-prices          # ITAD historical lows/deal data + current Steam snapshots
steamlib enrich-art             # SteamGridDB grids/heroes/logos/icons
steamlib sync-all               # run safe sync steps with stale-cache guards
```

Query/report commands:

```bash
steamlib list owned
steamlib list wishlist
steamlib list installed
steamlib deals wishlist
steamlib recommend wishlist-deals
steamlib query "controller games under 10h"
steamlib export --format json
steamlib export --format sqlite
```

Price-specific commands:

```bash
steamlib price current --appid 1245620 --country US
steamlib price history --appid 1245620 --country US
steamlib price sync-current --owned --wishlist --country US
steamlib price deals --appid 1245620 --country US
```

Utility/config commands:

```bash
steamlib doctor
steamlib config show
steamlib cache status
steamlib links --appid 1245620
```

---

## Proposed local data locations

Potential durable data location:

```text
~/library-notes/steam-library/
  README.md
  owned_games.json
  wishlist.json
  installed_games.json
  enriched_games.json
  deals.json
  steam-library.sqlite
```

Potential app config:

```text
~/.config/steamlib/config.toml
```

Secrets must not go in notes, Library docs, Vault docs, skills, or generated reports. Use env/keychain/local private config:

```text
STEAM_API_KEY
STEAMGRIDDB_API_KEY
ITAD_API_KEY
STEAM_ID64
```

If using config files, put secret-bearing config outside public/durable Markdown locations and make sure it is ignored by git.

---

## Proposed cache/schema concepts

### `games`

```sql
games(
  appid integer primary key,
  name text,
  steam_url text,
  steamdb_url text,
  type text,
  release_date text,
  store_metadata_json text,
  review_summary_json text,
  updated_at text
)
```

### `owned_games`

```sql
owned_games(
  appid integer primary key,
  playtime_forever_minutes integer,
  playtime_2weeks_minutes integer,
  last_played_at text,
  source text,
  synced_at text
)
```

### `wishlist_items`

```sql
wishlist_items(
  appid integer primary key,
  priority integer,
  added_at text,
  source text,
  synced_at text
)
```

### `installed_games`

```sql
installed_games(
  appid integer primary key,
  install_dir text,
  library_folder text,
  manifest_path text,
  size_on_disk integer,
  scanned_at text
)
```

### `steam_price_snapshots`

```sql
steam_price_snapshots(
  appid integer,
  country text,
  currency text,
  initial integer,
  final integer,
  discount_percent integer,
  captured_at text,
  primary key(appid, country, captured_at)
)
```

### `external_price_history`

```sql
external_price_history(
  appid integer,
  provider text,          -- e.g. itad
  provider_game_id text,
  shop text,
  country text,
  currency text,
  price integer,
  regular_price integer,
  historical_low integer,
  timestamp text,
  url text
)
```

### `steamgriddb_assets`

```sql
steamgriddb_assets(
  appid integer,
  sgdb_game_id integer,
  asset_type text,        -- grid, hero, logo, icon
  asset_id integer,
  url text,
  thumb_url text,
  style text,
  dimensions text,
  tags_json text,
  selected boolean,
  fetched_at text
)
```

---

## Data-source details and caveats

### Steam Web API: `IPlayerService/GetOwnedGames`

Purpose:

- Owned games
- App IDs and names if requested
- Playtime forever
- Recently played-ish fields depending on endpoint/options

Caveats:

- Depends on visibility/privacy.
- Requires Steam API key for many standard uses.
- Should fail clearly if profile/library details are private.

### Steam Web API: `IWishlistService/GetWishlist`

Purpose:

- User wishlist by SteamID
- App IDs and wishlist metadata such as priority/added time, depending on response

Caveats:

- Some wishlist endpoints are undocumented.
- Private wishlist behavior needs testing.
- Public/shared support first.

### Steam Store API: `appdetails`

Purpose:

- Current store metadata
- Current price/discount
- Genres/categories
- Screenshots/movies/store package data depending on filters

Caveats:

- Unofficial-ish public endpoint.
- Rate limit politely.
- Current price only, no historical app price history.

### SteamGridDB API

Purpose:

- Artwork assets: grids/heroes/logos/icons
- Game lookup by Steam AppID or SGDB search

Caveats:

- Requires bearer token.
- Apply content filters by default.
- Cache assets/URLs.

### IsThereAnyDeal API

Purpose:

- Historical lows
- Current best deals
- Cross-store pricing
- Deal URLs
- Country/shop support

Caveats:

- Terms/attribution matter.
- API keys/OAuth depending on endpoint.
- Does not equal SteamDB’s exact Steam-only regional history, but is probably the best legitimate API for deal intelligence.

### SteamDB

Purpose:

- Human reference for price history, charts, app metadata/history.

Caveats:

- No public API.
- No scraping/crawling dependency.
- Store reference links only.

### Local Steam manifests

Purpose:

- Installed status on the current machine.
- Library folder/install directory.

Caveats:

- Not full owned library.
- Machine-specific.

---

## Privacy/security design

Rules:

1. Do not store API keys in Markdown notes, durable Library docs, Vault docs, skills, or generated reports.
2. Do not log API keys or bearer tokens.
3. Prefer public/shared API access first.
4. Make private/authenticated modes explicit and opt-in.
5. Avoid browser cookie/session automation unless explicitly requested.
6. Separate raw API cache from human-readable reports if raw cache may contain sensitive account metadata.
7. Provide a `steamlib doctor` command that checks credentials without printing them.
8. Make stale-data status visible in reports.

---

## Recommended implementation plan

### Phase 0: Validate APIs with tiny probes

Before building a large CLI, run minimal probes for:

- `IPlayerService/GetOwnedGames` with the user’s SteamID/API key.
- `IWishlistService/GetWishlist` for the user’s SteamID.
- Steam Store `appdetails` for a few known app IDs.
- SteamGridDB lookup/art fetch for a known Steam AppID.
- ITAD game lookup and price/history for a known title.
- Local appmanifest scan on the user’s machine, if Steam is installed locally.

Each probe should save a redacted sample response shape for schema design.

### Phase 1: Minimal CLI/cache

Implement:

```bash
steamlib doctor
steamlib sync-owned
steamlib sync-wishlist
steamlib scan-installed
steamlib export --format json
```

Goal: produce a reliable joined list of owned/wishlist/installed app IDs.

### Phase 2: Pricing/deals

Implement:

```bash
steamlib enrich-store
steamlib price sync-current
steamlib enrich-prices
steamlib deals wishlist
```

Goal: answer the wishlist deal question with evidence.

### Phase 3: Artwork/presentation

Implement:

```bash
steamlib enrich-art
steamlib recommend wishlist-deals
```

Goal: agent-readable and human-readable reports with SteamGridDB assets.

### Phase 4: Hermes skill

Create a Hermes skill named something like `steam-library` documenting:

- CLI commands
- cache paths
- config paths
- required env vars
- privacy caveats
- stale-data rules
- how to answer common questions

### Phase 5: Optional MCP

Only add MCP if useful after CLI stabilizes. MCP should wrap the same cache/data layer rather than independently calling APIs with different semantics.

---

## Suggested report format for the wishlist deal question

When answering:

> “Go through my wishlist — what are the best deals right now?”

Return something like:

```markdown
# Best Steam wishlist deals right now

Data freshness:
- Wishlist synced: 2026-xx-xx
- Owned library synced: 2026-xx-xx
- Steam current prices synced: 2026-xx-xx
- ITAD historical lows synced: 2026-xx-xx

## Top picks

### 1. Game Name
- Current Steam price: $x.xx (-NN%)
- Historical low: $x.xx via provider/shop/date
- Wishlist priority/age: priority N, added YYYY-MM-DD
- Why recommended: ...
- Caveats: ...
- Links: Steam / SteamDB / ITAD

## Skip for now

### Game Name
- Reason: discount is weak vs historical low / reviews poor / already owned elsewhere / likely cheaper during seasonal sale
```

The report should distinguish evidence from interpretation.

---

## Open questions for the next model/researcher

1. Test `IWishlistService/GetWishlist` against the user’s actual SteamID. What exact fields are returned? Does it require public wishlist/library visibility?
2. Does `IWishlistService/GetWishlist` work without an API key as xPaw’s reference suggests, or does practical access vary?
3. What is the best ITAD endpoint flow for mapping Steam AppIDs to ITAD game IDs and fetching historical lows/deals in bulk?
4. What are ITAD terms implications for a private personal agent CLI? If unclear, should we contact ITAD or keep usage personal/attributed/cached?
5. Does SteamGridDB provide direct Steam AppID lookup sufficient for all owned/wishlist items, or do we need fuzzy fallback search?
6. Which existing GitHub project has the best schema and error handling to borrow from?
7. Should the CLI be Python (fast iteration, local scripting), TypeScript (MCP ecosystem), or both CLI + optional MCP wrapper?
8. What should be the canonical cache: JSON files for transparency, SQLite for queries, or both?
9. Should cross-store deals be opt-in? Some users may only want Steam purchases despite better authorized-store prices.
10. What privacy mode should be default for reports that include wishlist and playtime?

---

## One-line recommendation

Build a small local `steamlib` CLI with JSON/SQLite cache. Use Steam Web API for owned library and wishlist, Steam Store for current pricing, ITAD for historical/deal context, SteamGridDB for artwork, local appmanifests for installed status, and SteamDB only as human reference links — no scraping dependency.
