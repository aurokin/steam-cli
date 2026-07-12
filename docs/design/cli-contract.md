# CLI and JSON contract

Status: M1, M2, M3, and M4 accepted; M5 implemented under acceptance

The selected executable is `steam-agent`. This document separates the current
M1 process contract from the longer-term vocabulary so agents do not mistake a
design sketch for an available command.

## Implemented M1 commands

```text
steam-agent [--data-dir PATH] [--format json|table] status
steam-agent [--data-dir PATH] [--format json|table] capabilities
steam-agent [--data-dir PATH] [--format json|table] doctor [--offline]
steam-agent [--data-dir PATH] [--format json|table] sync installed [--machine ID] [--steam-root PATH]
steam-agent [--data-dir PATH] [--format json|table] games query --scope installed [--machine ID] [--include-paths]
```

The format option can instead appear after a leaf command, for example
`steam-agent status --format table`. JSON is the default. `--machine` defaults
to `local` and identifies independent installed projections; it is not inferred
from a Steam account.

`--data-dir` overrides the application data directory. The environment
equivalent is `STEAM_AGENT_DATA_DIR`. `sync installed` resolves its Steam root
from `--steam-root`, then `STEAM_AGENT_STEAM_ROOT`, then platform defaults.

All M1 capabilities are local and credential-free. Secret-like arguments such
as `--api-key`, `--token`, `--password`, `--cookie`, and `--client-secret` are
rejected without echoing their value.

## Implemented M2 capability-gate commands

These commands are accepted under the M2 capability and credential boundary:

```text
steam-agent accounts discover [--steam-root PATH] [--include-identifiers]
steam-agent accounts configure (--from-local-most-recent | --steam-id64 ID) [--alias ALIAS] [--steam-root PATH]
steam-agent accounts status [--alias ALIAS] [--include-identifiers]
steam-agent accounts remove [--alias ALIAS] --yes
steam-agent auth set steam-web-api [--backend os|file] [--yes-file-risk]
steam-agent auth status steam-web-api
steam-agent auth remove steam-web-api --yes
steam-agent auth set <isthereanydeal|steamgriddb|gg-deals> [--backend os|file] [--yes-file-risk]
steam-agent auth status <isthereanydeal|steamgriddb|gg-deals>
steam-agent auth probe <steamgriddb|gg-deals>
steam-agent auth remove <isthereanydeal|steamgriddb|gg-deals> --yes
steam-agent owned capability [--account ALIAS]
steam-agent owned probe [--account ALIAS]
```

`accounts discover` returns a candidate count and whether primary selection is
available, ambiguous, or absent. It does not return Steam identifiers, account
names, or persona names unless `--include-identifiers` is explicit. An
ambiguous discovery can then be configured by passing one listed identity to
`--steam-id64`; unlisted identities are rejected. `accounts configure` persists only the chosen alias,
SteamID64, source kind, and timestamps. `accounts status` requires the explicit
`--include-identifiers` opt-in to return SteamID64.

`auth set` reads and confirms the key with hidden terminal input. The default
`os` backend must resolve to an approved native credential store. The `file`
backend is a POSIX-only, permission-protected but unencrypted fallback and
requires `--yes-file-risk`; it is never selected automatically. `auth remove`
removes the local credential but does not claim to revoke the key at Valve.

Optional third-party keys use distinct provider-scoped Keychain entries. `auth
status` is network-free and proves only local resolvability. `auth probe` is an
explicit fixed-host HTTPS validation request whose bounded response is discarded.
ITAD and SteamGridDB authenticate in headers. GG.deals documents only query-key
authentication, so its constructed request target is confined to the transport
boundary and is never returned in output or exception text. These credential
commands do not activate pricing/artwork adapters or persist provider data.
ITAD credential storage/status are available, but its live probe is deliberately
disabled until a canonical public project URL or private-use approval exists.

`owned capability` is read-only and makes no provider request. It reports
support, identity, credential, and last-probe state as separate axes.
`owned probe` is the explicit network boundary: the key is sent in the
`x-webapi-key` header to the fixed Valve HTTPS API host. The response is
processed in memory and discarded; only coarse result and retry metadata are
persisted. This gate does not store an owned library or prove complete license
ownership. A persisted one-second cross-process interval limits locally managed
user-key requests; a refusal returns `REQUEST_THROTTLED` as retryable.

