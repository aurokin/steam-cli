# M2 truthful account inventory execution plan

Status: accepted 2026-07-11

This document turns the M2 outcome in Linear into bounded implementation and
review gates. Product semantics and lifecycle rules remain canonical in the
linked repository documents. Issue status, dependencies, and final acceptance
remain canonical in Linear.

## Outcome and issue sequence

M2 must let an agent distinguish configured account access, visible-owned
evidence, installed state, bounded catalog identity, and unavailable or stale
sources without turning any one source into complete license truth.

The execution order is:

1. AUR-620 accepts the redacted account/authentication capability boundary.
2. AUR-627 adds deletion-gated, last-known-good visible-owned persistence.
3. AUR-629 joins the selected account, machine, and bounded catalog evidence.

AUR-620, AUR-627, and AUR-629 are accepted. The final gate passed 403 tests,
package construction, live primary-account tracers, and a two-reviewer
Diffwarden pass with zero findings. This acceptance does not activate wishlist,
pricing, recommendations, artwork, compatibility, or Steam actions.

## Capability evidence collected 2026-07-11

Three explicit live probes were processed in memory and retained only as coarse
states:

| Probe | Result | Supported conclusion |
| --- | --- | --- |
| Configured primary account with configured key | `ready` | The documented visible-owned request is usable for that target at that time |
| Deliberately invalid key | `authentication_failed` | Rejected credentials are distinct from provider and visibility failures |
| Syntactically valid nonexistent SteamID64 | `data_inaccessible` | The response is inaccessible or ambiguous; it does not prove profile privacy |

No response body, header, key, Steam identifier, visible count, or game record
was retained as evidence. Synthetic contract tests remain responsible for
malformed, oversized, redirect, timeout, rate-limit, and server-failure shapes.

## Live inventory evidence collected 2026-07-11

- Two complete owned synchronizations returned the same 782 demanded
  applications: 764 in the default visible-owned set and 18 expanded-only.
- After the second promotion, SQLite retained 782 current rows, 782 normalized
  observations, and 782 account evidence rows while preserving two coarse run
  records; the superseded per-game payload was gone.
- The official catalog tracer processed six ordered pages across its games and
  aggregate non-game streams, persisted only the 782 demanded AppIDs, and
  classified 739 as games, 4 as non-games, and 39 as not observed.
- The joined query returned 782 distinct stable entity IDs with complete
  owned, installed, and catalog capability status. Normal output contained no
  SteamID64 or local path fields. Package, bundle, and edition mappings remained
  unknown as designed.

These are aggregate acceptance facts, not a committed copy of the user's game
list or provider response bodies.

## AUR-627 tracer bullet

The acquisition pair requests `GetOwnedGames` first with
`include_played_free_games=false` and then with it set to `true`, with app info
enabled only to obtain the optional display name. Both bodies are bounded and
processed in memory. Promotion requires:

- HTTP and JSON success for both requests;
- explicit nonnegative counts and matching unique positive AppID lists;
- a default set that is a subset of the expanded set;
- the same immutable target account and credential, protected by the shared
  account/credential operation lock for the request and commit; and
- successful transactional normalization and projection replacement.

The stored per-game allowlist is AppID, optional response name, optional total
playtime minutes with missing distinct from zero, and `visible_owned` or
`played_free`.
Provider, support level, requested flags, retrieval time, sync ID, and evidence
relationships provide provenance. Icons, last-played data, rolling or platform
playtime, raw bodies, provider errors, and SteamID64 do not enter the owned
observation payload. Response names stay account-scoped and never update shared
catalog or installed evidence.

Only the current last-known-good owned projection is retained. Coarse run
history explains later failures, while superseded per-game observations and
their account-scoped evidence are removed after promotion. A failed pair never
partially promotes and never clears an earlier projection.

## Persistence and deletion shape

Owned current/observation rows, sync subjects, and evidence subjects reference
the immutable local account row ID. Account identity must not exist only in a
JSON context field. The shared Steam Web API credential stays scoped to the
local data profile because the key owner and queried account are separate
identities.

Before the first persistent sync, a versioned disclosure is shown and
explicitly acknowledged. It enumerates fields, retention, visibility and
played-free limitations, storage-location/country semantics, backup limits,
and deletion choices. Acknowledgment does not authorize background work or
another target/capability.

Per-account deletion removes the account subject and all dependent Steam
account observations, current rows, evidence links, probes, and run history. It
preserves M1 machine observations, other accounts, third-party provider data,
and the shared Steam key. All-Steam-data termination removes every Steam
account subject and then the shared local key/reference. Local removal and
Valve revocation remain separate.

Account deletion also removes the target's catalog demand and attempt lineage.
Catalog facts remain only when another account demand/current projection or M1
installed evidence still needs the AppID, in which case their public provenance
is detached from the deleted subject. Unneeded facts, evidence, run provenance,
and orphan identities are pruned atomically.

