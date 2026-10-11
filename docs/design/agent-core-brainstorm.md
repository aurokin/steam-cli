# Agent core simplification and maturity brainstorm

Status: **Proposed — not accepted. Completed review, 2026-10-11 UTC.**

This review responds to the owner's request for exhaustive independent agent
brainstorming before a session reset. Recommendations do not remove accepted
M1–M7 behavior or widen broker authority. Implementation fixes are recorded
separately from product proposals. The [host support record](agent-host-support.md)
contains the preceding live qualification; the [CLI contract](cli-contract.md)
and accepted ADRs remain authoritative.

## Objective and constraints

Make the dependable center small: owned games, wishlist, installed games,
install/update/verify, launch, runtime observation and graceful stop on Linux
and Windows as the target. Windows client launch/stop requires native
qualification; Windows install/verify requires backend implementation as well
as qualification and is not merely an untested existing capability.
Preserve cache/evidence truth, last-good installed scans, private
data redaction, deterministic JSON and planner/broker separation. Existing SSH
is the transport; additional daemons or services need a demonstrated benefit.
Wishlist editing and purchase execution remain research, with no accepted
consumer mutation implementation.

## Review rounds

| Round | Independent responsibilities | Status |
| --- | --- | --- |
| 1 | Product cuts; architecture/data-flow; adversarial reliability | Three independent reviews complete; four concrete defects found |
| 2 | Account/wishlist/purchase capability design; agent interface/discovery; verified bug repairs | Two independent design reviews complete; repairs implemented |
| 3 | Native-host/release qualification; skeptical counterproposal | Two independent reviews complete |
| 4 | Performance/acquisition; Windows content design; privacy/safety; verification gap design | Four additional reviews complete; final repairs frozen and peer-reviewed |

Each reviewer inspects code rather than judging by test counts. Findings must
separate reproducible defects, unqualified native behavior, reversible internal
refactors, compatibility migrations and proposals needing an ADR. No credentials
or private account responses are shared. Live Steam actions are outside this
brainstorm.

## Preliminary evidence and priorities

| Finding | Evidence | Proposed response |
| --- | --- | --- |
| Product breadth dominates the surface | `cli.py` approximately 9,618 lines; `storage.py` approximately 10,101 | Freeze optional feature expansion; offer a short core workflow without hiding contractual exhaustive help |
| Evaluation is a separate maintenance center | About 24,174 runner lines; no evaluator imports in product source or package scripts | Consider extracting model campaigns; retain deterministic evidence, privacy, inertness and execution acceptance checks |
| Evaluation tests carry substantial source maintenance | 16 eval-named test files, about 29,523 lines and 802 test functions; function counts are not collected/parameterized test counts or runtime measurements | Measure CI cost before moving gates; do not infer runtime coupling from checkout size |
| Intake and execution responsibilities overlap | Direct request service plus separate legacy intake; client executor inherits content executor | Normalize intents behind existing entry points; separate client/content handlers with a common fresh authorization gate |
| Receipt shapes differ by path | Initial direct run uses outcome; repeat returns state; inspection adds another shape | Add a versioned common receipt while preserving legacy shapes through adapters |
| Runtime presence and scan completeness are flattened | Positive attributed processes coexist with global unknown on denied environment reads | Propose an explicit two-axis evidence model; preserve conservative completion and lease gates |
| Content policy revocation is missed after initial broker check | Isolated fake-adapter reproduction: permanently denying dispatch callback is never called by install/verify; content still runs | High-priority fix: recheck before every effect boundary and during content abort polling; allow restorative journal recovery |
| Malformed process environment can crash observation | Synthetic SteamAppId longer than Python's integer conversion limit raises ValueError | Bound decimal input before conversion; retain unknown instead of crashing |
| Windows stop gate is not checked per close request | Synthetic two-HWND reproduction revokes permission after the first close; second WM_CLOSE still sends | Propagate authorization to each native request, distinct from process identity failure |
| Partial close can be misreported as no dispatch | First HWND receives close, then a later close/owner probe fails before the process backend returns | Preserve a per-process partial-dispatch flag; terminal uncertainty prevents replay |

## What to cut, freeze or move

These are scope proposals, not permission to remove accepted commands. Prefer
freezing acquisition and isolating modules before deleting personal-assistant
features whose actual use is unknown. Migration cost is a reviewer estimate,
not a measured schedule.

