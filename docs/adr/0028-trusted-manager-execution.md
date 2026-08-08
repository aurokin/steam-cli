# ADR 0028: Trusted-manager execution on a single identity

Status: accepted 2026-08-08 (owner decision 2026-08-08; supersedes, for the
execution surface, [ADR 0027](0027-provisioned-execution.md)'s Decision
item 2 — the untrusted-submitter trust boundary: dedicated broker OS user,
local plan socket, second restricted SSH user, broker-held Discord bot token,
and Discord-interaction confirmation ("prose is never confirmation") — the
session-helper clause of its Decision item 3, and the same-OS-identity
premise of its "executor inside the planner" rejection. ADR 0027's planner
inertness (item 1), execution planes (item 3 otherwise), maintenance lease
(item 4), update contract (item 5), postconditions (item 6), move composite
(item 7), hard-deny classes (item 8), policy posture (item 9), Phase 0
evidence, and the human-only uninstall decision continue to govern.)

## Context

ADR 0027 modeled the LLM agent as an untrusted submitter behind an OS
identity boundary: plans over a local socket, confirmation over a broker-held
Discord connection. Phase 1 was built and hardened under that model but
deployed interim as the desktop user with a CLI confirm stand-in; the socket
daemon, Discord bot, and three-identity bootstrap were never built.

The owner then re-scoped the product: the agent is a trusted manager of this
machine's Steam library. The same trusted agent that plans operations reaches
the broker CLI directly over SSH as the desktop user, and human interaction
flows through the owner's existing conversation with that agent. In that
topology the confirmation ceremony no longer buys a boundary: the confirming
human is reached through the same conversation a prompt injection would
control, and the nonce round-trip degenerates to a copy-paste between two
lines of one shell session. The identity boundary added a cross-host
synchronization layer the product does not need, at the cost of the core
intent — giving the agent maximal information about and control over Steam
on this host.

## Decision

1. **Single identity, direct CLI.** The broker runs as the desktop user and
   is driven by the trusted manager agent over SSH via `steam-agent-broker`.
   The dedicated broker OS user, local plan socket, restricted agent SSH
   user, session helper, and broker-side Discord gateway are cancelled, not
   deferred. Human interaction, when wanted, flows through the agent's
   conversation with the owner.
2. **Authorization is a policy outcome.** Per-class grants are
   `allow | confirm | deny`. `allow`: the broker auto-authorizes at request
   time when the policy's limits pass, by consuming the operation's own
   freshly minted nonce in-process (`confirmation_actor = policy:<version>`,
   the content hash of the exact policy bytes that granted it). `confirm`:
   today's two-step flow, unchanged — the agent relays the owner's
   conversational approval by running the `confirm` verb; the actor string is
   provenance, not authentication. `deny`: unchanged. Nonces remain the
   ledger's single-use authorization token binding every executed operation
   to exactly one authorization event; the 4-hour execution window and
   single-active constraint are unchanged and apply to `allow` identically.
3. **Limits bound unattended cost.** A broker-measured free-disk floor
   (`[limits] min_free_gb`) is required whenever a grant is `allow` and is
   evaluated at auto-confirmation; when it fails (or cannot be measured) the
   request degrades to ordinary `pending_confirmation` so the owner can still
   explicitly approve. Per-day download budgets, maintenance windows, and
   per-app scoping remain future work with the scheduler.
4. **Custody posture restated** (previously carried by 0027 item 2): the
   broker state directory remains mode 0700 and exclusively holds the policy
   file, ledger, adoption journal, logs, and steamcmd's private HOME with its
   cached credentials. steamcmd keeps that private HOME as a `HOME`
   environment override — the Phase 0 clobbering finding is a mechanical
   two-writer fact about `config.vdf`, independent of identity count.
5. **The execution engine is retained unchanged**: ledger state machine and
   CAS transitions, maintenance-lease gates, client stop/restore contract,
   journaled single-manifest adoption, crash reconciliation, mount-identity
   checks, per-user lock. Its adversaries are the Steam client as an
   uncoordinated concurrent writer and power loss, not the submitter.
