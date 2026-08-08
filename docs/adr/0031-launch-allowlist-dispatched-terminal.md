# ADR 0031: Launch is allowlisted, client-plane, and terminates dispatched

Status: accepted 2026-08-08 (Phase 2c of
[ADR 0027](0027-provisioned-execution.md)'s delivery plan, under
[ADR 0028](0028-trusted-manager-execution.md)'s trusted-manager model,
following [ADR 0030](0030-verify-as-a-second-executable-class.md). Completes
the broker's planned verb set; no earlier decision is reversed.)

## Context

ADR 0027 placed launch on the client plane with two properties fixed from
the start: a per-app allowlist, and a terminal result of `dispatched`
because process observation cannot prove a game is playable. Phase 2c is
that design becoming code.

Launch is unlike install and verify in every operational respect. It mutates
no content, so there is nothing to make recoverable: no lease, no manifest
adoption, no journal, no resume. What it does instead is take over the
machine's foreground, which is a different kind of consequence and needs a
different kind of bound.

## Decision

1. **Launch is a client-plane operation with its own executor path.** It
   takes no maintenance lease, stops nothing, runs no steamcmd, and adopts
   no manifest. It starts the client if absent and issues Valve's
   `steam -applaunch <appid>`.
2. **Two independent permissions.** The `launch` grant says launching is a
   capability at all; `[launch] allowed_appids` says which titles. Both are
   checked at request, at confirm, and again at run. A live grant with an
   empty or absent allowlist is a policy error, not "any game" — the
   ambiguity is refused rather than resolved.
3. **`dispatched` is terminal and never upgraded.** The client accepted the
   request; that is the whole claim. Seeing a process afterwards cannot
   distinguish a playable game from a hung launcher or a DRM prompt, so
   nothing observes its way to a stronger statement. `run` exits 0 on
   `dispatched`.
4. **Human activity defers, it does not fail.** A running game or a Remote
   Play session leaves the row authorized for a later retry. A download in
   flight does not block a launch — the client handles that itself.
5. **No resume.** An interrupted launch has no half-done state to recover,
   so `dispatched` is reachable only from `authorized` and the row simply
   lapses if never run.

## Alternatives rejected

- Confirming playability by observing processes: the observation is not
  evidence. A launcher that hangs, a DRM prompt, and a running game are
  indistinguishable from the outside, and reporting `confirmed` on that
  basis would be the misrepresentation ADR 0027 exists to prevent.
- A blanket launch grant without an allowlist: "the agent may start games"
  is not a decision an owner can review. The allowlist is what makes the
  grant inspectable.
- Treating a busy session as a failure: the operation is still authorized
  and still wanted; deferring keeps it retryable in place, matching how
  every other gate refusal behaves.

## Consequences

The broker's planned verb set is complete: `install` (with update),
`verify`, and `launch`. Remaining content verbs are human-executed by
decision rather than unbuilt — uninstall (ADR 0027 §10a) and move
(ADR 0029). An owner granting launch is authorizing the agent to take over
the machine's foreground for the listed titles, which is why the list is
explicit and per-AppID.
