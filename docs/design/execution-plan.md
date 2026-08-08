# Provisioned execution plan

Status: proposed working plan for [ADR 0027](../adr/0027-provisioned-execution.md); drafted 2026-08-07 after a five-angle mechanism brainstorm and three adversarial review rounds. Supersedes nothing until the ADR is accepted.

Goal: extend steam-agent (Python CLI, read-only today by ADR 0013) so an AI
agent, commanded from Discord, can keep a gaming PC's Steam library installed,
updated, and curated — unattended where granted, confirmed where not.

Scope discipline: v1 targets ONE OS — **Linux** (owner decision, resolved) —
specified completely, with named portability seams (executor adapters,
path/identity abstraction) but no pretense that the session/permission model is
already portable. Other OSes are later ports, each behind its own
Phase-0-equivalent acceptance gate.

Non-goals (v1): fine-grained download-queue control, CEF/SteamClient.* injection, cold-file
surgery (future ADR at most), any store/market/wallet/credential/
account-settings operation (hard-deny forever).

## 1. Threat model and trust boundary

The Discord-facing agent process is untrusted-by-default (prompt-injectable).
Execution is not a code path it can call; it is a capability held by a
different OS identity.

- A small **executor broker** runs as its own restricted OS user. It
  exclusively owns: the policy file, the operation ledger, the steamcmd
  installation + cached credentials, and the right to invoke steamcmd and
  client lifecycle commands. The agent reaches it only via a local
  authenticated socket carrying structured plan documents.