6. **Hard-deny classes are unchanged and now primary**: store, market,
   wallet, credentials, account settings, and unknown-capability operations
   fail closed as absent code, never policy entries. Uninstall remains
   human-in-Steam per ADR 0027's Phase 0 decision.
7. **Intake validation is retained**, re-rationalized as robustness against a
   confused or injected manager: bounded plan reads, typed appid, machine
   pinning, idempotency keys.
8. **The policy file is intent, not enforcement.** It is owner-editable and,
   being same-UID, agent-editable; it is the owner's recorded intent, an
   accident brake, and the kill switch (`install = "deny"`, re-read at every
   decision point, fails closed when unreadable).

## Security posture

This decision trades away prompt-injection resistance. A compromised or
misled agent can submit and self-authorize any operation the policy grants,
edit the policy file, and read everything in the broker state directory —
the ledger records what happened but prevents nothing. Two bounds are
distinct and must not be conflated. The broker's own bounds — hard-denied
classes, human-only uninstall, the disk floor, single-active — constrain
broker-mediated actions: within the broker surface the worst case is wasted
bandwidth and disk plus temporary unplayability, and never deleted saves,
commerce, or credential operations, because those are absent code paths, not
gated ones. But the agent holds a general shell as the desktop user, so the
real bound on a fully adversarial agent is the agent harness and the SSH
grant themselves — a trust that predates this ADR and is accepted with it.
The cached steamcmd login is a real credential whose exact blast radius has
not been audited; this ADR does not claim it is download-only. Documentation
must never describe the confirm verb, actor strings, or the ledger as
security boundaries; they are guard rails against mistakes and an audit
trail for the owner.

## Alternatives rejected

- Build the three-identity boundary anyway: permanent operational cost and a
  cross-host confirmation dependency, protecting against a party the owner
  has decided to trust with the capability itself; the ledger was only ever
  tamper-evident against an agent identity that no longer exists.
- Delete the nonce/confirm machinery: the `confirm` class still needs an
  atomic single-use binding between a displayed plan and its approval, and
  `allow` reuses the same hardened path for free.
- Ship declared- or measured-byte download budgets now: no accounting
  substrate exists; a declared cap is advisory under one UID; the measured
  disk floor bounds the same worst case. Revisit with the scheduler.
- Merge execution into the planner: still rejected — the separate
  `steam-agent-broker` entry point is what keeps ADR 0013's inert planner
  surface testable, even though the original prompt-injection premise of
  0027's rejection is now accepted fact rather than a boundary.

## Consequences

The manager loop is `request` (auto-authorized under `allow`) → `run` →
`status`/`reconcile`, two SSH round trips on the happy path; `confirm` mode
adds one, with the human step in the owner conversation. A crash in the
microsecond window between the request insert and the in-process confirm
leaves a pending row whose nonce was never emitted; it self-heals via the
existing 15-minute nonce expiry (`expire_lapsed`), which also frees the
idempotency key. Auto-confirmed rows are attributable in the ledger row and
its event to the exact policy version that granted them. The roadmap's
boundary-eval refusal matrix remains future work; the existing
planner-inertness eval is unaffected, and when boundary evals are built they
follow this model (no confirmation-transport rows; add
auto-grant-within-limits, degrade-on-limit, and revocation rows). The
"gates probe the invoking UID" declined finding dissolves: one UID is the
permanent design.

## Acceptance gates

`uv run ruff check .` and the full `uv run pytest -q` suite green, including
new tests: `allow` parses and requires the floor, auto-confirm authorizes
without emitting a nonce and records the policy actor, floor failure
degrades and stays explicitly confirmable, revocation still dead-ends
confirm and run. Documentation coherence in the same change: ADR register
and design map rows, AGENTS.md execution bullet, execution-plan.md,
execution-linux-session-model.md, and actions.md all updated per this ADR.