SQLite `secure_delete` is the bounded database-file overwrite control. Results
report logical row and credential deletion separately. They do not promise
erasure from backups, snapshots, filesystem journals, flash remapping, or
copies created by other tools.

## Required acceptance tests

### Acquisition and promotion

- A valid nonempty pair promotes deterministically; a valid empty pair clears.
- Missing versus zero playtime survives parsing, storage, and JSON output.
- Only-expanded AppIDs are `played_free`; baseline AppIDs are never labeled
  paid or non-free.
- The classification basis is a sequential set difference; output warns that a
  concurrent library change between requests can appear expanded-only.
- A non-subset pair, duplicate/invalid AppID, count mismatch, missing or invalid
  required response envelope, oversized body, redirect, timeout, 401/403, 429,
  or 5xx does not promote. Additive provider fields are ignored unless they
  invalidate a required field.
- A failed first run is unavailable; a failed later run returns stale
  last-known-good evidence with the latest failure classification.
- Account deletion/reconfiguration or credential replacement during the
  request prevents promotion.
- No secret, SteamID64, response body, provider error text, or unreviewed field
  appears in SQLite, output, fixtures, logs, or exception chains.

### Retention and deletion

- Two successful syncs leave only the second per-game projection/evidence;
  a failed third sync preserves it.
- Deleting one of two accounts removes every row linked to that immutable
  account ID and preserves the other account, shared key, and all M1 data.
- All-Steam-data deletion removes every Steam account subject and locally
  managed shared key/reference while preserving unrelated provider and M1 data.
- Missing, unsafe, unavailable, or locked credential backends and injected
  transaction and filesystem failures produce typed
  incomplete results and remain safely retryable.
- Deletion is idempotent and reports backup/snapshot limits without echoing the
  deleted account data.

### Joined truth

- Installed-only, visible-owned-only, and overlapping AppIDs remain distinct.
- Each observed Steam application AppID maps to a stable local game entity via
  a typed external-identity row; packages and bundles cannot enter that mapping.
- Catalog persistence is bounded to the owned/installed demand set. The
  documented upstream `GetAppList` stream has no arbitrary-AppID filter, so the
  scan reads ordered pages through the highest demanded AppID and discards
  non-demanded entries in memory.
- Per-page catalog provenance is retained only for a complete two-stream
  promotion. Partial or failed attempts retain coarse run diagnostics, discard
  page details, and never replace the last-good facts or provenance.
- Separate games and aggregate non-game streams support `game`, `non_game`, and
  `not_observed` only. Package, bundle, edition, and exact non-game subtype
  mappings remain `unknown`, never inferred from title or AppID.
- Catalog attempts persist their account/machine subject and demanded AppIDs
  before retrieval. The newest relevant attempt is derived independently for
  each demanded AppID and exposed as an honest aggregate; a successful attempt
  for one AppID cannot mask another AppID's newer failure. Freshness is evaluated
  for each retained fact using a 24-hour window.
- Shared normalization and subject truth are separate projections. Each
  account/machine query uses the classification, evidence, and observation time
  promoted by its own last-good attempt, so another account's refresh cannot
  freshen or reclassify it.
- Subject last-good promotion is per AppID rather than whole-demand replacement.
  Narrower refreshes preserve disjoint facts, and out-of-order completions may
  fill disjoint AppIDs without replacing newer overlapping evidence.
- A running catalog refresh leaves a fresh subject last-good projection
  complete with a `SYNC_IN_PROGRESS` warning; abandoned or stale refresh state
  remains partial. Empty demand is complete without inheriting historical
  catalog attempt state.
- A first or newly demanded AppID can have an active/failed attempt before it
  has a subject fact. That state is partial and reports `NOT_SYNCED` together
  with in-progress, abandoned, or sanitized failure evidence rather than
  collapsing every case to generic unavailability.
- Two accounts and two machines never cross-leak projections.
- Missing or conflicting catalog facts do not remove an owned/installed item;
  source-specific names/types and resolution provenance remain inspectable.
- Unknown and non-game application types remain visible.
- Family availability, playable-now, purchasability, and license kind are
  `unknown` until separate evidence supports them.
- Normal output is deterministic and omits local paths and SteamID64 unless the
  existing explicit identifier/path opt-ins apply.

## Canonical references

- [`steam-data-lifecycle.md`](steam-data-lifecycle.md)
- [`cli-contract.md`](cli-contract.md)
- [`product-questions.md`](product-questions.md)
- [`evidence-matrix.md`](evidence-matrix.md)
- [`architecture.md`](architecture.md)
- [`../project-governance.md`](../project-governance.md)
- [Steam CLI Product Roadmap](https://linear.app/aurokin/project/steam-cli-product-roadmap-dc80b02971d6)
