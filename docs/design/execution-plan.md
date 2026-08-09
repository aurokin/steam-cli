# Provisioned execution plan

Status: working plan under [ADR 0027](../adr/0027-provisioned-execution.md)
as re-scoped by [ADR 0028](../adr/0028-trusted-manager-execution.md)
(trusted-manager model, single identity); Phase 1 implemented
(`steam-agent-broker`).

Goal: extend steam-agent (Python CLI, read-only today by ADR 0013) so the
owner's trusted manager agent, driving the broker CLI directly over SSH, can
keep a gaming PC's Steam library installed, updated, and curated — unattended
where granted, confirmed where not.

Scope discipline: v1 targets ONE OS — **Linux** (owner decision, resolved) —
specified completely, with named portability seams (executor adapters,
path/identity abstraction) but no pretense that the session/permission model is
already portable. Other OSes are later ports, each behind its own
Phase-0-equivalent acceptance gate.

Non-goals: fine-grained download-queue control, CEF/SteamClient.* injection,
cold-file surgery (removed from the roadmap by ADR 0029), any store/market/
wallet/credential/account-settings operation (hard-deny forever).

## 1. Trust model (re-scoped by ADR 0028)

The agent is a **trusted manager** of this machine's Steam library, not an
untrusted submitter. It reaches `steam-agent-broker` directly over SSH as
the desktop user; the broker runs as that same user (single identity). The
agent remains an LLM and therefore injectable — that risk is accepted, and
bounded by the broker surface rather than by an identity boundary:

- **Hard-denied classes are absent code, not policy entries**: store,
  market, wallet, credentials, account settings, content deletion. Within
  the broker surface the worst case is wasted bandwidth and disk plus
  temporary unplayability — never deleted saves, commerce, or credential
  operations.
- **The policy file is the owner's recorded intent**, an accident brake,
  and the kill switch (`install = "deny"`, re-read at every decision point,
  fails closed when unreadable). It is not an enforcement boundary against
  the agent, which shares the broker's UID.
- **The ledger is an audit trail** for the owner, not tamper-proof against
  the agent. Human interaction, when wanted, flows through the owner's
  existing conversation with the agent.

## 2. Execution planes and verb matrix

Two planes, Valve-authored code only:

- **Content plane (steamcmd, isolated root + PRIVATE HOME — Phase 0 proved a
  shared HOME lets the client clobber steamcmd's credential cache)**: install,
  routine update (`app_update <appid>` — plain), repair
  (`app_update ... validate` — a separate capability with its own confirmation
  text: heavy, replaces modified official files → explicit mod warning).
  Uninstall: `app_uninstall` FAILED its Phase 0 spike (4/4 silent no-ops with
  valid auth) — open mechanism decision, see §10a.
- **Client plane (installed Steam client)**: lifecycle (`-silent`,
  `-shutdown`), launch (`-applaunch` / `steam://rungameid/`) for a per-app
  allowlist with explicit confirmation. Launch's terminal result is
  `dispatched` — process observation does NOT upgrade it (seeing a process
  cannot distinguish a playable game from a hung launcher or DRM prompt).

Per-app, per-account **content capability record**: `supported | unsupported |
unknown`, learned empirically (steamcmd consumer coverage is not contractual:
"No subscription", encrypted/shared depots, DLC, Family licenses,
third-party-launcher titles). `unknown` fails closed → falls back to an inert
human plan. Non-default installs (beta branch, platform override, language,
DLC deltas) are refused by precondition until explicitly supported; the pre-op
ACF and steamcmd's output manifest are compared before adoption.

Considered Valve-native alternative, on file: **Steam Remote Downloads**
(authenticated web/mobile session asks the running client to install — the
supported remote-install path). Automating it means driving an authenticated
store web session — the riskiest policy class — so it is not a v1 plane;
revisit if measured steamcmd coverage is poor.

## 3. Maintenance lease (the handoff protocol)

All content-plane work happens inside an exclusive, fail-closed **maintenance
lease** per Steam installation:

- Gates (ALL must pass; re-checked immediately before every destructive
  transition): no running Steam game process; no Remote Play/VR session; no
  client download transaction in flight; no recent interactive input
  (configurable idle threshold); no other OS user logged in with library
  access; interactive-session state matches the target-OS model (locked-screen
  and no-session behavior are specified there, §12 item 0); within an approved
  maintenance window unless this operation was confirmed "now".
- Gate failure defers or aborts. A timeout is never permission to proceed.
  The client is never killed as a fallback.
- Record whether the client was running before the lease; restore that state
  after.
- **Global single-writer**: exactly one execution per Steam installation,
  enforced by an OS-level exclusive lock + the ledger. If the client reappears
  mid-operation (auto-start, human), steamcmd is aborted and the operation
  parks in `interrupted` (non-terminal) for reconciliation.

## 4. Content mutation: the recoverable in-place update contract

steamcmd runs from an **isolated root** (own steamapps, depotcache, credential
profile; owned by the broker). Never symlink its steamapps onto the client
library's (shares workshop state, libraryfolders.vdf, staging dirs, every
manifest — independent writers over undocumented shared metadata; demoted to a
disposable-lab experiment only).