The accepted M1 `capabilities` payload remains unchanged for schema `0.1`.
During the M2 gate, `owned capability` is the canonical detailed account
capability surface. Merging it into the top-level index requires an explicit
schema-contract change rather than silently altering the accepted M1 payload.

The 2026-07-11 live evidence for this gate contains only coarse
classifications: the configured primary account returned `ready`, a deliberately
invalid key returned `authentication_failed`, and a syntactically valid
nonexistent SteamID64 returned `data_inaccessible`. The last state means
inaccessible or ambiguous; it is not serialized as `private`.

## Implemented M2 persistent-inventory commands

The following surface is the accepted M2 contract:

```text
steam-agent sync owned --account ALIAS [--acknowledge-local-storage]
steam-agent sync catalog --account ALIAS --machine ID
steam-agent games query --scope owned --account ALIAS
steam-agent games query --scope library --account ALIAS --machine ID
steam-agent data delete --provider steam-web-api --account ALIAS --yes
steam-agent data delete --provider steam-web-api --all --yes
```

The first `sync owned` returns a typed `DATA_POLICY_ACKNOWLEDGMENT_REQUIRED`
result until the current disclosure is acknowledged by rerunning with
`--acknowledge-local-storage`. The blocked attempt makes no provider request.
The acknowledgment is account-scoped and records policy version, acceptance
time, and acknowledgment of user-controlled backup implications.

`sync owned` performs the documented false/true
`include_played_free_games` request pair. It promotes only when both responses
are structurally valid and the default set is a subset of the expanded set.
The query reports `visible_owned` and `played_free`; neither is a
purchase or license-kind claim. Authentication, visibility ambiguity, provider
failure, malformed data, inconsistent pairs, and interrupted runs preserve the
last-known-good projection. Only an explicit valid empty pair clears it.
Visible-owned snapshots older than 24 hours are returned as partial/stale even
when the last attempt succeeded; a running refresh does not make an old
projection fresh.

`sync catalog` derives a demand set from the selected owned and installed
projections, scans the documented ordered `IStoreService/GetAppList` games and
aggregate non-game streams, and persists only demanded AppIDs. The upstream API
has no documented arbitrary-AppID filter, so the initial scan may read multiple
pages through the highest demanded AppID. Classification is `game`,
`non_game`, or `not_observed`; the aggregate stream does not establish an exact
non-game subtype. Application, package, bundle, and edition identities remain
separate, and the latter three are `unknown` because this endpoint exposes no
supported mapping.

Every catalog attempt records its account alias subject, machine ID, and full
AppID demand before the first provider request, including attempts that later
fail. For every demanded AppID, joined completeness selects the newest attempt
for that same account and machine whose demand included that AppID, then
aggregates the unique relevant attempts. An unrelated account, machine, or
disjoint AppID refresh cannot make retained facts fresh or stale. If any AppID
lacks an applicable attempt, the catalog slice is unavailable; any relevant
failed or partial attempt makes it partial. Relevant attempts are listed with
their exact AppIDs so output never implies one run represented the whole query.
When an AppID has a relevant attempt but no last-good fact yet, the slice is
partial with `NOT_SYNCED` plus the attempt state: `SYNC_IN_PROGRESS`,
`SYNC_ABANDONED`, or its sanitized failure code. An AppID with neither a fact
nor an applicable attempt remains unavailable.
Each account/machine also retains its own last-good fact references; the shared
normalized catalog may advance independently, but a different subject's newer
classification, evidence, or observation time cannot replace query truth for
this subject. Subject promotion merges by AppID: a narrower completed demand
updates only its AppIDs, and an older completion may fill disjoint AppIDs but
cannot replace a newer overlapping fact.
Each catalog fact has a 24-hour freshness window; if any demanded fact is older,
the catalog slice is partial and reports the number and age range of stale
facts. One or more non-abandoned relevant refreshes in progress report
`SYNC_IN_PROGRESS` but do
not degrade a fresh complete last-good projection. It also does not make stale
facts fresh. Empty demand is vacuously complete and has no applicable historical
catalog attempt, warning, or stale capability.

Catalog page provenance is promoted only after both catalog streams complete.
A partial or failed attempt retains coarse run status and a sanitized error
code, but discards its page details and never replaces the last-good catalog
facts or provenance.

Normal owned and joined query output omits SteamID64 and local filesystem paths.
It keeps the following independent:

- visible in the default owned response;
- included only by the played-free flag;
- installed on the selected machine;
- application type from catalog/local evidence; and
- a stable local entity ID plus typed Steam application AppID identity;
- family availability, playable-now, purchasability, and license kind, which
  remain `unknown` without separate evidence.

Per-account deletion removes that target's normalized projection, account-
scoped evidence, sync/probe history, and account metadata. It does not remove
the data-profile-wide Steam Web API key. `--all` is the local Web API
termination path and also removes the shared key and reference; it does not
claim Valve revoked the key. Deletion results distinguish logical row deletion,
key-store deletion, and user-controlled backup remediation rather than returning
one misleading boolean. SQLite uses `secure_delete`; this is not a promise about
external backups, filesystem snapshots, flash remapping, or other copies.

Per-account deletion also removes that account's catalog attempt subjects,
demand membership, and catalog run rows. Public catalog facts survive only when
another account's demand/current projection or M1 installed evidence still
needs the AppID; retained facts have shared provenance with no deleted-account
subject. Unneeded catalog evidence and orphan application identities are
removed transactionally.

## Implemented M3 wishlist and deal-evidence commands

The following commands are accepted for M3:

```text
steam-agent sync wishlist --account ALIAS [--acknowledge-local-storage]
steam-agent sync prices --scope wishlist --account ALIAS --country US [--provider auto|gg-deals|cheapshark] [--max-items N]
steam-agent games query --scope wishlist --account ALIAS
steam-agent deals query --scope wishlist --account ALIAS --country US [--store-class official|keyshop|unknown] [--format json|table]
steam-agent data delete --provider <gg-deals|cheapshark> (--account ALIAS | --all) --yes
```

`deals query` requires an explicit account alias and country and defaults only
`--store-class` to `official`. M3 currently accepts only explicit `US` country
context and reports USD comparison context. It never infers country from IP
address, locale, or the Steam account.

The deal query is cache-only. It reads the wishlist projection, price facts,
per-AppID subjects, and relevant provider attempts as one SQLite snapshot. It
makes no network request, resolves no secret, and never opens a returned URL.
Network access and credential use occur only in the separate explicit
`sync wishlist` and `sync prices` commands.

For each wishlist AppID, the JSON result preserves the stable local game ID,
wishlist metadata and evidence IDs, every attributed current-offer and
historical-low fact (including conflicting candidates), selected ranked facts,
freshness, comparison grade, provider attempts, and manual references. Money is
integer minor units with currency and country. The context records store class
and its comparison scope: `official` maps to official-store lows, `keyshop` to
keyshop lows, and `unknown` to any-store lows. A comparison that is not
like-for-like remains degraded or noncomparable rather than being silently
ranked as exact.

The fallback ladder is deterministic:

1. exact-AppID GG.deals API summaries;
2. CheapShark's USD normalized-game fallback; and
3. manual-only GG.deals and SteamDB AppID references.

Manual references have `access_mode: manual_only` and
`automation_supported: false`. They are output for a human or separately
authorized browser workflow; Steam Agent does not fetch, scrape, or count them
as completed API evidence.

### M3 query completeness

- No last-good wishlist is `unavailable`, has `empty: false`, and reports
  `wishlist.read` missing. The warning distinguishes `NOT_SYNCED`,
  `SYNC_IN_PROGRESS`, `SYNC_ABANDONED`, or a sanitized failed-attempt code.
- A successfully synchronized empty wishlist is `complete`, has `empty: true`,
  and requires no price evidence.
- A stale last-good wishlist is `partial` and stale. A failed or abandoned
  latest refresh keeps last-good candidates but is `partial`. A running refresh
  is reported as `SYNC_IN_PROGRESS`; it is informational when the last-good
  wishlist remains fresh and otherwise cannot make stale data fresh.
- A fresh GG.deals `ready` subject completes the API ladder for an AppID. A
  fresh primary `not_found` intentionally requires a completed CheapShark rung.
  A fresh CheapShark `ready` or `not_found` completes the fallback rung; two
  `not_found` results produce a complete deal bucket of `unknown`, never a free
  price claim.
- Unevaluated, failed, running, abandoned, expired, or unsynchronized evidence
  remains a typed provider attempt. If no completed rung covers that AppID, the query is
  `partial` and reports `prices.wishlist.read` missing. Expired facts or
  terminal subjects are `partial` with `prices.wishlist.read` stale, not
  missing solely because they are stale.
