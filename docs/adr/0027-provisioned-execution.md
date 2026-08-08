# ADR 0027: Provisioned execution behind a broker trust boundary

Status: proposed 2026-08-07 (supersedes the execution prohibition of
[ADR 0013](0013-m7-read-only-operation-plans.md) if accepted; preserves its
observation, ranking, and inert-plan clauses)

## Context

ADR 0013 accepted observe/rank/inert-plan capabilities and approved no
executable action class. The owner now requires agent-driven execution:
install, update, uninstall, launch, and move, commanded remotely from Discord
against an unattended Linux gaming PC, so the library stays curated and
current without human maintenance.

Steam still exposes no documented consumer administration API. The mechanism
review (five-angle brainstorm plus three adversarial review rounds, August
2026) rejected CEF/SteamClient.* injection (undocumented, churns with client
updates, silent breakage is disqualifying for an unattended daemon) and
cold-file surgery over game content (deferred; a future optimization ADR at
most), and converged on Valve-authored code only.

## Decision

Execution is a provisioned capability held by an OS identity boundary, not a
code path in the planner.

1. **Planner stays inert.** The read-only CLI surface retains ADR 0013's
   guarantees byte-for-byte; a regression eval enforces that the planner
   alone still never executes.
2. **Broker trust boundary.** A broker service under its own restricted OS
   user exclusively owns the execution policy, the operation ledger, the
   steamcmd installation and cached credentials, the Discord bot token, and
   the right to invoke execution mechanisms. The LLM-facing agent (a remote
   Hermes agent, reaching the machine over SSH as a second restricted user)
   can only submit plan documents over a local socket. Confirmations arrive
   as Discord slash-command/button interactions directly at the broker,
   bound to single-use broker-minted nonces (full plan hash, machine,
   operation, data impact, policy version, confirmer identity, execution
   window), consumed atomically. Prose is never confirmation.
3. **Two execution planes, Valve-authored code only.**
   Content plane: steamcmd from an isolated root, `force_install_dir` into
   the client library's `common/<installdir>`, followed by journaled atomic
   adoption of exactly one Valve-written `appmanifest_<appid>.acf`. Nothing
   else under the client's `steamapps/` is ever written.
   Client plane: the installed Steam client for lifecycle
   (`-silent`/`-shutdown`) and allowlisted launch (`-applaunch`), executed
   through a session helper in the desktop user's graphical session.
4. **Maintenance lease.** All content mutation runs inside an exclusive
   fail-closed lease: no running game, no Remote Play, no in-flight client
   download, idle threshold met, approved window or explicit "now"
   confirmation. Timeouts never grant permission; the client is never
   killed; prior client run-state is restored. One execution per Steam
   installation at a time.
5. **Recoverable in-place update contract.** Updates mutate working installs
   in place. This is accepted because only unmodified official depot content
   is exposed (modified installs refuse unattended update), the client never
   observes mixed state (manifest adopted only on verified success), and a
   deterministic per-transition recovery table maps every crash point to
   exactly one action. Failure cost is bounded at temporary unplayability
   plus re-downloaded bytes, never silent corruption.
6. **Semantic postconditions.** `content_present`, `client_adopted`, and
   `ready_to_play` are distinct. Unattended operations terminate at
   `client_adopted` + `first_run_required`; `ready_to_play` requires a
   human-attended first launch (Steam install scripts, EULAs, DRM, and
   anti-cheat run there). Launch results are terminal `dispatched`. Uninstall
   success means manifest and official content removed; residual user
   content (mods, saves) is inventoried and reported, never deleted.
   Reinstall is content reacquisition, never called rollback.
7. **Move-by-reinstall** is a single supervised composite (owner-approved):
   destination fresh-install and verification first, then one client-stopped
   manifest swap (never two adopted manifests). Invariant: at least one
   complete adoptable copy exists at every crash point. The composite
   terminates `confirmed_with_residue` — game playable at the destination,
   source residue inventoried — and source cleanup is a human step, per the
   uninstall decision below.
8. **Hard-deny classes, forever:** store, market, wallet, credentials,
   account settings, and any operation whose capability record is `unknown`
   (per-app steamcmd support is learned empirically and fails closed).
