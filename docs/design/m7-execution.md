# M7 local operations and safe plans execution plan

Status: accepted 2026-07-15

## Outcome and sequence

M7 gives agents useful local operational answers without pretending that the
Steam client exposes a supported administration API. The milestone is three
read-only tracer slices:

1. AUR-623 returns a cache-only operational view over the promoted installed
   projection.
2. AUR-643 ranks reclaim-space candidates and conditional travel-install
   candidates without mutating storage or Steam.
3. AUR-628 returns short-lived, inert operation plans and official human UI
   references. AUR-631 records that no executable action class is approved.

The command process must not scan Steam, call a provider, enumerate processes,
open a browser or client, execute a URI, or write data while answering an M7
query. A writable `sync installed` remains the separate, explicit observation
step inherited from M1.

## Evidence boundary

The approved local parser reads only `libraryfolders.vdf` and numeric
`appmanifest_*.acf` files. The promoted projection can truthfully establish:

- installed presence on one configured machine;
- manifest-declared `SizeOnDisk` and `buildid` when present;
- scan observation time and manifest filesystem modification time; and
- immutable local evidence lineage.

Manifest modification time is source provenance, not a product update time.
Build ID is an observed identifier, not proof that the installation is current.
Raw `StateFlags` remain an internal, unofficial qualification heuristic and are
never exposed or decoded into download/update states.

The initial adapter cannot establish running state, download/update queues,
update currency, bandwidth or ETA, per-library capacity, saves or Steam Cloud
state, screenshots/recordings, Workshop or mod state, or compatibility-tool
selection. Each is returned once as `unavailable: adapter_not_implemented`, not
as false or empty. Adding any such parser is a later reviewed slice.

Operational installed evidence is fresh for 15 minutes. A future or malformed
observation time has unknown freshness. A newer running, partial, or failed scan
makes a retained last-good observation stale; it never replaces that projection.
Missing optional size/build fields remain field-level unknown without erasing
known installed presence.

## Query contracts

```text
steam-agent operations observe --machine MACHINE [--format json|table]

steam-agent storage rank --recipe reclaim-space/0.1 --machine MACHINE --target-bytes BYTES --limit N [--explain] [--format json|table]

steam-agent storage rank --recipe travel-install/0.1 --account ALIAS --machine MACHINE --country CC --language english --budget-bytes BYTES --limit N [--explain] [--format json|table]

steam-agent operations plan launch|install|uninstall|verify|backup APPID --account ALIAS --machine MACHINE [--expires-minutes N] [--format json|table]

steam-agent operations plan move APPID --account ALIAS --machine MACHINE --destination-library-ordinal N [--expires-minutes N] [--format json|table]
```

All output is path-free. Account and machine aliases are request context, never
provider identifiers. The observe and ranking commands read one coherent cache
snapshot. Complete empty authorized scopes are successful empty results;
missing first snapshots are unavailable. Unknown account or machine aliases
are invalid rather than aliases for empty or unsynchronized scopes. A move plan
requires a request-local destination-library ordinal from 1 through 1024; the
option is rejected for every other operation.

## Storage ranking

`storage-ranking/0.1` separates rankability from action safety.

`reclaim-space/0.1` considers only the selected machine's promoted installed
projection. Fresh known installed presence plus a known non-negative manifest
size is rankable. Candidates sort by eligibility, known bytes descending, then
AppID. The request-local target reports whether one candidate meets the target
and its target fraction; it never constructs a supposedly safe uninstall set.
Save, mod, and cloud risks remain explicit unknowns, so an eligible candidate
means only that its content-size evidence is usable.

`travel-install/0.1` considers visible-owned games that are not positively
installed on the selected machine. It uses an explicit request-local byte
budget and the existing conservative English minimum-requirements parser.
Declared minimum storage is neither download size nor actual installed size.
An upper bound within budget passes that gate, a lower bound over budget fails,
and a budget inside the SI/IEC interval is unknown. Known incompatibility
excludes a candidate; unknown compatibility is conditional. Actual footprint,
download/update bytes, bandwidth, queue state, and completion time always remain
unknown, so the recipe cannot promise that a game will install tonight.

Hard gates always precede preference evidence. Deterministic ties use AppID.
No system-profile drive is treated as a Steam-library target because the current
collector has no trustworthy app-to-drive join.

## Operation plans and human-open references

`operation-plan/0.1` is inert data. Each plan includes the operation and target,
generation and expiry times, a deterministic idempotency key, capability and
policy state, typed preconditions, risks, interactive-human-only confirmation,
Steam UI instructions, official HTTPS references, rollback guidance, and
postconditions the human should verify.

Plans never authorize execution and remain useful when preconditions are failed
or unknown. No command invokes `open`, a Steam URI, subprocess, shell, client
IPC, or filesystem mutation. Undocumented Steam URI schemes are absent. The
initial official references are Steam Store/Support pages; the human performs
the action in Steam.

Install, uninstall, move, verify, launch, and backup plans all warn that local
save/config/mod state is unknown where relevant. A content backup is never
represented as proof that saves are protected. Move guidance follows Steam's
Storage UI; verify guidance follows the Steam Support installed-files flow.

## Acceptance harness

Acceptance covers at least:

- exact known/unknown/stale/future and M1 last-good operational semantics;
- path and raw-state canaries in JSON and table output;
- deterministic ordering and permutation tests for both ranking recipes;
- zero-byte, missing, malformed, overflow, and exact freshness boundaries;
- travel interval fit, incompatibility, unknown compatibility, and conditional
  final feasibility;
- explicit unknown save/mod/cloud/download/time fields;
- deterministic plan identity, expiry, validation, and per-operation golden
  output;
- tripwires proving cache-only queries and plans perform no network,
  filesystem, subprocess, browser, client, or storage writes; and
- regression tests preserving M1-M6 schemas and behavior.

## Acceptance evidence

Accepted on 2026-07-15 with 1,468 repository tests, Ruff, source/wheel builds,
installed-wheel smoke, six active cache-only M7 common-question oracles, path
and raw-state canaries, and a final two-reviewer Diffwarden pass with zero
findings. The review loop fixed unknown-machine validation, explicit unsupported
bandwidth/completion states, CLI-shaped plan evals, ownership/license
separation, and false `NOT_SYNCED` plan diagnostics. A path-placement report was
rejected as invalid after the normal scenario loader, repository-relative diff,
and full test suite confirmed the files were correctly located.

Linear closes AUR-623, AUR-643, AUR-628, and AUR-631 against this evidence. No
executable action class is approved; future parsers or executors require a new
reviewed slice and decision record.