Two mutation cases, different risk, handled differently:

- **Fresh install** (no live install to damage): `+force_install_dir
  <library>/steamapps/common/<installdir>` directly. A crash leaves a partial
  directory that is invisible to the client (no manifest adopted); recovery is
  resume (`app_update` continues) or cleanup-delete of the partial dir —
  recorded in the adoption journal, never ambiguous.
- **Update of an existing install**: an in-place mutation of a working game,
  and the plan says so honestly rather than claiming atomicity it doesn't
  have. The accepted contract: a crashed update MAY leave the install
  partially overwritten, and that is acceptable because (a) only unmodified
  official depot content is ever exposed to it — installs with detected local
  modifications are refused unattended update and require explicit
  confirmation with a mod warning, so everything at risk is re-downloadable
  by definition; (b) the client never sees mixed state — its manifest is
  backed up before steamcmd runs, adoption happens only on verified success,
  and the client is not restarted against this app's library until recovery
  completes; (c) recovery is deterministic (§5). The failure cost is
  bounded: temporary unplayability + re-downloaded bytes, never silent
  corruption or misrepresentation. (Stage-and-swap for precious installs is
  cold-surgery class; explicitly out of v1.)

**Internal recovery-repair**: `app_update <appid> validate` exists from
Phase 1 as a broker-internal recovery mechanism — it completes or restores
consistency for an already-authorized operation and needs no fresh
confirmation, because it can only reacquire official content for the app the
owner already authorized mutating. It is distinct from the user-facing
`repair` verb (Phase 2b), which is separately confirmable precisely because a
user-invoked validate can replace mods.

**Manifest adoption** is a journaled, atomic, single-file operation: place the
Valve-written `appmanifest_<appid>.acf` (minimally patched, e.g. `installdir`)
into the client's `steamapps/`, prior manifest backed up first, only while the
lease holds and the client is fully stopped. Nothing else in the client's
steamapps is ever touched. If adoption proves unreliable on the current
client, that is a spike failure → stop and reassess, never escalate to broader
writes.

## 5. Durable execution state

Three mechanisms with named responsibilities (not pretended into one):

- **Operation ledger (SQLite)** — the state machine and system of record:
  `authorized → lease_acquired → client_stopping → content_running → adopting
  → client_restart_pending → verifying → {confirmed | unconfirmed |
  contradicted | aborted | failed}`, plus non-terminal `interrupted` (successor:
  reconciliation → resume | repair | abort). One row per operation: plan hash,
  grant matched, nonce consumed, mechanism, per-state timestamps, outcome.
  Nonce consumption and idempotency claims are atomic DB transitions.
- **OS-level exclusive lock** — cross-process mutual exclusion (the ledger
  can't stop a second process that hasn't read it yet).
- **Adoption journal** — filesystem-side intent records + manifest backups for
  the single-file adoption and staged/partial content, enabling exact
  reconciliation.

**Recovery transition table** — deterministic: each evidence combination maps
to exactly one action. Startup reconciliation walks every non-terminal row.
"Window valid" = the confirmed execution window (§6) has not lapsed.

| Died during | Evidence | Action (exactly one) |
| --- | --- | --- |
| authorized / lease_acquired | no side effects | abort; record nonce outcome `expired` |
| client_stopping | client running | abort (no side effects yet) |
| client_stopping | client stopped, window valid | continue to content_running |
| client_stopping | client stopped, window lapsed | abort; restore prior client state |
| content_running (fresh) | window valid | resume `app_update` |
| content_running (fresh) | window lapsed | cleanup-delete partial dir (journaled); mark `failed` |
| content_running (update) | window valid | resume `app_update` |
| content_running (update) | window lapsed | internal recovery-repair (§4) to restore consistency; then verifying decides terminal state |
| adopting | journal shows new manifest fully written (checksum matches) | complete the swap |
| adopting | journal shows swap incomplete | restore backed-up manifest; then content_running rules apply to the content |
| client_restart_pending | prior state = running | restart client |
| client_restart_pending | prior state = not running | leave stopped |
| verifying | any | re-run verification; outcome decides terminal state |

The adoption journal records the new manifest's checksum before placement, so
"fully written" is decidable, making the adopting rows unambiguous.

Crash/reboot/disk-full/network-loss at each transition is a required Phase 0
test, not an afterthought.