- A failed or unavailable GG.deals rung may still produce a complete result
  through fresh CheapShark evidence. The result reports the sanitized primary
  failure and `DEGRADED_FALLBACK` rather than hiding the fallback.

Result and warning order are deterministic. JSON contains no SteamID64,
internal account ID, credential, raw provider body, or local filesystem path.
Table output is a safe abbreviated view over the same result and retains
completeness and warning rows; it cannot turn unavailable or partial evidence
into an apparently empty or complete list.

Account-scoped Steam Web API deletion removes the account's wishlist and price
demand, observations, subjects, attempts, evidence links, and orphaned
identities along with its M2 data. Provider/account deletion removes only that
provider's price data demanded by that account and preserves the shared
provider credential. Provider-wide `--all` deletion removes that provider's
cached facts and locally managed credential/reference while preserving Steam
account data and other price providers. Every deletion is confirmed,
transactional, idempotent, and reflected by later cache-only queries as typed
remaining, missing, or unsynchronized evidence.

## Implemented process behavior

- JSON data and typed JSON errors go to stdout. Diagnostics for unexpected
  failures go to stderr.
- Table data goes to stdout; table-form errors go to stderr.
- Table fields escape tabs, newlines, carriage returns, Unicode line/paragraph
  separators, and other control characters so provider or manifest text cannot
  inject rows or columns.
- JSON never emits color, spinners, banners, or log text.
- Timestamps are RFC 3339 UTC. Result keys and installed items are deterministic;
  installed items are ordered by AppID.
- Successful envelopes include `schema_version`, `command`, `generated_at`,
  `context`, `completeness`, and `data`.
- Error envelopes include `schema_version`, `command`, `generated_at`,
  `context`, and `error`, but no success `data` or `completeness`.
- Normal game queries omit local paths. JSON includes `library_root`,
  `install_dir`, and `manifest_path` only with `--include-paths`. The compact
  table view does not add path columns, but it does print completeness,
  missing/stale capability, and warning rows before item rows.
- A partial sync returns exit code 0 and a success envelope with partial
  completeness. It never replaces the previous complete projection.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Command completed, including a partial sync or an informational unavailable doctor/capability result. Inspect `completeness.status`. |
| 1 | Unexpected/internal operational failure. |
| 2 | Invalid arguments or an attempted secret on argv. |
| 3 | `sync installed` could not find or read the selected Steam root. |

### M1 success example

```json
{
  "command": "games.query",
  "completeness": {
    "missing_capabilities": [],
    "stale_capabilities": [],
    "status": "complete",
    "warnings": []
  },
  "context": {
    "machine_id": "local",
    "scopes": ["installed"]
  },
  "data": {
    "items": [
      {
        "appid": 10,
        "app_type": "game",
        "build_id": "12345",
        "evidence_ids": [1],
        "name": "Example Game",
        "observed_at": "2026-07-10T22:00:00Z",
        "size_bytes": 987654321,
        "state": "installed"
      }
    ],
    "next_cursor": null,
    "snapshot": {
      "last_attempt_status": "complete",
      "last_successful_sync_at": "2026-07-10T22:00:00Z"
    }
  },
  "generated_at": "2026-07-10T22:00:00Z",
  "schema_version": "0.1"
}
```

### Completeness and last-good rules

`complete`, `partial`, and `unavailable` are serialized distinctly. Scanner
warnings are typed and include only the source filename, not its full local
path. A partial or failed sync run remains stored for diagnostics but is not
promoted. `games query` therefore returns the last complete result for the
requested machine. Before any sync it returns `unavailable` with `NOT_SYNCED`.
If a partial or failed attempt has no prior complete run it remains
`unavailable`; if a last-good projection exists, the query returns it as
`partial` with `STALE_LAST_GOOD`. Snapshot metadata distinguishes the latest
attempt from the last successful sync. A currently running sync reports
`SYNC_IN_PROGRESS`: it is `unavailable` before the first successful snapshot,
or returns the unchanged last-good snapshot as `complete` with an informational
warning. M1 deliberately defines no timeout for declaring a running row
abandoned. A cancellation observed by the sync process is finalized as a failed
attempt with `SCAN_CANCELED` before the cancellation is re-raised, so it does
not leave a permanent in-progress result.

