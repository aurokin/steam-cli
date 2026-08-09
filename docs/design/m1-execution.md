# M1 execution plan

Status: historical acceptance record; implemented and accepted

This file preserves M1 scope and evidence at acceptance time. Use the
[user guide](../user-guide.md) and [CLI contract](cli-contract.md) for current
behavior across M1–M7.

Linear project: [Steam CLI — M1 Installed Library](https://linear.app/aurokin/project/steam-cli-m1-installed-library-e0f0bdc817cc)

## Outcome

Deliver a credential-free installed-library tracer bullet:

```text
local Steam files
    → read-only scanner
    → typed observations and sync run
    → transactional SQLite promotion
    → installed-game projection
    → deterministic CLI JSON
```

Implemented commands:

```text
steam-agent status --format json
steam-agent capabilities --format json
steam-agent doctor --offline --format json
steam-agent sync installed --machine local
steam-agent games query --scope installed --format json
```

`--format json|table` is also accepted as a global option before the command.
`sync installed` accepts `--steam-root PATH`, and `games query` accepts
`--include-paths`. No other sync or query scope is implemented in M1.

## Operating contract

The Steam root is resolved in this order for `sync installed`:

1. `--steam-root PATH`;
2. `STEAM_AGENT_STEAM_ROOT`;
3. the platform default (`~/Library/Application Support/Steam` on macOS,
   common Program Files locations on Windows, and the common `~/.local/share`
   or `~/.steam` locations on Linux).

The root must contain the primary `steamapps` directory. Additional libraries
are read from `steamapps/libraryfolders.vdf`. Relative paths are supported for
isolated fixtures; normal Steam files use absolute paths. The scanner only
reads `libraryfolders.vdf` and `appmanifest_*.acf` metadata. Because these are
unofficial local formats, M1 conservatively includes a manifest in the installed
scope only when its numeric `StateFlags` contains the locally observed
`FullyInstalled` bit (`4`), or when a recognized update state is paired with an
existing resolved installation directory. This keeps an installed game visible
during an in-place update without treating a new staged download as installed.
Missing/invalid flags or manifests lacking either form of evidence make the scan
partial; raw flags remain in the observation for future refinement.

The database is `steam-agent.sqlite3` under the platform data directory. The
directory can be overridden with global `--data-dir PATH` or
`STEAM_AGENT_DATA_DIR`. Creating or migrating this application-owned database
is the only filesystem mutation; Steam-owned files are never modified.

JSON is the default and uses the versioned success/error envelopes described in
the CLI contract. Table output is intentionally compact. Installed-game queries
omit `library_root`, `install_dir`, and `manifest_path` unless JSON is requested
with `--include-paths`; table output remains compact even with that flag. Table
queries still print completeness, stale/missing capability, and warning rows
before game rows, so an unavailable or stale result cannot look like a confirmed
empty library. The database and evidence records retain local paths, so its
directory must be treated as private user data. On POSIX systems the application
creates its own data directory with mode `0700` and sets the database to `0600`;
an existing caller-selected data directory is not chmodded.

## Promotion and partial results

Each sync run is recorded as `complete`, `partial`, or `failed`:

- A complete run atomically replaces the installed projection for that machine.
  A complete empty scan therefore clears that machine's projection.
- If overlapping complete scans finish out of order, only the newer run may
  promote; the older run remains recorded but cannot overwrite newer state.
- Scanner warnings, unsafe/unrecordable install paths, malformed manifests, or
  inaccessible declared libraries make the run partial. Valid observations are
  retained for diagnostics, but the run is not promoted.
  (Amended 2026-08-08, after a real library froze its projection: warnings
  that merely record a manifest being correctly excluded from the projection —
  `not_fully_installed`, `uninstalled_app_state` — no longer make a run
  partial. Only warnings meaning the scan could not see or trust everything
  do. See the [CLI contract](cli-contract.md) for the current rule.)
- An unexpected exception marks the run failed and is not promoted.
- Partial and failed runs preserve the previous complete projection. If there
  has never been a complete run, a query returns no promoted installed games.

Partial sync is a successful command with `completeness.status="partial"`,
typed warnings, `sync_status="partial"`, and exit code 0. Callers must inspect
completeness rather than treating exit code 0 as proof of complete evidence.

## Credential-free boundary

M1 performs no network access and accepts no Steam or provider credentials.
`doctor --offline` makes that boundary explicit, although every current M1
command is local-only. Arguments resembling secrets are rejected rather than
stored or echoed. SteamID, profile privacy, owned games, wishlist, price
providers, and browser/client automation are deferred.

## Acceptance

- Works against configurable fixture directories and a real configurable Steam
  root without committing personal paths or data.
- Discovers multiple Steam library folders.
- Keeps missing, inaccessible, empty, malformed, and partial distinct. The JSON
  envelope reserves stale-capability reporting, but M1 defines no staleness
  threshold for local installed metadata.
- A malformed manifest produces partial success without discarding valid games.
- Re-syncing unchanged inputs is idempotent.
- A failed scan never replaces the last valid promoted projection.
- JSON ordering, fixed-clock timestamps, typed errors, and exit statuses are
  deterministic; diagnostics go to stderr.
- Machines are modeled explicitly rather than as a global singleton.
- Steam files are never written or modified.
- Documentation and review evidence match the implemented behavior.

The implementation and test suite cover these properties. AUR-605 closed with
122 passing tests, successful source/wheel builds, an entry-point smoke test,
and a final three-lane Diffwarden review with zero findings.

## Linear work graph

1. [AUR-600](https://linear.app/aurokin/issue/AUR-600) — prove the stack and
   record only M1-required decisions.
2. After AUR-600, three parallel foundations:
   - [AUR-602](https://linear.app/aurokin/issue/AUR-602) — CLI, JSON, errors,
     capabilities, and golden contracts.
   - [AUR-601](https://linear.app/aurokin/issue/AUR-601) — SQLite migrations,
     evidence, sync runs, and projection promotion.
   - [AUR-603](https://linear.app/aurokin/issue/AUR-603) — local Steam library
     and appmanifest scanning.
3. [AUR-604](https://linear.app/aurokin/issue/AUR-604) — integrate the full
   installed-library command path.
4. [AUR-605](https://linear.app/aurokin/issue/AUR-605) — documentation,
   adversarial review, diffwarden closure, and M1 acceptance evidence.

## Deliberately deferred

M1 does not include Steam credentials, owned games, wishlist, pricing, provider
accounts, recommendations, compatibility, action execution, MCP, daemon/TUI/web
interfaces, artwork, or cross-launcher support.

These remain design inputs rather than accepted implementation commitments.

## Human checkpoint

The next planned user decision is after AUR-605, when M1 behavior and evidence
can be reviewed as a whole. A newly discovered blocker that changes the product
boundary or requires credentials is an earlier checkpoint.
