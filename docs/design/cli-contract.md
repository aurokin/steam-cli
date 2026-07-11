# CLI and JSON contract

Status: M1 installed-library contract implemented; M2 capability gate in progress

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

These commands are implemented while M2 live validation remains open:

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

## Exploratory future command shape

A composable vocabulary scales better than one command per natural-language
question:

```text
steam-agent sync <owned|recent|wishlist|catalog|store|prices|achievements|system|friends>
steam-agent games get <appid>
steam-agent games query --scope <owned|wishlist|store|family|group>
steam-agent games compare <appid...>
steam-agent compatibility assess <appid...> --system <profile>
steam-agent deals query --scope wishlist --country US
steam-agent group query --members <profiles...> --ownership all
steam-agent achievements query --state near-complete
steam-agent profile show|set|infer
steam-agent feedback rate|avoid|finish|abandon|snooze
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