| Proposal | Why / code seam | Counterargument and migration |
| --- | --- | --- |
| Extract opt-in model campaigns | `evals/runner` owns transport, attestation, blinded review, judge slots and supersession; product source does not import it | Agent answer quality matters. Move orchestration to a versioned research tool testing an installed wheel; retain pure scenario materializers/oracles, privacy and inertness locally. Supersede acceptance duties explicitly |
| Separate residual-size enrichment | `local_steam._read_manifest` calls residual walks with a 60,000-entry per-app budget | Reclaim estimates need residual truth. Keep migration 026 and unknown/partial/measured distinctions; make enrichment explicit with its own freshness. A default acquisition change needs a contract decision; benchmark first |
| Freeze price-provider expansion | `price_library`, `deal_query`, third-party adapters and migrations 013–015 add quotas, identity, currency, fallback and retention | Existing deal answers are useful. Keep accepted reads and cleanup; no additional countries/providers/bundles until core qualification |
| Deprecate orphan credential onboarding | ITAD/SteamGridDB credential surfaces exist without corresponding implemented acquisition consumers | Preserve status/remove for stored credentials. Explicitly deprecate new onboarding before removing unused probe code; keep Steam credentials |
| Freeze compatibility grammar/hardware expansion | Requirement parsing, native system probes and multi-source composers do not establish playability or depot selection | Preserve accepted assessments; do not make subjective compatibility a broker prerequisite. Shared declared facts cannot be deleted indiscriminately |
| Freeze household model expansion | Profiles, ownership/family/fact assertions, group composition and migration 024 form a separate knowledge base | Group play can matter. Multi-host inventory is not multi-person ownership; isolate imports and retain scoped deletion before considering deprecation |
| Freeze store-wide discovery ambitions | Current discovery deliberately uses a bounded candidate universe | Keep explicit candidate metadata shared by groups/compatibility; defer tags/mechanics inference and friend/store enumeration |
| Freeze recommendation recipe growth | Public versioned recipes and cross-domain wishlist-fit composition add scoring obligations | Preference memory and deterministic ranking remain valuable. Preserve established recipe versions; separate them from host control |
| Freeze achievements/reviews hydration expansion | Per-title activity/review adapters and migrations 017–020 add privacy/freshness/retention states | Retain useful owned playtime and existing outputs; recipe changes need versioning rather than silently dropping signals |
| Freeze travel/generic human-plan expansion | Travel intentionally leaves footprint/download time unknown; backup plans do not establish restorable saves | Keep simple size evidence and human uninstall/move handoffs. Do not expand executable authority as a simplification |
| Make direct verbs the primary workflow | `agent_requests.record_or_reuse` already accepts explicit intent and request IDs | Keep legacy request/confirm/run compatibility and single-use nonces; consolidate decisions without erasing deliberately different expiry semantics |
| Isolate optional command families | Eager CLI imports and the broad Storage facade couple unrelated domains | Concrete modules and lazy dispatch where useful; keep facades and atomic snapshots. Avoid a plugin registry or dependency-injection framework |
| Consolidate discovery metadata | Help, catalog, skills and living docs repeat command/effect/schema facts | Keep exhaustive help and separate static support from readiness. A core guide/lens is safer than hiding accepted syntax |
| Freeze richer human tables | New agent workflows need stable JSON more than new table/export layouts | Preserve accepted table output; this is lower priority than acquisition and evaluator costs |
| Reduce living-document duplication | Several pages repeat broker grants and host support | Preserve ADRs/history. Give current facts one canonical page; move incident narratives into an evidence appendix rather than making them first-use instructions |
| Defer new daemons/transports/platform modes | Existing SSH and host-local CLI meet the immediate transport need | Windows interactive-session invocation is a real requirement; qualify one bounded mechanism. Windows content is an explicit later implementation milestone to meet the owner's target, not a permanent cut; arbitrary executable/elevation fallbacks remain out |
| Avoid a universal HTTP framework | Eight adapters duplicate bounded fixed-host transport patterns | Share a small primitive only when repeated fixes justify it; provider-specific access/parsing constraints remain explicit |

Keep the two entry points, SQLite, last-good promotion, joined snapshot
atomicity, provenance and truth distinctions, private output, forward migrations,
request binding, per-user execution serialization, dispatch intent, current
policy gates, process identity and content adoption/recovery. These address
observed failure modes rather than speculative extensibility.

## Reshape/refactor: proposed sequence

| Step | Concrete change | Invariant / behavior-level acceptance |
| --- | --- | --- |
| 1 | Common fresh authorization at effect boundaries | Denied/unreadable policy blocks new work after lock waits, shutdown and polling; restoration/journal recovery still works under denial |
| 2 | Two decoders feeding typed execution intent/intake | Preserve exact original binding, permanent direct IDs and legacy expiry behavior; compare both ingress paths for malformed/mismatched intent |
| 3 | Compose launch, stop and shared content handlers | One ledger/lock coordinator; client-only provisioning constructs no SteamCMD adapter; content recovery remains restore-before-terminal |
| 4 | Portable session vocabulary | Move gate/result types out of Linux implementation; preserve explicit OS probes rather than pretending equal evidence |
| 5 | One internal operation receipt plus serializers | Separate durable state from invocation outcome; preserve old wire shapes through adapters, introduce richer versioned output explicitly |
| 6 | Runtime presence and completeness as separate axes | Positive identities coexist with incomplete coverage; absence requires complete scope; partial stop never certifies global absence |
| 7 | Inject clocks consistently | Aware wall time for durable expiry/evidence, monotonic time for polling; one timestamp per state/event transition, no naive values |
| 8 | Domain storage repositories on one owned connection | Keep Storage facade and transaction owner; joined snapshots, last-good, retention and deletion remain atomic; forward migrations only |
| 9 | Planner services returning typed values | CLI owns argv/envelopes/printing, services receive scoped dependencies; cache-only services cannot construct providers or resolve credentials |
| 10 | Collectable bounded desktop transport receipts | Reopen exact owned task after caller exit, relate job to request/operation, collect once, retain queued/running/foreign/unknown jobs safely |
| 11 | Broker instance identity and validated config | Identical local operation numbers on two hosts remain distinguishable; host labels do not become hidden SSH transport |
| 12 | Command metadata beside route registration | Generate static catalog without database/session/secret access; parse every published route and preserve exhaustive help |

Prefer small concrete services. A workflow engine, universal evidence graph,
central ledger, event-sourced replacement database or daemon is an alternative
only if its measured benefit outweighs migration and operational cost.