`status`, `capabilities`, and `doctor` are intentionally narrow in M1:

- `status` reports version, database initialization, and the installed count
  for machine `local`.
- `capabilities` reports the local `installed.read` capability based on default
  Steam-root discovery. When unavailable, its envelope and `doctor` both name
  `installed.read` as missing and provide the same typed warning.
- `doctor` reports the same local prerequisite and returns an informational
  unavailable completeness state, rather than a nonzero exit, when no default
  Steam installation is found.

## Implemented M4 recommendation query

```text
steam-agent recommendations query --account ALIAS [--machine local] [--scope owned] --recipe resume/0.1|finishability/0.1|preference-fit/0.1 [--time-minutes N] [--require installed=true|false] [--require user:<slug>=true|false] [--unknown include|exclude] [--override appid:N:<constraint>=pass|fail|unknown] [--explain] [--format json|table]
```

The only accepted candidate scope is `owned`, which is also the default. The
command reads visible-owned last-good candidates, the selected machine's
installed last-good projection, catalog classification, activity,
subject-consistent achievement summaries, and explicit feedback and rules in
one SQLite transaction. It performs no refresh, credential resolution, or
write. Known non-games fail the `game` gate; an unobserved classification is
`unknown` and follows the explicit unknown policy.

Requirements and overrides are bounded, exact-match expressions. Duplicate or
malformed expressions are rejected. Every result retains original and
effective gate states, component inputs and points, evidence lineage, factors,
tradeoffs, unknowns, freshness, confidence, and completeness under schema
`recommendations/0.1`. Missing titles are allowed; SteamID64, internal account
IDs, secrets, and local filesystem paths are not returned.

### Wishlist fit and public review evidence

```text
steam-agent sync reviews --scope wishlist --account ALIAS [--max-items N] [--acknowledge-local-storage]
steam-agent recommendations wishlist --account ALIAS --country US [--store-class official|keyshop|unknown] [--unknown include|exclude] [--override appid:N:CONSTRAINT] [--format json|table]
```

`sync reviews` retains only normalized aggregate counts, the complete fixed
request context, a fixed source locator, a typed manual-only store-page
reference, and bounded demand/attempt lineage. It never retains review text,
authors, cursors, or raw bodies. An omitted limit converges through fresh
terminal subjects in batches of 20; an explicit limit refreshes the
deterministic wishlist prefix.

`recommendations wishlist` reads wishlist, accepted M3 deal evidence, direct
feedback and rules, and optional aggregate reviews in one cache-only
transaction. Its immutable recipe is `wishlist-fit/0.1`. Eligibility,
preference fit, deal value, reviews, release, and compatibility remain separate
dimensions. Reviews are report-only; release and compatibility are explicitly
unknown, and `purchase_recommendation_supported` is false when every preference
dimension is unknown.

## Implemented M5 compatibility commands

M5 keeps collection and assessment as separate process boundaries:

```text
steam-agent sync system --machine MACHINE [--acknowledge-local-storage]
steam-agent system query --machine MACHINE
steam-agent sync compatibility --scope library --account ALIAS --machine MACHINE --country CC --language LANG [--appid APPID...] [--max-items N] [--acknowledge-local-storage]
steam-agent compatibility assess APPID... --account ALIAS --target machine:MACHINE|valve:steam-deck [--context-machine MACHINE] --country CC --language LANG [--require KIND:NAME] [--override APPID:NAME:GATE=pass|fail|unknown] [--explain] [--format json|table]
```

`sync system` is an explicit local observation and requires the current
machine-scoped persistence disclosure. It stores only the redacted
`system-profile/0.1` allowlist. `system query` is cache-only and does not run a
collector.

`sync compatibility` demands only AppIDs from the selected account's cached
visible-owned projection. Its disableable Steam storefront adapter is
provisional: it stores normalized publisher platform, controller, language,
positive accessibility-category, DRM/account-notice, and bounded sanitized
requirement declarations, but no raw response or HTML. Country and language
are part of the evidence context. Partial or failed attempts do not erase a
usable last-good declaration.

