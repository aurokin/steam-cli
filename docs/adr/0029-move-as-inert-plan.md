# ADR 0029: Move ships as an inert plan, not a broker composite

Status: accepted 2026-08-08 (owner decision 2026-08-08; supersedes
[ADR 0027](0027-provisioned-execution.md)'s Decision item 7 — the
move-by-reinstall composite, its `confirmed_with_residue` terminal state, and
its one-complete-copy invariant — and the "cold-file surgery for move"
deferral in its Alternatives. ADR 0027's other decisions, and
[ADR 0028](0028-trusted-manager-execution.md)'s trusted-manager model,
continue to govern.)

## Context

ADR 0027 designed move as a supervised broker composite: fresh-install the
title into the destination library, verify it, swap manifests under one
client-stopped step, and terminate `confirmed_with_residue` because the
broker cannot remove the source. That design followed from two constraints —
only Valve-authored code may touch content, and cold-file surgery was
deferred — so re-downloading was the only move the broker could perform.

It also inherited an awkward ending. Phase 0 proved steamcmd cannot uninstall
consumer titles, so the composite could never clean the source; it would
finish by handing the owner a residue-cleanup chore, having just
re-downloaded content that already existed on the same machine.

Steam itself moves an installed title between libraries in place, from the
storage management UI, with no re-download. That is an officially supported
consumer feature and the owner asked to leverage it rather than reimplement a
worse version of it.

## Decision

1. **Move is an inert plan, executed by a human in Steam.** `steam-agent
   operations plan move APPID --destination-library-ordinal N` remains the
   whole surface: preconditions, risks, destination capacity warning, and the
   storage-UI instructions. The broker never moves content and gains no move
   operation class.
2. **The composite is cancelled, not deferred**: no `dest_downloading`,
   `dest_verified`, `manifest_swap`, or `source_cleanup` sub-states, no
   `confirmed_with_residue` terminal state, and no one-complete-copy
   invariant, because no broker-side move exists to need them.
3. **Cold-file surgery is no longer on the roadmap even as a deferral.** It
   existed to make the reinstall composite cheaper for large titles; Steam's
   own move already achieves that, safely, inside the client.
4. **Uninstall's reasoning generalizes.** Where Valve ships a supported
   consumer mechanism for a destructive content operation and steamcmd does
   not, this project plans and the human executes. Install and update remain
   broker-executed because steamcmd genuinely performs them.

## Alternatives rejected

- Broker-driven move by reinstall (ADR 0027 item 7): re-acquires bytes that
  are already on the machine, cannot clean the source, and terminates in a
  state that needs human cleanup anyway — strictly worse than the supported
  in-client move on every axis that mattered.
- Cold-file surgery (staged copy, checksum, manifest switch): moves content
  behind the client's back to save a download the client already knows how to
  avoid. The risk was only ever justified by the composite's cost.
- Driving the storage UI programmatically: UI automation is the
  silent-breakage class ADR 0027 rejected for CEF, and Phase 0 separately
  showed the client owns library registration.

## Consequences

Move costs the owner a few clicks and no bandwidth. The planner surface needs
no change — move plans already carry storage-UI instructions — so this ADR
mostly deletes proposed work: Phase 2d and the cold-surgery phase leave the
roadmap. A second library remains useful for exercising move plans against a
real destination ordinal, but creating one is a human step: Phase 0 found the
client strips offline-added `libraryfolders.vdf` entries, so library
registration is UI-only.

The broker's executable surface stays exactly `install` (covering update).
Every other content verb is either planned-and-human-executed (uninstall,
move) or not yet built (repair, launch). (Superseded in part by
[ADR 0030](0030-verify-as-a-second-executable-class.md): repair shipped as the
`verify` class, and by
[ADR 0031](0031-launch-allowlist-dispatched-terminal.md): launch shipped as an
allowlisted client-plane class terminating `dispatched`. Move remains an inert
plan, which is this ADR's decision.)