## Verification inventory

| Priority | Work | Acceptance / limits |
| --- | --- | --- |
| High | Content revocation | Install and verify: deny/corrupt during lock wait, shutdown, poll and late pre-adoption probes; no further effects, proper restoration and legal terminal states |
| High | Two real broker subprocesses | Same operation, run/reconcile races and separate state directories; exactly one external effect and no restoration over another writer |
| High | Process crash injection | Terminate disposable broker before intent, after intent and before acknowledgement; reopen SQLite and repeatedly reconcile without replay |
| High | Real fake content child | Broker death leaves a harmless writer; survivor detection blocks another writer; later recovery preserves ownership and restores correctly |
| High | Native Windows desktop bridge | Session selection, Task Scheduler registration/RunEx, fixed interpreter/package, LUA token, ACL, delayed worker after timeout and receipt collection; currently unqualified |
| High | Native Windows stop | Readable AppID attribution, multiple HWNDs, ownership change, UIPI, windowless child, exit and timeout without escalation; currently unqualified |
| Medium | Windows per-HWND authorization/counts | Revocation/expiry between windows stops later closes; first successful close remains counted if a later request fails |
| Medium | Cross-interface request binding | Direct vs legacy ingress, lost replies, expired IDs, force/config changes; characterize intentional differences before sharing intake |
| Medium | Title/runtime classes | Native, Proton, container wrappers, chooser, launcher-to-game transition, respawn and inaccessible peers; single-title success is not universal qualification |
| Medium | Exception cleanup during live fake writer | Poll/SQLite/filesystem failure must not restart Steam over surviving SteamCMD; durable recovery remains responsible |
| Medium | Crash-left raw spools | Synthetic sensitive output plus SIGKILL leaves private temporary logs outside Python finally; bound retention and cleanup without deleting active files |
| Medium | Evidence freshness | Missing secondary library, partial after complete, stale absence, alias mismatch and private/empty owned results; timestamps and limitations remain visible |
| Low | Bounded malformed inputs | Oversized AppID, nested JSON, invalid Unicode/TOML, replaced locks, symlink/reparse job files and malformed receipts; sanitized failure and no effect |
| Low | Long-running operation history | Retained desktop tasks/jobs, ledger/log growth, disk full, upgrades and active-intent downgrade refusal |

## Verified repairs during this review

- Oversized AppID environment conversion: bounded before conversion; the focused
  runtime suite passed **44 tests**, including 5,000-digit and zero-padded inputs.
- Content-policy revocation repair: implementation complete; **158 focused
  executor tests passed**. New content work rechecks
  policy at entry, shutdown, intent, ownership, polling and adoption; existing
  journal recovery and client restoration remain possible under denial.
- Windows per-HWND authorization and partial request counting: implementation
  complete; runtime/workflow/Windows-session check **90 passed**.
  Each native request gets a fresh gate after identity reads, and an earlier
  successful request remains counted if a later request fails.
- Recovery documentation: current behavior is canonical in the user guide;
  the earlier design table and Phase-0 experiment instructions are explicitly
  historical. No cleanup-delete or expired repair action was introduced.
- Final complete gate after all safety code changes: **3,909 passed, 45 skipped
  in 64.89 seconds**; locked sync, Ruff, source/wheel build, installed-wheel smoke
  and diff whitespace checks passed. These prove tested behavior, not native
  Windows qualification, game save retention or universal Steam compatibility.
- Partial-zero residual truth mapping: zero bytes from incomplete measurement
  now means unknown, while observed positive bytes mean present. **115 focused
  CLI/operation-plan/storage-ranking tests passed**, including six cases. The
  reviewer proved the internal mapping defect, not a visible false-absence
  output from the current reclaim gate.
- Malformed manifest numeric fields: ASCII/32-bit bounds return unknown for
  oversized AppID/StateFlags instead of raising during stopped-client execution
  or reconciliation. Parser and restoration checks passed within **210 focused
  executor/content tests**.
- Surviving-writer restoration: a fake proved failure could restart Steam and
  terminalize while SteamCMD remained alive. Restoration now requires writer
  absence, including when the client was initially stopped; unreadable probes
  defer and preserve recovery ownership. **166 focused executor tests passed**,
  including alive/unreadable writer and initially running/stopped client cases.
- Poll-callback exceptions: a fake proved callback failure could escape without
  killing/reaping the owned SteamCMD child. Adapter now aborts and attempts
  group termination with bounded leader reap on failed polls/waits/cancellation;
  polling failure outranks output success/auth markers and cleanup uncertainty
  raises a sanitized error. **61 focused content tests passed**; combined
  executor/content check **227 passed**. SIGKILL/detached descendants and native
  whole-group exit remain qualification gaps. Independent final peer review
  found no further concrete regression in these changes.

## Additions: agent-facing core

These are proposals. Command examples below describe future interfaces unless
explicitly identified as existing; they are not an implemented command reference.

