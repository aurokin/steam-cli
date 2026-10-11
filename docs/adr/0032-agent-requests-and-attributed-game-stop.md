# ADR 0032: Agent requests and attributed game stopping

Status: accepted 2026-10-10 by the project owner for implementation following
review of broker reliability and Windows/Linux host requirements. Native
qualification is recorded separately; acceptance does not claim game-action
validation on either platform.

## Context

The owner needs personal agents to inspect a library and reliably launch or
stop games on Windows and Linux. M1–M7 already supply library evidence; the
broker supplies install, verify and launch. The previous launch sequence could
replay after a crash between the external command and its ledger transition,
and could dispatch after authorization or activity changed during startup.
Requiring agents to manufacture new planner inputs to obtain a new attempt
also makes an otherwise useful broker unnecessarily difficult to operate.

## Decision

- Keep `steam-agent` Steam-read-only and preserve its schema `0.1` contracts.
  Keep all execution in `steam-agent-broker`; no daemon or MCP layer is needed.
  Run the broker on each gaming host as its desktop user. Existing SSH can
  invoke it remotely; a machine alias is an identity, not a transport.
- Add `stop` as the fourth executable class. Its grant and per-AppID allowlist
  are independent of launch. `[stop] allow_force` defaults to false; `--force`
  must be requested explicitly and authorized independently. Stop-only `allow`
  needs no disk floor because it writes no game content.
- Attribute process evidence through consistent Steam AppID environment fields,
  limited to the invoking user and, on Windows, the same interactive session.
  Exclude shared Steam/Wine infrastructure. Check PID and creation time again
  immediately before each action. Missing or conflicting target evidence refuses action. A partial scan may
  request exit only for positively attributed, freshly revalidated targets;
  inaccessible evidence keeps completion unknown. This can observe manually Steam-started games; it does not
  claim to recognize every running game.
- Graceful stop requests SIGTERM on Linux and WM_CLOSE on Windows. Neither
  proves that saves completed. A timeout never escalates to force. Force uses
  the OS termination primitive only for freshly attributed process identities.
- Launch and stop write `dispatch_pending` before the external action. Launch
  ends `dispatched` only on acknowledgement. Stop ends `dispatched` when the
  attributed processes exited or were already absent, with explicit detail.
  Uncertain dispatch ends terminal `unconfirmed`; reconciliation never replays
  it. This amends ADR 0031's direct authorized-to-dispatched transition.
- Recheck launch activity, client presence, authorization window and current
  policy after client startup and immediately before durable dispatch intent.
  Stop rechecks its window and policy after fresh process scanning and before
  each exit request; denial after durable intent stays terminal and preserves
  any requests already sent.
- Add direct broker operation verbs with caller-supplied request IDs. Bind the
  ID to exact intent, account alias and machine. A retry returns the existing
  nonce or receipt and resumes only an already authorized retryable operation;
  it never replays terminal results. Expired direct requests retain their receipt
  and binding; another attempt needs a new ID. Legacy stdin-plan intake remains
  supported with its existing expiry semantics.
- Client-only provisioning requires no SteamCMD. Content provisioning maps an
  account alias explicitly to a Steam login; aliases are not login names.
  Windows launch/runtime adapters and portable locks are included. Windows
  install/verify remain unavailable until content adoption and recovery are
  qualified there. Existing Linux content behavior remains separate.

## Alternatives and consequences

Process-name matching and blanket process-tree killing were rejected because
launchers, shared Wine infrastructure and PID reuse can target unrelated work.
An unofficial backend replacement was rejected as an immediate migration:
[Aurelia](https://github.com/Drackrath/Aurelia) offers useful comparison material,
but its public launch documentation limits stop tracking to its own launches
and does not establish this broker's durable recovery contract.

No database migration is required: ledger states are unconstrained text, and
the active-operation index already derives its predicate from terminal states.
Old rows remain readable. Old broker binaries must not run against new active
`dispatch_pending` rows; reconcile with the new binary before downgrading.

Runtime results are separate process observations and never upgrade a launch
receipt to proof of playability. Wishlist mutation, purchase, wallet, market,
credentials, uninstall and move retain their earlier boundaries. Research and
remaining qualification are in [agent host support](../design/agent-host-support.md).

## Evidence

Focused tests exercise crash boundaries, startup expiry/activity/revocation,
request receipt reuse and content binding, independent stop/force permission,
PID reuse, manual-launch attribution, infrastructure exclusions, Windows window
ownership, inaccessible observations and portable lock contention. Controlled
clocks also remove calendar dependence from retention tests. Real game launch,
stop and content recovery are separate acceptance checks, not inferred from
passing unit tests.

Primary OS references: [psutil process API](https://psutil.io/api/),
[WM_CLOSE](https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-close),
[ProcessIdToSessionId](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-processidtosessionid),
and [window ownership](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid).
