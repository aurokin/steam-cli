# M5 compatibility and ready-now execution plan

Status: accepted 2026-07-12

## Outcome and sequence

M5 answers two bounded questions without promising performance:

- what is known about whether a game is compatible with this explicit target;
- what additional evidence or condition prevents it from being playable now.

Linear sequence:

1. AUR-622 captures a private portable `system-profile/0.1` for an explicit
   machine alias.
2. AUR-633 captures bounded source-attributed declared application facts.
3. AUR-638 joins system, declared, owned, installed, and explicit-constraint
   evidence through immutable `compatibility/0.1` assessment rules.

AUR-622 and AUR-633 are independent foundations. The pure AUR-638 rule engine
and synthetic evals may proceed in parallel, but its integrated command closes
only after both foundations are stable.

M5 does not add generic FPS prediction, benchmark inference, remote-machine
inventory, broad discovery, group recommendation, Steam client mutation, or an
action executor.

Acceptance evidence: 1,284 repository tests, Ruff, packaged-wheel and bounded
live primary-account smokes, an independent adversarial audit, and a final
Diffwarden review with zero findings. Missing Deck review, performance, or M7
operational evidence remains unknown.

## System-profile boundary

`sync system --machine MACHINE` is an explicit local collection action requiring
a versioned machine-scoped disclosure. `system query` is cache-only.

The portable schema allowlists normalized OS family/version/build/kernel, CPU
architecture/model/core counts and bounded feature names, total memory, graphics
adapter vendor/model/memory/driver/API facts when directly observed, coarse
logical storage capacity/free space, and conclusive bounded device capability.
Every fact carries a typed state and evidence reference. Unknown is never zero,
false, or absence.

Collectors retain no raw native output. Hostname, username/home, serials,
hardware or disk UUIDs, MAC/IP, registry/PNP identifiers, display serials,
volume labels, device nodes, command lines, process lists, environment values,
and filesystem paths are excluded from storage and normal output.

Static OS/CPU/memory facts are fresh for 30 days, graphics/driver facts for
seven days, storage capacity for 24 hours, and available space for 15 minutes.
Stale causes remain visible. Superseded/failed attempt lineage is hard-deleted
within 30 days. Explicit system-profile deletion removes only this capability,
not the M1 installed projection or stable alias.

## Declared-application evidence boundary

The documented `IStoreService/GetAppList` change signal schedules only demanded
library AppIDs; it does not provide rich compatibility data. The isolated
provisional storefront JSON adapter may retain only selected normalized facts:

- publisher-declared Windows/macOS/Linux support;
- bounded per-OS minimum/recommended source text plus only unambiguous numeric
  RAM/storage or architecture derivations;
- declared controller and language/full-audio facts;
- positive accessibility category declarations;
- explicit DRM or external-account notices as conditional runtime claims.

Missing categories/notices are unknown. `linux=false` means no declared native
Linux build, not Proton incompatibility. Requirements HTML is sanitized into
bounded text; raw HTML/body and marketing content are discarded. Country,
language, support level, retrieval time, pacing, demand, attempt, and last-good
lineage remain attributed. A shape change disables the adapter without erasing
last-good.

Normalized declarations and account/machine demand lineage have a 30-day
logical retention boundary. Cache reads exclude expired rows. Physical pruning
runs on the next writable storage open (including sync, status, and explicit
data-management commands); `compatibility assess` deliberately opens the cache
query-only and never performs retention writes. A user who keeps the cache
offline and runs only assessment can use the documented provider/account data
deletion command for immediate physical removal.

Automated Deck-report retrieval and local `appinfo.vdf` are excluded because
their public/cache contracts are undocumented. Steam/ProtonDB/PCGamingWiki may
be typed human-only references. AreWeAntiCheatYet is a possible later
MIT-licensed runtime-risk adapter; it is not required for the first M5 tracer.

## Compatibility contract

```text
steam-agent sync system --machine MACHINE [--acknowledge-local-storage]
steam-agent system query --machine MACHINE
steam-agent sync compatibility --scope library --account ALIAS --machine MACHINE --country CC --language LANG [--max-items N] [--acknowledge-local-storage]
steam-agent compatibility assess APPID... --account ALIAS --target machine:MACHINE|valve:steam-deck [--context-machine MACHINE] --country CC --language LANG [--require KIND:NAME] [--override APPID:NAME:GATE=pass|fail|unknown] [--explain]
```

`--context-machine` selects account/machine-scoped declared-fact attempt
lineage for Steam Deck without applying that machine's hardware or installed
state. It is inferred only when one machine is configured and is required for
a multi-machine store. A machine target always uses its own machine context.

Assessment is cache-only and returns every explicitly requested AppID. Every
primitive gate is pass/fail/unknown; stale, inaccessible, and conflict remain
orthogonal evidence states. Any decisive hard failure yields `incompatible`.
Required unknowns yield `unknown`; otherwise a known manual/runtime condition
yields `conditional`. `compatible` requires every mandatory gate to pass.

The assessment opener rejects an outdated schema or uncheckpointed SQLite WAL
with an actionable database error instead of migrating or creating a sidecar.
Running a writable command applies migrations, checkpoints pending writes, and
performs due retention maintenance before the assessment is retried.

Publisher-declared native build, effective execution support, architecture,
meets-minimum, exact Valve target review, runtime risks,
accessibility/input/language constraints,
`likely_good_experience`, and `playable_now` remain separate. Valve Verified,
Playable, Unsupported, and Unknown map only to the exact reviewed target. M5
normally leaves likely-good-experience unknown. Installed plus visible-owned is
still not playable-now pass because M7 update/client/launcher/network state is
not observed; known fresh not-installed may fail playable-now for the selected
target. Visible-owned absence does not prove a missing entitlement. Publisher
`linux=false` fails only the declared-native-build fact; effective Linux
execution remains unknown unless a separately reviewed route supports it.

Query constraints are ephemeral and potentially sensitive. They are not
persisted. A named override preserves original/effective states and request
lineage without rewriting evidence.

## Acceptance harness

Normal CI covers redaction/denylist canaries, hostile native output, fixed
commands and size/time bounds, consent, migrations, machine/account isolation,
complete/partial/failed promotion, freshness, races, deletion, demand-bounded
provider convergence, retry/cooldown/interruption, provisional schema changes,
all gate states and overrides, input-order determinism, table safety, and
installed-wheel behavior.

Accepted synthetic M5 evals cover supported/failed OS, safe numeric RAM failure,
unparsed CPU/GPU unknown, target-scoped Valve ratings, accessibility positive
versus absent, stale/conflicting sources, playable-now unknown, not-installed
failure, overrides, deletion, and privacy canaries. Every deterministic oracle
executes in normal CI; natural-language judging remains the separate AUR-652
track.

Redacted live acceptance records only schema versions, coarse fact/state counts,
assessment state counts, and missing/stale capability codes. It never records
hardware models, titles, AppIDs, account or machine identifiers, paths, native
output, requirements prose, SteamID64, or credentials. Deletion tests use a
temporary data directory rather than the primary store.