| Priority | Addition | How to implement / meaningful acceptance |
| --- | --- | --- |
| First | Typed broker failures preserved through desktop transport | Today `_fail` writes stderr only; `run_worker` cannot decode stdout and turns a known refusal into `desktop_worker_result_unknown`. Introduce a small redacted error vocabulary and versioned envelope; synthetic policy refusal survives transport without forwarding raw stderr. Preserve exit meanings |
| First | Consistent receipt, attempt and retry fields | Derive durable state from ledger; invocation `deferred` can coexist with durable `authorized`. Keep old serializers while introducing an explicit contract. Distinguish same-operation resume, confirmation, reconcile, inspect after transport uncertainty and deliberate new intent |
| First | Exact AppID membership queries | Parser currently rejects `games query --scope installed --appid …`; add additive filtering to installed/owned/wishlist/library cache reads. Preserve context/completeness on known empty versus unavailable results. No provider or credential access |
| First | Wishlist cached names | Current wishlist rows omit titles. Batch-join existing cached app names with provenance; missing names remain unknown. No hidden catalog refresh |
| First | Broker version and expected-host check | Broker parser currently rejects `--version`; multiple hosts otherwise default to `local`, with planner account `primary` versus broker `desktop`. Add safe version/context and optional expected-machine validation before request insertion. Alias checking is not cryptographic attestation |
| Next | Read-only operation preflight | Separate implementation, provisioning, owner policy, current session/activity and recorded native qualification. Reuse execution gates; never insert intent, consume nonce or mutate Steam. Advisory result does not replace dispatch-time rechecks; launch ignores content-only download blockers |
| Next | Read-only lookup by request ID | Existing `inspect` needs an operation number; lost reply may lose it. Existing `--request-only` is a workaround. Add lookup without creating/resuming work. Not-found after a desktop timeout does not exclude a delayed worker |
| Next | Explicit freshness alongside completeness | Installed queries, operations observation and owned/wishlist use different existing age rules. Add source time, age and named freshness policy, preserving completeness and last-good. One aware now per query; no universal TTL |
| Next | Bounded pagination when measured useful | Bind cursor to scope, account/machine, filter, ordering and promoted snapshot. Changed snapshot yields `CURSOR_STALE`, never mixed generations; retain no-filter behavior. AppID filtering comes first |
| Next | Distinct membership/enrichment usability | Missing catalog classification can make the joined library partial while membership remains usable. Expose component evidence rather than inventing an independent ready flag |
| Later | Bounded observation follow-up | Optional runtime wait only observes; timeout never causes redispatch. Positive attribution, complete coverage and presentation are separate. Launcher process alone cannot prove chooser/main-menu/playability |
| Later | Structured supported-launcher presentation | PID/create-time/AppID-bound status can independently report awaiting profile choice. Prefer this over generic UI inference; absent supported evidence remains unknown. Keep profile names, titles, screenshots and save paths private |

For a new receipt contract, use separate `receipt.state`, `attempt.outcome`,
`attempt.reason_code`, dispatch certainty and a structured next-action category.
Do not rename `outcome` to `state`: they answer different questions. A terminal
uncertain result must never advertise automatic retry. A reused old receipt
must not invent an invocation outcome that was not persisted.

Keep a short core route in the guide and skills: smallest cached scope;
explicit authorized refresh if needed; intended host and operation preflight;
one chosen request ID; direct broker verb; inspect the original request on lost
response; separate runtime observation; resume/reconcile the same durable work.
The existing `install` class includes update, and there is no `update` verb.
Keep planner and broker skills separate. Clarify planner-only eval wording
that presently says the entire product never launches.

Operation budgets also need a single reference: content can run 30 minutes;
client shutdown/startup can wait 60/90 seconds; stop confirmation defaults to
10 seconds; desktop receipt polling defaults to 30 seconds. Transport wait
expiry does not mean cancellation. Stable phase/timestamp/reason fields are
more truthful than fabricated download percentages.

## Additions: account, wishlist and purchase assistance

| Priority | Addition | How to implement / meaningful acceptance |
| --- | --- | --- |
| First | Explicit ephemeral credential source | Current CLI owned/wishlist sync requires stored credential metadata before resolving a key, although pure sync services already accept `SecretValue`. Add a typed runtime resolver with explicit `stored` versus `process_env` selection and fixed provider variable. Never persist locator, key fingerprint or secret; no command execution, autologin, rotation or browser-cookie fallback |
| Next | Account × host evidence summary | Show configured credential availability separately from provider validation; visible-owned inventory separately from complete licenses; recent-local-login heuristic separately from active desktop account. Cached evidence summary and live broker preflight must be distinct views, sharing vocabulary rather than a second readiness database |
| Next | Explicit bounded refresh orchestration | Compose existing owned/wishlist services and individual receipts before dependent enrichment. Preserve last-good on partial failure; no provider call from a cache query. Keep actual retrieval times and validation method |
| Next | Inert wishlist change proposal | Bind add/remove/reprioritize suggestions to exact baseline sync ID/time and expected membership. `execution_supported:false`, human-open references only. Local desired priority/notes remain distinct from Steam facts. Human change plus separate refresh supplies completion evidence |
| Next | Purchase decision card / human handoff | Compose exact product/edition identity, comparable price, quote time, country/currency, seller, ownership uncertainty and compatibility prerequisites. Current price context is US/USD; no account-specific checkout-total claim, cart or purchase action |
| Later | Bounded snapshot change history | Compare successful promoted snapshots, detect equal-count membership replacements, retain evidence lineage. Failed refresh never means removal; account deletion removes retained changes |
| Later | Portable host evidence export/import | Validate host/schema/time/privacy fields; incomplete import cannot assert absence or replace complete installed projection. Avoid a central service until real fleet friction justifies one |
| Later | Local wishlist notes and review rules | Existing preferences/feedback hold intention separately from provider priority. Price thresholds need fresh comparable evidence; scheduling is explicit and outside queries |
| Later | Product/edition/DLC identity resolver | Use existing product/price subjects and reference validation; base games, DLC, packages and bundles cannot silently collapse |
| Research | Account-mutation qualification dossier | Identify supported interface and permission, accepted ADR and independent authorization/recovery contract before any mutation code. Do not borrow consumer/client/SteamCMD sessions |