## 6. Authorization (re-scoped by ADR 0028)

- Authorization is a policy outcome per operation class:
  `allow | confirm | deny`. Under `allow` the broker auto-authorizes at
  request time within the policy's limits (measured free-disk floor;
  degrade to pending when it fails or cannot be measured). Under `confirm`
  the agent relays the owner's conversational approval by running the
  `confirm` verb; the actor string is provenance, not authentication.
- The **nonce** is a broker-minted single-use token binding one request row
  to exactly one authorization event — consumed in-process under `allow`,
  via the CLI verb under `confirm`. It is mistake-guarding and audit, not a
  security boundary against the agent. Consumed atomically in the ledger.
- **Deferred-execution rule**: authorization carries an execution window.
  If the lease isn't acquired within that window, the operation expires and
  requires fresh authorization — a "now" approval never silently becomes a
  next-day execution.

## 7. Postconditions: semantic, per verb, honest about first-run

- `content_present` — steamcmd completed (structured stdout parse + expected
  build/depot manifest ids + bytes/dir sanity; never exit code alone).
- `client_adopted` — after client restart, the client indexes the app at the
  expected build without triggering re-download.
- `ready_to_play` — never claimable unattended. Steam install scripts
  (registry writes, redistributables, sometimes elevation), EULAs,
  third-party-launcher logins, DRM activation, anti-cheat setup can all run on
  first launch (Valve-documented). Unattended operations terminate at
  `client_adopted` + `first_run_required`; `ready_verified` is set only after
  a human-present (or user-attended Remote Play) first launch. The product
  promise is "installed, updated, and waiting."
- **Uninstall**: success = adopted manifest removed + OFFICIAL content removed.
  Residual user content (mods, saves, configs Steam leaves behind) is
  inventoried and REPORTED, never deleted — its removal would be a separate
  destructive capability we do not build. Uninstall is irreversible for
  anything beyond depot content; reinstall is "content reacquisition", never
  called rollback. Destructive plans present the unexpected-file inventory
  before confirmation.

## 8. Authentication as a state, not an event

steamcmd auth is a renewable state (`authenticated | auth_required |
unknown`) owned by the broker. Fail-fast noninteractive settings; any Guard
challenge or token invalidation → `auth_required` + owner alert, never a retry
loop. The cached token IS a credential and lives in the broker's private
steamcmd HOME (a `HOME` override under the state directory); Guard alerts
surface through broker output into the owner conversation. Onboarding expects one interactive
login per machine; token longevity is measured in Phase 0 and the design
assumes re-auth interrupts happen.

## 9. Preflight

Per operation: disk space (download + staging headroom), library mount
present and writable, no pending client self-update, network reachable; sleep
inhibited for the duration; bounded timeouts everywhere; unknown states fail
closed.

## 10. Move (DECIDED 2026-08-08: inert plan, human executes in Steam)

Steam moves an installed title between libraries in place from its storage
management UI, with no re-download. That is an officially supported consumer
feature, so this project plans the move and the human performs it — the same
reasoning as uninstall (§10a). See
[ADR 0029](../adr/0029-move-as-inert-plan.md).

The whole surface is the existing inert plan: `steam-agent operations plan
move APPID --destination-library-ordinal N`, carrying preconditions, the
destination-capacity risk, storage-UI instructions, and rollback guidance.
The broker gains no move operation class.

Superseded by that decision (do not rebuild): the reinstall-to-destination
composite, its `dest_downloading`/`dest_verified`/`manifest_swap`/
`source_cleanup` sub-states, the `confirmed_with_residue` terminal state, and
the one-complete-copy invariant. They existed because re-downloading was the
only move a broker could perform under the Valve-authored-code rule; the
supported in-client move is cheaper and leaves no residue to clean.

Cold-file surgery is off the roadmap entirely rather than deferred: it was an
optimization for the composite's download cost, which no longer exists.

## 10a. Uninstall mechanism (DECIDED 2026-08-08: human-in-Steam only)

`app_uninstall` is dead: 4/4 silent no-ops on the current steamcmd with valid
auth (Phase 0, target machine). Owner decision: **uninstall stays human-present,
executed inside Steam** (the plan's UI instructions include the
`steam://uninstall/<appid>` shortcut as instruction text — schema `0.1` fixes
typed references to HTTPS pages — and the agent never deletes game content).

Rationale (owner + review finding 16 agree): broker file-deletion only
mimics uninstall — it pulls the install out from under the client, skipping
Valve's uninstall semantics (uninstall scripts, per-user cleanup, the
client's own bookkeeping). Rejected. CEF `SteamClient.Apps.Uninstall` stays
rejected for silent-breakage risk.