9. **Policy posture.** Human-declared intent, agent-executed steps, no
   credential or commerce automation, rate limits, append-only attributable
   ledger. steamcmd authentication is a renewable state owned by the broker;
   Guard challenges alert the owner and never retry-loop.

Delivery is gated: a Linux session-model document
([execution-linux-session-model.md](../design/execution-linux-session-model.md))
and a Phase 0 spike with predefined pass/fail thresholds precede any
implementation phase; each verb ships only after its spike line passes.

## Alternatives rejected

- CEF remote debugging of the client (total verb coverage, including
  download-queue control): silent breakage across client updates is
  disqualifying for unattended operation.
- Whole-`steamapps` alignment (symlinking steamcmd's library onto the
  client's): shares workshop state, `libraryfolders.vdf`, and staging
  directories between independent uncoordinated writers.
- Cold-file surgery for move/uninstall: deferred; move ships as the
  reinstall composite, and file-copy moves remain a future optimization ADR.
- Steam Remote Downloads automation: Valve-supported concept, but driving an
  authenticated store web session is the riskiest policy class; kept on file
  if measured steamcmd coverage is poor.
- Executor inside the planner process: a prompt-injected agent with the same
  OS identity could bypass any in-process confirmation layer.

## Consequences

Agents can keep the library installed and current from Discord, with
destructive verbs gated by structured human confirmation and standing grants
scoped by rule. The product promise for unattended work is "installed,
updated, and waiting" — never "ready to play" without attended evidence.
Boundary evals flip from refuse-always to a refusal matrix (unprovisioned,
ungranted, nonce missing/consumed/expired, hash mismatch, gate failure,
lapsed window, hard-deny, unknown capability, non-default install) with
execute-and-verify when granted. macOS and Windows become ports behind their
own session-model documents and spike gates.

## Acceptance gates

Phase 0 spike on the target machine with metadata backups: single-manifest
adoption coherence, `app_uninstall` reliability, owned-library coverage,
Guard token longevity, shutdown/process-tree detection, steamcmd output parse
stability, identity/ACL model validation, and kill-at-every-transition
recovery per the plan's reconciliation table. Eval matrix and regression
scenarios listed above implemented and passing; two-reviewer pass on the
execution schemas (`operation-execution/0.1`, plan schema bump).

## Phase 0 evidence (herb, 2026-08-07/08 — evidence in
`scripts/spike-phase0/results-herb/`)

- PASS — install + single-manifest adoption, 4/4 cycles (Spacewar ×3, Desk
  Job 4.2 GB): client indexes steamcmd-installed titles at StateFlags=4 with
  zero re-download. steamcmd writes its manifest under the
  `force_install_dir` target (deterministic location).
- PASS — shutdown choreography 5/5 (3–4 s full-tree exit vs 60 s threshold);
  `systemd-run --user` session-helper mechanism works from SSH.
- PASS — kill matrix automated cases: resume after kill -9 mid-download;
  torn-manifest checksum detection with journal-directed recovery.
- PASS — coverage 70/70 servable (34 native Linux depots, 36 Windows-depot/
  Proton via `@sSteamCmdForcePlatformType windows`). Caveat: depot visibility
  is a proxy; a sampled Proton-title install remains to be run.
- FAIL — `app_uninstall`: 4/4 silent no-ops with valid authentication;
  steamcmd cannot uninstall consumer titles. DECIDED (owner, 2026-08-08):
  uninstall remains human-present inside Steam — the agent ranks candidates
  and emits inert plans with in-Steam instructions, never deletes game
  content. Rationale: file deletion bypasses Valve's uninstall semantics
  (uninstall scripts, client bookkeeping) — it pulls the install out from
  under the client rather than uninstalling it. Move's source cleanup
  inherits this: the composite terminates `confirmed_with_residue` with
  human cleanup as the permanent design.
- FINDING (hardens this ADR's boundary): with a shared HOME, steamcmd stores
  its credential cache in the client's `config.vdf` and the running client
  clobbers it — the broker's private HOME/identity is empirically mandatory,
  not hygiene.
- FINDING: the client strips offline-added `libraryfolders.vdf` entries;
  library creation is UI-only (benign: execution targets existing libraries).
- OPEN: Guard-token longevity (clock started 2026-08-08 under private HOME),
  mid-game gate polarity run, manual mutual-exclusion case, sampled
  Proton-title install.