Ephemeral source selection belongs at an explicit acquisition boundary. Pure
cache queries must not resolve secrets. A cache evidence summary may report
configured source but must not imply present secret validation; any live
credential availability check must be explicitly identified. Capture the value
only for the approved provider request and strip documented secret variables
from explicitly constructed unrelated child environments; the content adapter
currently copies `os.environ`. Do not broadly allowlist away Steam's required
desktop context or mutate global environment concurrently. Keep `SecretValue`
non-formatting, and test synthetic canaries across stdout/stderr, SQLite/WAL,
files, exceptions and child environments. Python wrapping does not erase
memory or isolate from privileged processes.

Wishlist count/list agreement is **not an atomic snapshot**. Count/list/count
also misses equal-count membership replacement. Any bounded stability check
must describe its actual assurance and preserve support/freshness limitations.

### Public API research, checked 2026-10-11

The unauthenticated [official API inventory](https://api.steampowered.com/ISteamWebAPIUtil/GetSupportedAPIList/v1/)
listed wishlist read methods, but not consumer writes in that response. It also
omitted documented `GetOwnedGames`, so absence here is not universal proof.
[ADR 0004](../adr/0004-provisional-wishlist.md) remains provisional.

Valve supports [Web API key headers](https://partner.steamgames.com/doc/webapi_overview/auth).
[GetOwnedGames](https://partner.steamgames.com/doc/webapi/IPlayerService) is
visibility-dependent and excludes unplayed free games by default; missing
membership is not proof of no license. Publisher-scoped
[CheckAppOwnership](https://partner.steamgames.com/doc/webapi/ISteamUser) does
not supply an arbitrary personal-license ledger.

[OpenID](https://partner.steamgames.com/doc/features/auth) supplies identity,
not a consumer store session. [Steam OAuth](https://partner.steamgames.com/doc/webapi_overview/OAuth)
requires a Valve-issued partner client and permissions. SteamCMD cached login
remains a content-plane credential. Future wishlist/purchase mutation research
must assess the [Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/)
and [Web API Terms](https://steamcommunity.com/dev/apiterms); owner approval
alone does not establish Valve permission. No credentials or real-account API
requests were used in this review.

## Agent behavior verification, beyond unit counts

Retain useful deterministic oracles, but add small sealed **synthetic broker
trajectories**, with no native effect path: lost reply, pending confirmation,
request-ID conflict, known bridge refusal, authorized deferral, chooser evidence,
terminal uncertainty and interrupted content recovery. Generate fixtures from
existing injected-backend tests and validate commands against the real parser.
Report incorrect decision, relevant receipt field and expected next-action
category; distinguish parsing, workflow, truth claim and safety failures.
Command count, bytes consumed and unnecessary reads/waits are diagnostics,
not substitutes for correctness. Required-command answer tracks do not prove
discoverability.

## Native qualification and release design

Qualification is a dated observation tied to an artifact, route, host and title;
it is neither a policy grant nor proof of current readiness. The package remains
version `0.1.0`, so identify tested wheels by digest as well as Python/dependency
and OS versions. Record only coarse normalized results publicly.

| Stage | Where / procedure | What it proves, and what remains open |
| --- | --- | --- |
| 0 | Checkout deterministic gate and installed wheel | Test/package contracts; no native Steam assurance |
| 1 | Disposable Windows desktop, actual Task Scheduler read-only capability/runtime worker from session 0 | Same-user interactive session, fixed interpreter, non-elevated token checks, bounded receipt and cleanup; not Haste configuration |
| 2 | Same desktop, native test-owned attributed process/windows and private acknowledgement channel | Real HWND enumeration/ownership/WM_CLOSE, stale identity refusal, per-window revocation and exit evidence; not a Steam title |
| 3 | Linux gaming host read-only package/session/library checks | Artifact/identity context and disclosed observation limits |
| 4 | Prepared Linux title in owner-selected window | Launch/retry/observe/graceful stop and independent target identity exits; title-specific only |
| 5 | Windows gaming host when idle | Repeat transport canary, then prepared title; Haste development is not disturbed to close the report |
| 6 | Linux disposable title/library maintenance | Install/verify/adoption, interrupted writer handling, revocation and restoration; separate from client launch/stop |
| 7 | Windows content backend after implementation | Independent install and verify recovery qualification; never inferred from client-path success |

The previous Herb run already demonstrates one bounded Linux title/launcher
path. It does not establish saving during gameplay, arbitrary Proton games,
Windows execution or a fresh Linux content recovery sequence.

Native Windows fixture tests should cover multiple HWND acknowledgements,
revocation after the first close, first success plus later failure, ignored close,
windowless child, unrelated windows, changed owner, mismatched creation time,
denied access/cross-session and no force escalation. Real PID reuse is difficult
to induce reliably; deterministic reuse plus native stale identity and churn
tests must not be mislabeled an exercised real reuse incident.

The current session flag/arguments match the documented
[Task Scheduler RunEx interface](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/nf-taskschd-iregisteredtask-runex).
Native registration acknowledgement does not establish worker start. Test
logout, unavailable interpreter, session/SID/elevation mismatch, queued execution,
caller disconnect, late receipt, and cross-process lock contention. Task
[security context rules](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks)
can ignore RunLevel in some configurations, so worker token self-checks remain
necessary. [WM_CLOSE](https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-close)
can prompt or be ignored; delivery is not exit.

Linux title classes need separate evidence: native, custom chooser,
Proton/Wine and container wrappers; launcher-to-game transition, respawn,
inaccessible peers and excluded infrastructure. Do not extend an uncertain stop
to newly appearing targets automatically. An additive target-exit receipt could
report selected identities/requests/original exits while keeping global scan
completion unknown. Preserve immutable operation terminals.

Prefer a supported launcher status channel (`chooser_waiting`, `profile_selected`,
`game_starting`, `game_observed`, `error`) with AppID/PID/create-time/time binding,
bounded retention and self-reported support. It supplies launcher observation,
not playability. A new profile-selection action needs its own reviewed intent
contract; status can arrive first. Generic mouse automation stays outside the
qualification spine.

Offline/online mode remains an owner workflow under current account-settings
boundaries. Follow Valve's [offline preparation guidance](https://help.steampowered.com/en/faqs/view/0E18-319B-E34B-B2C8),
and restore online afterward; do not auto-resolve
[Cloud conflicts](https://help.steampowered.com/en/faqs/view/68d2-35ab-09a9-7678).
Bounded owner-designated save hashes/inventories can establish changes in
covered locations; a main-menu-only check and six inspected files cannot prove
progress preservation. Separate synchronization metadata from designated saves.

Release candidates should be staged side by side. Switch only after active
execution/jobs and recovery settle; preserve ledger/request bindings. Extend
installed-wheel smoke with harmless old-ledger reopen, dispatch-pending
reconciliation, terminal same-request reuse and unsupported reader/state refusal.
Planner migrations and broker-ledger evolution are separate checks. Never restore
an old ledger snapshot merely to accommodate an older binary.

Desktop timeout recovery needs read-only owned-job discovery and receipt
collection, plus completed-only cleanup that re-proves token/source/task pairing
and idle state at deletion. Unknown/running jobs remain retained. Public records
omit account names/SIDs, tokens, full environments, raw responses, screenshots
and save paths; temporary private target identities may be used for verification.

## Skeptical synthesis: delivery milestones

The twelve refactor candidates above are a menu, **not twelve prerequisites**
before a useful release. Keep structural changes inside a demonstrated behavior
milestone. This is the recommended order:

| Milestone | Concrete delivery / exit criterion |
| --- | --- |
| 1. Durable execution and recovery | Settled safety repairs, then two real harmless broker subprocesses, killed-before/after-intent cases, surviving fake writer, restore-before-terminal, delayed-worker collection and partial stop nonreplay. Characterize both ingress paths before combining them |
| 2. Small unattended agent workflow | Exact cache membership, explicit credential-source refresh, typed bridge-safe refusal, original-request lookup/collection, version/intended-host checks. Synthetic keys never leak to output, SQLite/WAL, jobs or unrelated children; preserve old contracts unless explicitly selecting a new one |
| 3. Native Linux/Windows client qualification | Actual Task Scheduler/session/token/ACL mechanism and real OS stop fixtures, then relevant owner titles; publish route/title limits, immutable dispatched semantics and unknown global completion |
| 4. Windows content implementation and qualification | Decide and implement process ownership/abort, containment/durability, conservative activity gates, isolated authentication, adoption/recovery and client restoration. Existing install/verify grants remain independent. Both classes pass interruption/revocation/recovery and installed-projection verification |
| 5. Measured simplification, optional after core | Benchmark acquisition versus residual enrichment and evaluator maintenance separately. Keep residual observation time, partial truth and last-good; if campaigns move, replace/supersede accepted duties explicitly. Freeze optional expansion rather than delete unmeasured useful features |

For now the honest support label is **Linux content; Linux/Windows client code,
with route/title-specific qualification gaps**. The target includes Windows
content and milestone 4 stays open. A backend port within already accepted
classes does not itself widen authority; consequential new assumptions still
need an accepted decision.

Preflight reuses execution facts rather than a second gate engine. Cached
readiness says unchecked when current credentials/session cannot be observed
without access. Exact filters precede full pagination. Structured launcher status
is optional for native qualification. Moving evaluator code cannot accelerate
the product because the product does not import it. Preferences/group-fit remain
valuable until usage evidence supports a cut.

## Performance evidence and smaller optimizations

Measured offline on Python 3.13.13/macOS in the existing environment, warm OS
caches, bytecode writes disabled and synthetic temporary inputs. Root checks
ran concurrently; timings are descriptive samples, not portable performance
targets or production benchmarks.

| Finding | Actual evidence | Small change / acceptance |
| --- | --- | --- |
| Eager CLI imports | Five subprocess samples: help median 140.34 ms, static catalog 142.39 ms, CLI import 139.14 ms; static catalog module alone 17.52 ms versus interpreter 14.93 ms. CLI loads 42 product modules including keyring | Lazy-load keyring/provider adapters at their actual boundaries, retain one parser. Same help/catalog/privacy/credential behavior; remeasure subprocess startup. This moves needed cost to its command, not away entirely |
| Residual walks before duplicate selection | Ten synthetic apps ×200 files: 5.53/4.35 ms across two scans, 20 scandir calls and 10 distinct roots; shared symlink target had zero visits | Move walks after winning duplicate-AppID selection first. Ordinary distinct apps already have distinct roots; do not claim each walks the same Proton runtime. Repeat refreshes do repeat traversal; root mtime cannot certify recursive totals |
| Full inventory for bounded membership | Compatibility reads full owned/installed snapshots for requested AppIDs; group membership builds full per-member sets | Bounded projection preserving authoritative snapshot/visibility/freshness metadata. Whole-library ranking still needs full sets; source complexity is not a measured latency claim |
| Per-demand SQL amplification | `max_items=1`: 507 SELECTs for 100 synthetic demands, 5,007 for 1,000; single warm calls 3.13/25.99 ms | Batch context/freshness/negative-cache/active request/identity reads and select latest demand in SQL. Do not simply truncate before eligibility; preserve lineage quarantine, deterministic order, concurrency and untargeted audit state |
| Broad catalog streams for sparse demand | Source inspection: refresh restarts both streams at zero; page bound 50,000 entries/16 MiB; no provider latency measured | Reuse fresh contextual classifications and converge unresolved demand, preserving explicit refresh and page provenance. App-details substitution changes evidence support; two owned-library requests encode intentional different semantics |

Five synthetic Storage reopens with 1,000 demands took 0.70–0.97 ms: changing
retention-on-open is low priority and changes lifecycle semantics. The inspected
wheel was 389,102 bytes, 85 product files/26 migrations, with no tests/evals;
uncompressed product content was 1,788,106 bytes. The inspected sdist was
1,177,994 bytes compressed and includes roughly 1.22 MB evals/2.34 MB tests
uncompressed. Artifact freshness against the working tree was not established
by this reviewer. No accidental wheel bloat was found.

CI has four build/smoke matrix jobs. Sharing one pure-Python wheel can remove
duplicate builds while retaining per-platform dependency installation/smoke.
Eval round trips call `cli.main` in-process, so import improvements primarily
help external invocations. Full eval runtime was not measured; scenario/schema
caching is a smaller candidate than cutting acceptance coverage.

## Privacy and crash-recovery checks still open

These are risks/test gaps, not demonstrated credential disclosures or native
exploits. The fresh-eyes review found no additional defect in the settled policy
latch, per-window accounting or repaired residual mapping.

| Rank | Missing assurance | Meaningful acceptance |
| --- | --- | --- |
| 1 | SIGKILL leaves raw spools; read-back cap is not growth cap | Kill a harmless noisy broker child; bound growth/retention and clean only proven orphan files. Active or unknown writers retain their evidence |
| 2 | Live-child exceptions and termination failure | Poll/SQLite/wait/kill failures never restore Steam or retire recovery while a writer might survive; bounded owned-child cleanup and repeated reconcile |
| 3 | Ephemeral secret child boundary | Synthetic key only reaches explicitly selected provider; absent from native children, SteamCMD, jobs, stdout/stderr, logs, SQLite/WAL and errors, while desktop environment remains usable |
| 4 | Native Windows ACL/reparse/token behavior | Junction/replaced root/files, untrusted-user access, inherited ACLs, elevated/wrong SID/session worker and sanitized COM failure; no executable/session/elevation fallback |
| 5 | Fresh-process desktop collection | Reopen exact task/job/source/principal/action/instance; cleanup completed owned work only. Not-found is not assurance a delayed worker cannot run |
| 6 | Adversarial diagnostic redaction | Synthetic spaced/UNC/Unicode/mixed-case paths and identities, nested fields, sensitive suffix on recognized log line; prefer fixed structured reasons over more raw logging |
| 7 | Alias versus identity | Expected host/account mismatch fails before intent; aliases do not attest desktop login, license ownership or remote authentication |
| 8 | Request IDs in job argv | Require nonsecret opaque caller IDs; avoid unnecessary echoes. Hashing ledger keys does not anonymize operational history |
| 9 | Known refusal versus transport unknown | Typed bridge-safe refusal without raw stderr; unexpected error preserves uncertainty and never claims no effect |
| 10 | Active-operation upgrade/downgrade | Synthetic ledger/journal/job compatibility, unsupported reader/schema refusal and immutable request binding; no ledger snapshot rollback to fit an old binary |

Cross-process primitive locks already have tests. The remaining gap is actual
concurrent broker intake/run and process death at durable intent/effect/ack
boundaries. A named concurrent ledger-opener test opens sequentially. Do not
add redundant tests merely to increase counts; prove the missing composed
behavior with disposable subprocesses and harmless adapters.

## Windows content implementation dossier

Recommendation: port a narrow SteamCMD content backend for qualified Windows
10/11, ordinary local NTFS libraries and manually provisioned authentication.
Keep the current guard until this exists and passes native recovery checks.
Human install instructions, potentially a separately qualified install URI,
are an immediate **proposed** fallback; the repo does not currently implement
an install URI. That attended path does not meet unattended installation.
Consumer store-session remote downloads remain outside accepted authority.

| Missing piece | Concrete Windows implementation / acceptance |
| --- | --- |
| Process ownership/cancellation | Native executable atomically assigned to non-breakaway Job Object, non-inherited handle and kill-on-close; prove all writer processes exited before adoption/restart, not just leader exit. Qualify SteamCMD bootstrap/self-update and spaces/Unicode argv |
| Containment/identity | Handle-based volume/file identity and reparse inspection; bind markers/recovery to it. Initially reject junctions, UNC/device/ADS paths, unsupported filesystems, device names and trailing-dot/space/invalid components. Same-user mistake guarding, not adversarial owner isolation |
| Adoption durability | Destination-volume staged manifest, flushed files, durable intent and specified replacement/backup recovery. Do not make POSIX directory fsync a successful no-op; test actual filenames/hashes after failures, ACL preservation, sharing violation, disk full and reboot |
| Content lease | Replace hard-coded unknown download gate with qualified evidence. Exclude games/Remote Play/downloads/other relevant writers across all configured libraries and owner sessions. Unknown stays defer; another session is never shut down. Recheck policy/window/identity/client during work |
| Isolation/auth/deployment | Qualify Windows executable root/current-directory/cache/bootstrap behavior; HOME alone is not assumed isolation. Manual credential provisioning, typed auth-required and secret-environment redaction. Web API key is not SteamCMD login |
| Long-running desktop transport | Current scheduled worker limit is two minutes versus thirty-minute content budget. First qualify local interactive terminal; later define bounded content-task lifetime/cancellation/collection. Increasing timeout alone is insufficient |

Use small dedicated process, Windows filesystem/adoption and lease adapters,
reusing ledger/request/policy/result vocabulary. Version private journal and
identity records; unsupported old readers refuse them. Existing recovery can
resolve journals/restore under deny, while new SteamCMD work needs fresh grant.
No universal filesystem framework is required first.

Public Windows evidence supports the design constraints, not a implemented port:
[atomic process/job creation](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812),
[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[file identity](https://learn.microsoft.com/en-us/windows/desktop/api/winbase/ns-winbase-file_id_info)
and [directory handles](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).
[ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)
requires same-volume replacement/destination/backup and has unsupported
write-through behavior; some failures change filenames. Existing backup
locations may cross drives. [MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)
documentation alone does not prove POSIX-equivalent crash durability.
Valve's SteamCMD/protocol wiki returned access errors during this research,
so current install-dialog behavior was not verified.

Delivery ladder: proposed portability/recovery decision; disposable native
job/path/lock harness; fault-injected journal implementation; one prepared title
fresh install plus controlled official-file verify and independent installed
rescan; second drive/library and update/auth-expiry/client-relaunch/other-session/
revocation/scheduler/reboot cases; gaming-host rollout when idle. Keep title/depot
coverage explicit and separate first-run/DRM/playability/save assurance.

## Concrete verification design

| Missing composed proof | Fixture / boundary / expected result |
| --- | --- |
| Simultaneous intake and execution | Two real broker processes, shared synthetic ledger and per-user lock, barrier before intake/run. Same ID binds once; one durable operation and one harmless effect. Repeat separate state dirs to verify shared execution serialization |
| Dispatch crash cuts | Disposable injected command writes an acknowledgement file. Kill broker before intent, after committed intent but before effect, and after effect before ledger ack. Reopen/reconcile repeatedly; only pre-intent case can execute normally, committed uncertainty never blindly replays |
| Orphan content writer | Fake child writes bounded harmless bytes and exposes PID/create-time. Kill broker; next invocation proves writer presence and defers without restart/new writer. After exit, reconcile retains safe original-operation recovery |
| Poll/SQLite/wait/kill exceptions | Inject at abort check/ledger read/process wait/tree termination. Owned-child shutdown attempted and bounded; survivor/unknown retains nonterminal row and does not restart Steam. Synthetic exception text never appears in diagnostics |
| Installed broker upgrade | Separate installed environments open synthetic old ledger, active journal and terminal receipt; safe dispatch-pending reconciliation and exact binding reuse. Future schemas/readers reject. Do not roll back live ledger history |
| Time boundaries | Inject aware wall and monotonic clocks; exact window expiry, offset/naive input, backwards wall jumps and polling deadlines. State/event timestamp consistency, no unrelated freshness refresh |
| Delayed desktop worker | Original token/request job finishes after caller timeout or disconnect; fresh process collects exact receipt. Earlier not-found never permits replacement dispatch; cleanup waits for proved completion/ownership |

Existing coverage already includes cross-process primitive locks, expired direct
ID binding, fake delayed-worker-before-timeout and basic malformed/probe/nonce
semantics. Preserve it; the table targets composed gaps rather than duplicates.

Eight minimal effect-free agent trajectories: cached missing versus known empty;
confirmation nonce; transient authorized deferral; conflicting request ID;
lost reply followed by original-request inspection; delayed desktop worker;
attributed chooser without redispatch/playability claim; interrupted content
with surviving writer and later reconcile. Fixtures come from injected-backend
behavior and real parser validation; no native adapter is available in the
simulator. Score decisions/truth/safety separately from command/token economy.

## Reset handoff

The working tree already contains the previous broker/runtime/cross-platform
implementation and storage/discovery refactor. Do not discard those changes.
The preceding complete gate was 3,771 passed and 45 skipped. The final safety
repairs passed 3,909 tests with 45 skipped, plus locked sync, Ruff, builds and
installed-wheel smoke. Documentation and diff checks passed. All worker code
ownership and reviews are finished; do not discard the pre-existing changes.

Eleven independent review assignments across four rotating rounds are complete;
eight concrete safety/truth defects were repaired and independently checked,
and the final complete gate passed. Preserve
recommendations as **Proposed**. No new deletion,
external provider, credential, live Steam or native Windows operation is
authorized merely by its inclusion in this report.