Consequences: the unattended curation loop can install, update, repair, and
launch, but disk reclamation requires the owner in the Steam UI — the agent
ranks and proposes uninstall candidates (existing storage-ranking capability)
and hands over an inert plan. Move follows the same shape for the same
reason (§10), and leaves no residue at all: Steam's own storage UI relocates
the files in place.

## 11. ADR and eval changes

- ADR 0027 (accepted) supersedes 0013's execution prohibition for the broker
  surface; ADR 0028 (accepted) re-scopes 0027 to the trusted-manager
  single-identity model with policy-gated authorization.
- The boundary-eval refusal matrix remains future work under the 0028 model:
  refuse when (no grant | nonce missing/consumed/expired | gate failed |
  execution window lapsed | hard-deny class | capability unknown |
  non-default install), plus auto-grant-within-limits, degrade-on-limit, and
  revocation rows; no confirmation-transport rows. The existing regression
  eval — the planner surface alone still never executes — is retained.
  Future evals: mid-game request defers; nonce replay rejected; crash
  reconciliation per table row; agent never claims `ready_to_play`
  unattended; uninstall never touches residual user content; the broker
  refuses move outright (ADR 0029) rather than attempting one.

## 12. Delivery phases

- **Item 0 (before Phase 0 code): Linux session model.** One document (now
  single-identity per ADR 0028): the broker CLI runs in the desktop user's
  session; client lifecycle via `-shutdown`/`systemd-run --user`;
  presence/idle gates via process observation; autologin posture for
  unattended windows. Phase 0 validated it.
- **Phase 0 — spike**, safe boundaries: disposable secondary library + full
  metadata backup, free/small titles first then owner account against the
  disposable library, pass/fail thresholds defined before testing,
  kill-at-every-step and reboot-recovery per the §5 table. Measures:
  (1) force_install_dir + single-manifest adoption coherence (incl. updates
  both directions), (2) app_uninstall reliability, (3) coverage % across the
  owner's real library incl. DLC/Family/third-party-launcher titles,
  (4) Guard token longevity, (5) `-shutdown` + process-tree-exit detection,
  (6) steamcmd stdout parse stability, (7) the identity/ACL model works
  (retired by ADR 0028: single identity is the permanent design).
- **Phase 1 — broker + ledger + install/update only. IMPLEMENTED**
  (`steam-agent-broker`), all work serialized, every other verb denied.
  Originally "explicit confirmation for everything"; superseded by ADR
  0028's policy grants (`allow` within limits auto-authorizes).
- **Phases 2a/2b/2c — independent verb additions**, each gated on its own
  spike line and shippable alone: 2a uninstall-as-inert-plan — IMPLEMENTED
  (reclaim-space ranking, per-app residual inventory measured by the
  installed scan and reported as plan risk plus a ranking gate, and in-Steam
  instructions with the `steam://uninstall/<appid>` shortcut; human executes,
  per §10a) — 2b repair — IMPLEMENTED as the `verify` operation class
  ([ADR 0030](../adr/0030-verify-as-a-second-executable-class.md)) — and
  2c launch allowlist, dispatched-terminal — IMPLEMENTED
  ([ADR 0031](../adr/0031-launch-allowlist-dispatched-terminal.md)). A former 2d
  (move-by-reinstall) is cancelled by ADR 0029 — move ships as an inert plan
  and needs no broker work. **Phase 2 is complete.** The remaining execution
  work is the §11 boundary-eval refusal matrix (unbuilt; the broker's
  refusals are unit-tested only) and Phase 3.
- **Phase 3 — unattended policy limits** (owner decision 2026-08-08:
  scheduling machinery is explicitly out of scope for this project). The
  loop that decides when to act — walking the backlog, picking a moment,
  invoking the CLI — belongs to the owner's agent and its own scheduler,
  which needs no code here. This project ships only the limits that must be
  *enforced at authorization*, because a confused or injected driver must
  not be able to talk its way past them: maintenance windows, per-day byte
  budgets, and per-appid grant scoping. (Standing grants shipped early as
  ADR 0028's `allow`; the kill switch already exists as
  `install = "deny"`.) Then second-OS port behind its own gate.

## Owner decisions
1. (2026-08-07) Target OS: **Linux**.
2. (2026-08-07) Spike posture: owner account against a disposable library
   with backups — host is not the owner's primary gaming machine,
   disruption acceptable.
3. (2026-08-07) Topology: CLI/broker are localhost-only on the PC; the LLM
   agent reaches it over SSH. (Restricted-user/Discord-transport clauses
   superseded by decision 5.)
4. (2026-08-07) Launch ships as Phase 2c with dispatched-only honesty.
5. (2026-08-08) Trusted-manager re-scope: the agent drives the broker CLI
   directly as the desktop user; confirmation demoted to policy grants.
   See [ADR 0028](../adr/0028-trusted-manager-execution.md).