- **Topology (resolved)**: the LLM agent is a Hermes agent running on another
  host; it reaches the gaming PC over SSH. The CLI and broker operate on
  localhost only. The permissions circle resolves as three identities on the
  PC: desktop user (owns Steam + library + a small session helper for client
  lifecycle/launch), broker user (systemd service; owns policy, ledger,
  steamcmd root + credentials, Discord bot token; library write via
  group/ACL), and a restricted SSH login user for the agent (owns neither;
  its only capability is the broker's local socket). The Hermes agent's SSH
  key lands in that restricted user's authorized_keys — never the desktop
  user's. Concrete ACL layout is the Item 0 doc (§12), validated in Phase 0.
- The ledger is an operational log; it is tamper-evident against the agent
  identity only via these OS permissions. (Optional later: hash-chained
  records to a remote sink.)
- Proportionality: personal machine, not a bank — but the identity boundary is
  cheap now and impossible to retrofit later.

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

## 6. Remote confirmation (Discord)

- Plan hashes identify WHAT; authorization is a **random single-use nonce**,
  minted broker-side, bound to: full plan hash, machine id, operation+appid,
  expected data impact, policy version, confirmer's immutable Discord user id
  and channel, and expiry. Consumed atomically in the ledger.
- **Deferred-execution rule**: the nonce carries an execution window shown at
  confirmation time ("will run within N hours / at tonight's window"). If the
  lease isn't acquired within that window, the operation expires and requires
  fresh confirmation — a "now" confirm never silently becomes a
  next-day execution.
- Confirmation is a **Discord slash-command/button interaction** (structured,
  identity-carrying). The agent relays and explains plans; it never decides
  that prose was a confirmation.
- Transport (resolved): the Hermes agent reaches the PC over SSH as the
  restricted user; agent↔broker is a localhost socket behind that. The broker
  holds the Discord bot token and maintains its own outbound gateway
  connection, so confirmations (slash-command/button) flow Discord → broker
  directly — they never pass through the LLM agent or the SSH channel.

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
loop. The cached token IS a credential and lives in the broker identity's
profile, unreadable by the agent user. Onboarding expects one interactive
login per machine; token longevity is measured in Phase 0 and the design
assumes re-auth interrupts happen.

## 9. Preflight

Per operation: disk space (download + staging headroom), library mount
present and writable, no pending client self-update, network reachable; sleep
inhibited for the duration; bounded timeouts everywhere; unknown states fail
closed.

## 10. Move-by-reinstall (owner-approved), as a supervised composite

Move is approved as reinstall-to-destination + removal-at-source. Review
established it is a transaction, not two independent operations — so it is
built as ONE ledger operation with sub-states, one confirmation, and one
governing invariant:

**Invariant: at every crash point, at least one complete, adoptable copy of
the game exists.** The source is never degraded until the destination is
verified.

Sequence (single nonce; single lease where steps touch manifests):

1. `dest_downloading` — fresh-install path (§4) into the destination
   library's `common/<installdir>`. No manifest adopted; the client still
   sees only the source install. Failure here aborts the whole move with the
   source untouched.
2. `dest_verified` — `content_present` proven (build/depot ids, dir sanity).
3. `manifest_swap` — one client-stopped adoption step: back up + remove the
   source library's manifest, place the destination manifest. Ordering rule:
   the two libraries NEVER both have an adopted manifest for the appid, and
   the swap is journaled/checksummed like any adoption (§5 adopting rows
   apply; restore direction = source manifest).
4. `client_adopted` at destination verified after restart.
5. `source_cleanup` — HUMAN step (per the §10a uninstall decision: the agent
   never deletes game content). The unattended composite terminates at
   `confirmed_with_residue`: game fully playable at the destination, source
   content inventoried and reported, and the plan hands the owner the exact
   in-Steam cleanup instructions. Note the destination manifest is the
   adopted one, so the client's own uninstall UI on the residue is safe —
   it only sees the destination copy; source residue is plain directory
   cleanup the owner performs (or leaves).

Honesty requirements in the plan/confirmation text: full re-download of the
stated GB (acceptable for retro-scale titles — this is why file-copy moves
stayed out); source-side mods/saves/configs in the install dir follow the
uninstall rules (§7): inventoried, warned about before confirmation, reported,
never deleted as part of cleanup.

Dependencies: requires only the install path (Phase 1) — ships as Phase 2d.
`confirmed_with_residue` + human source cleanup is the permanent design, not
a degradation (§10a).

Cold-file surgery (staged copy, checksum, manifest switch without
re-download) remains deferred to a future ADR as the optimization for
large titles.

## 10a. Uninstall mechanism (DECIDED 2026-08-08: human-in-Steam only)

`app_uninstall` is dead: 4/4 silent no-ops on the current steamcmd with valid
auth (Phase 0, herb). Owner decision: **uninstall stays human-present,
executed inside Steam** (the plan carries the `steam://uninstall/<appid>`
reference and UI instructions; the agent never deletes game content).

Rationale (owner + review finding 16 agree): broker file-deletion only
mimics uninstall — it pulls the install out from under the client, skipping
Valve's uninstall semantics (uninstall scripts, per-user cleanup, the
client's own bookkeeping). Rejected. CEF `SteamClient.Apps.Uninstall` stays
rejected for silent-breakage risk.

Consequences: the unattended curation loop can install, update, repair, and
launch, but disk reclamation requires the owner in the Steam UI — the agent
ranks and proposes uninstall candidates (existing storage-ranking capability)
and hands over an inert plan. Move's source cleanup inherits this: see §10.

## 11. ADR and eval changes

- New ADR supersedes 0013: "read-only by default; execution by explicit
  provisioned grant." Preserves 0013's observation/ranking/inert-plan clauses;
  adds: broker trust boundary + three-identity model, two-plane mechanism
  decision, maintenance lease, fresh-vs-update mutation policy,
  single-manifest adoption rule, first-run honesty, VSA posture
  (human-declared intent, agent-executed steps, no credential/commerce
  automation, rate limits), the move-by-reinstall composite and its
  one-complete-copy invariant, and explicit deferral of cold-surgery/CEF.
- Boundary evals become a matrix: refuse when (broker not provisioned | no
  grant | nonce missing/consumed/expired | plan hash mismatch | gate failed |
  execution window lapsed | hard-deny class | capability unknown | non-default
  install), execute-and-verify when granted. Regression eval: the planner
  surface alone still never executes. New evals: mid-game request defers;
  nonce replay rejected; crash reconciliation per table row; agent never
  claims `ready_to_play` unattended; uninstall never touches residual user
  content; move never leaves fewer than one complete copy (kill-at-every-
  sub-state), never two adopted manifests, and source cleanup only targets
  the pinned source path.

## 12. Delivery phases

- **Item 0 (before Phase 0 code): Linux session model.** One document: the
  three identities and their ACLs; broker as a systemd system service; the
  desktop-user session helper (systemd user service) the broker commands for
  client start/stop/launch (the Steam client needs the graphical session;
  steamcmd does not); presence/idle gates via logind + process observation;
  autologin posture for unattended windows; SSH restricted-user setup.
  Phase 0 validates it.
- **Phase 0 — spike**, safe boundaries: disposable secondary library + full
  metadata backup, free/small titles first then owner account against the
  disposable library, pass/fail thresholds defined before testing,
  kill-at-every-step and reboot-recovery per the §5 table. Measures:
  (1) force_install_dir + single-manifest adoption coherence (incl. updates
  both directions), (2) app_uninstall reliability, (3) coverage % across the
  owner's real library incl. DLC/Family/third-party-launcher titles,
  (4) Guard token longevity, (5) `-shutdown` + process-tree-exit detection,
  (6) steamcmd stdout parse stability, (7) the identity/ACL model works.
- **Phase 1 — broker + ledger + install/update only.** Explicit confirmation
  for everything, all work serialized, every other verb denied. Smallest
  thing that delivers the use case (library kept current from Discord).
- **Phases 2a/2b/2c/2d — independent verb additions**, each gated on its own
  spike line and shippable alone: 2a uninstall-as-inert-plan (candidate
  ranking + inventory + in-Steam instructions; human executes, per §10a),
  2b repair, 2c launch allowlist (dispatched-terminal), 2d move-by-reinstall
  (composite of §10; terminates `confirmed_with_residue`, human source
  cleanup).
- **Phase 3 — standing grants + scheduler**: allow_unattended grants scoped by
  appid set/rule, maintenance windows, overnight cycles, budgets (max GB
  downloaded/deleted per day), kill switch. Then second-OS port behind its own
  gate.
- **Phase last (ideally never)** — move / cold-file surgery ADR.

## Owner decisions (all resolved 2026-08-07)
1. Target OS: **Linux**.
2. Spike posture: owner account against a disposable library with backups —
   host is not the owner's primary gaming machine, disruption acceptable.
3. Topology: CLI/broker are localhost-only on the PC; the LLM agent is a
   Hermes agent with SSH access, logging in as the restricted user. Broker
   holds the Discord bot token; confirmations bypass the agent entirely.
4. Launch ships as Phase 2c with dispatched-only honesty.
