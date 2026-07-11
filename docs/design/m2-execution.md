# M2 truthful account inventory execution plan

Status: active implementation plan and evidence record; not milestone acceptance

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

AUR-627 remains blocked until AUR-620 is accepted. This plan does not activate
wishlist, pricing, recommendations, artwork, compatibility, or Steam actions.

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

## AUR-627 tracer bullet

The acquisition pair requests `GetOwnedGames` first with
`include_played_free_games=false` and then with it set to `true`, with app info
disabled. Both bodies are bounded and processed in memory. Promotion requires:

- HTTP and JSON success for both requests;
- explicit nonnegative counts and matching unique positive AppID lists;
- a default set that is a subset of the expanded set;
- the same immutable target account and credential version at commit time; and
- successful transactional normalization and projection replacement.

The stored per-game allowlist is AppID, optional total playtime minutes with
missing distinct from zero, and `default_owned_set` or `played_free_only`.
Provider, support level, requested flags, retrieval time, sync ID, and evidence
relationships provide provenance. Names, icons, last-played data, platform
playtime, raw bodies, provider errors, and SteamID64 do not enter the owned
observation payload.

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

SQLite `secure_delete` plus compaction or database rebuild is a bounded
best-effort control. Results report logical deletion, compaction/rebuild, and
credential deletion separately. They do not promise erasure from backups,
snapshots, filesystem journals, flash remapping, or copies created by other
tools.

## Required acceptance tests

### Acquisition and promotion

- A valid nonempty pair promotes deterministically; a valid empty pair clears.
- Missing versus zero playtime survives parsing, storage, and JSON output.
- Only-expanded AppIDs are `played_free_only`; baseline AppIDs are never labeled
  paid or non-free.
- A non-subset pair, duplicate/invalid AppID, count mismatch, unknown top-level
  contract, oversized body, redirect, timeout, 401/403, 429, or 5xx does not
  promote.
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
  transaction, compaction, rebuild, and filesystem failures produce typed
  incomplete results and remain safely retryable.
- Deletion is idempotent and reports backup/snapshot limits without echoing the
  deleted account data.

### Joined truth

- Installed-only, visible-owned-only, and overlapping AppIDs remain distinct.
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