`compatibility assess` reads declared, system, installed, and visible-owned
inputs in one atomic cache snapshot. It performs no network request, secret
resolution, Steam-client access, or system collection. Every requested AppID
is returned under `compatibility/0.1`; primitive gates preserve pass, fail,
unknown, freshness, conflict, support level, and evidence lineage. Publisher
native-build evidence, effective execution support, exact-target review,
minimum comparison, requested features, likely-good experience, and
playable-now are separate claims. In particular, `linux=false` does not fail a
possible Proton route, and M5 does not rank CPU/GPU names or predict frame rate.

`--require` accepts an exact `accessibility`, `input`, or `language` feature.
`--override` is a named, AppID-scoped, ephemeral gate decision; output retains
both original and effective state, and no override is persisted. `--explain`
adds gate details. Returned Steam, SteamDB, ProtonDB, and PCGamingWiki URLs are
typed `manual_only` references with `automation_supported=false`; the command
does not open or read them.

For Steam Deck, `--context-machine` selects the account/machine-scoped sync
attempt lineage without applying that machine's system or installed facts. It
is inferred only when exactly one machine exists and is required for a
multi-machine store. A machine target always uses its own machine as context.

M5 cannot prove a title playable now because update, process, launcher,
network, and entitlement-session state are outside this milestone. A fresh
known-not-installed observation or known incompatibility may fail
`playable_now`; otherwise the operational result remains unknown.

## Exploratory future command shape

A composable vocabulary scales better than one command per natural-language
question:

```text
steam-agent sync <owned|recent|wishlist|catalog|store|prices|achievements|system|friends>
steam-agent games get <appid>
steam-agent games query --scope <owned|wishlist|store|family|group>
steam-agent games compare <appid...>
steam-agent group query --members <profiles...> --ownership all
steam-agent achievements query --state near-complete
steam-agent profile show|set|infer
steam-agent evidence show <evidence-id>
```

Likely shared options:

```text
--require FIELD=VALUE
--prefer FIELD=VALUE[:WEIGHT]
--avoid FIELD=VALUE[:WEIGHT]
--exclude FIELD=VALUE
--rank-by fit,deal,resume,finishability,group-fit
--unknown include|exclude|penalize
--freshness price=6h,metadata=7d
--explain
--fields ...
--limit ...
--format json|table
```

Agents should not need shell pipelines to recover facts hidden by human output.
JSON output must be complete; table output may be abbreviated.

## Future process goals

- Data goes to stdout; diagnostics and progress go to stderr.
- `--format json` never emits color, spinners, banners, or log text.
- Timestamps are RFC 3339 UTC; money uses integer minor units plus currency.
- Result order is deterministic, including tie-breakers.
- Schema, ranking recipes, and normalization rules are versioned.
- Pagination uses opaque cursors rather than offset assumptions.
- Partial success remains represented explicitly in the envelope.
- Secrets and raw authentication failures are redacted.

Suggested typed errors include `PROFILE_PRIVATE`, `GAME_DETAILS_PRIVATE`,
`WISHLIST_UNAVAILABLE`, `REGION_REQUIRED`, `STALE_CACHE`,
`PROVIDER_RATE_LIMITED`, `AUTH_REQUIRED`, `UNSUPPORTED_CAPABILITY`, and
`PROVISIONAL_PROVIDER_CHANGED`.

## Future enriched-envelope sketch

```json
{
  "schema_version": "0.1",
  "generated_at": "2026-07-10T22:00:00Z",
  "command": "games.query",
  "context": {
    "profile": "me",
    "country": "US",
    "currency": "USD",
    "system_profile": "desktop",
    "scopes": ["owned"]
  },
  "completeness": {
    "status": "partial",
    "missing_sources": ["wishlist"],
    "warnings": [
      {"code": "WISHLIST_UNAVAILABLE", "message": "Wishlist adapter unavailable"}
    ]
  },
  "results": [],
  "evidence": [],
  "next_cursor": null
}
```

A ranked result should expose `eligible`, constraint outcomes, each score
dimension, reasons/tradeoffs, availability, price context, separate confidence
dimensions, and evidence IDs. A derived evidence record points to upstream
evidence and the rule/model version that produced it.

## Future completeness rules

An empty owned library is not the same as a private library. A missing price is
not the same as a free game. An absent accessibility declaration is not proof a
feature is unsupported. These states must survive normalization and serialization.

Future `steam-agent capabilities` should report, for every capability:

- supported, provisional, unavailable, or disabled
- required credentials/consent without printing secrets
- last successful sync and last error
- source support level and known limitations
- completeness/freshness that queries can currently promise
