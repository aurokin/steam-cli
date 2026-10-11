# Agent core delivery

Status: **Working implementation, owner-directed 2026-10-11.** The owner has
requested implementation of the [recommendations](agent-core-brainstorm.md).
Accepted contracts and broker authority remain defined by the ADR register;
consequential new decisions are recorded there when their concrete slice is
tested. This page tracks delivery, not hypothetical support claims.

## Delivery order

| Work | Status | Exit evidence |
| --- | --- | --- |
| Real broker concurrency/crash/orphan recovery | Implemented | Real disposable subprocesses, exact state/event/effect counts, no replay after intent |
| Exact cached membership and wishlist labels | Implemented | Same evidence/completeness, bounded output, no provider or secret resolution |
| Steam-free account setup | Implemented | Explicit ID validation/provenance, no local discovery or provider access; composed API workflow and absent-root installed smoke |
| Explicit ephemeral credentials | Implemented | Stored default/no fallback, four-route synthetic canaries absent from output/persistence/native children |
| Broker results and lost-response recovery | Implemented | Opt-in versioned result, legacy output preserved, typed desktop refusal, read-only original-request lookup/collection |
| Advisory preflight/version/intended host | Implemented | Reused gates, writer/library certainty, no request/nonce/Steam mutation, dispatch rechecks remain authoritative |
| Native client qualification | Partially qualified | Haste read-only scheduler/receipt and four owned hidden-window stop cases passed; actual Windows title classes remain unqualified |
| Windows content backend | Candidate implemented; unsupported | Native owned-job/NTFS primitives passed; private authenticated cache, lease/provisioning and prepared-title verification remain |
| Wishlist and purchase handoffs | Implemented | Inert baseline-bound wishlist changes and product/price/ownership uncertainty; no account/store mutation |
| Measured refactoring | Implemented bounded slices | Lazy credential import, portable session types, pure installed handler, batched names/eligibility and exact compatibility inventories |
| Upgrade/downgrade recovery | Implemented | Unsupported ledger contracts refuse opening before WAL/index changes; original bindings and older supported index upgrades preserved |
| Content output privacy | Implemented | Bounded per-stream pipe capture, real noisy subprocess/SIGKILL tests and native Windows owned-job capture; uncertainty aborts owned work before adoption |

## Scope decisions

Freeze new provider/country/bundle acquisition, compatibility grammar, household
models, discovery inference, scoring recipes, achievements/review hydration and
rich table features while core delivery remains incomplete. Preserve accepted
existing features, cleanup, privacy and evidence contracts. Human move/uninstall
and generic backup plans remain inert. No daemon, MCP transport, universal graph,
plugin system or central fleet ledger is added without measured need.

The evaluator is absent from the installed wheel and core imports. Campaign
extraction is conditional on maintenance measurements and explicit replacement
of accepted qualification duties; it is not a runtime optimization. Deep residual
measurement remains truthful and separately measurable; first avoid traversal
for discarded duplicate manifests rather than silently change acquisition.

The proposed structural refactors are a menu, as stated in the research's
skeptical synthesis. Broad repository splits, intake/executor rewrites and model
campaign extraction are not prerequisites for this release. Preserve accepted
commands and evaluation duties while freezing expansion; move modules only at
measured seams. This delivery extracted the installed query's pure projection
without pulling coupled wishlist/library provenance helpers into new modules.

Declared-app eligibility reads fell from 507 to 12 SELECTs for 100 synthetic
demands and from 5,007 to 17 for 1,000, with equivalent demand/audit outputs.
Compatibility inventory reads now project requested AppIDs in SQLite-bounded
chunks while retaining full snapshot lineage/provenance; assessments match the
full-inventory reference. These measurements concern local query work, not
provider latency or arbitrary machine performance.

## Qualification limits

Native tests use owned synthetic fixtures before Steam titles. They do not
establish playability, save protection, arbitrary title coverage or current owner
account identity. The preceding [host support record](agent-host-support.md)
remains the source for live evidence. Haste's development activity is preserved.
Windows content guards remain closed until the relevant backend and qualification
exist. The owner confirmed no private SteamCMD cache is provisioned on Haste.
Manual credential provisioning and offline/online preparation stay with the
owner under the accepted boundaries. Automatic wishlist mutation and consumer
checkout remain outside executable authority; implemented handoffs are inert.
Old releases may have left raw spool files. This change prevents new spool
creation; safe cleanup of older artifacts still requires proven ownership and
writer status. It does not delete unknown files.

## Validation record

Final delivery gate on 2026-10-11: **4,186 passed, 50 skipped** in 71.74 seconds.
Locked dependency sync, Ruff, source/wheel build, installed-wheel smoke,
documentation checks and diff hygiene passed. The installed smoke runs outside
the checkout and verifies original-request recovery, binding conflicts,
uncertain-dispatch nonreplay and future-ledger refusal without Steam adapters.

Haste's separate native fixtures passed private init/read-only desktop dispatch
and fresh collection, four owned hidden-window stop cases, whole-job process
completion/cancellation and bounded pipe capture, plus local NTFS identity and
adoption/recovery guards. Exact limits and tested module identities are in the
[host record](agent-host-support.md). Test counts and synthetic fixtures do not
establish real Windows title behavior, authenticated content completion, saves
or power-loss durability. Those qualifications remain open as listed above.
