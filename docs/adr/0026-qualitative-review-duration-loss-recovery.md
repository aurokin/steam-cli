# ADR 0026: qualitative review duration-loss recovery

Status: accepted 2026-08-05

## Context

A valid qualitative-review invocation can produce exact verdict and event-log
bytes but lose its externally measured duration before the canonical operation
is persisted. Treating that invocation as a transport failure and calling the
judge again would reroll a valid outcome. Reconstructing a duration from file
timestamps, logs, or elapsed host time would fabricate evidence.

ADR 0025 cannot represent this case. Its interrupted-attempt class authorizes
attempt 2 for the campaign incident it accepts; it does not bind surviving
verdict and event bytes from that consumed attempt. Here the observed result
must remain attempt 1, and no durable event or attempt ledger can establish or
authorize retries. A separate amendment artifact would duplicate the
operation's bindings without adding evidence.

## Decision

### Publish one narrowly typed recovery operation

The runner exposes a dedicated duration-loss recovery command. On its first
successful use in a matrix, it validates the exact private verdict and event
files against the prepared case, configured judge, review package and matrix;
validates the tool-free event lifecycle, verdict schema, target hashes,
canary, privacy boundary, and isolation attestation; publishes one canonical
`steam-agent-eval-review-duration-loss-operation/0.1` operation; and imports
the embedded judgment. That operation is the sole durable recovery artifact.
There is no duration-loss amendment file or correction journal.

The operation binds the matrix and review-package identities, case and judge
slot, canary, isolation attestation, exact raw and normalized verdict digests,
event-log digest and count, and the embedded judgment. Its duration is
`{"state":"unavailable","reason":"attempt_duration_lost_before_persist"}`
and is non-authoritative. Agreement resolution and benchmark scoring use the
recovered verdict independently of timing.

`attempt_count` must be exactly 1. The preserved invocation bytes establish
one valid attempt, while the absence of a durable event or attempt ledger
cannot establish or authorize retries. Initial recovery publication is
eligible only when the target slot has no canonical operation and no judgment
and the matrix contains no other duration-loss recovery operation. Changed,
missing, malformed, or non-private source bytes, a different slot, an occupied
slot, or a second duration-loss recovery fails closed. The command does not
authorize a model call or consume another attempt.

Ordinary judgment assembly cannot create this operation schema or accept a
lost duration under this decision. The dedicated recovery command is the only
creation path. If it crashes after operation publication but before judgment
import, the same command validates the existing operation and resumes the
missing import from its embedded artifact. It never replaces the operation,
reruns the judge, estimates timing, or requires the disposable verdict and
event files after operation-first publication.

ADR 0025 remains unchanged. Its single
`review-measurement-amendment.json`, classes, attempt rules, and
`steam-agent-eval-review-unavailable-duration-operation/0.1` schema continue
to apply only to the two measurement cases that ADR accepts.

### Preserve the existing writer boundary

The recovery reuses ADR 0023's locks, canonical operation pathname,
append-only publication, exact-artifact validation, semantic judgment
uniqueness, and operation-first resume. Those invariants govern writes made
through the runner API. As in ADR 0023, a privileged actor that can coherently
rewrite private matrix and review-package files outside the runner is outside
the append-only runner-API threat model; this recovery neither claims to
detect nor protect against such filesystem rewriting.

## Consequences

One observed verdict can survive a host-side duration-persistence failure
without a reroll, an invented measurement, or a second durable exception
artifact. The distinct schema keeps this operation from being confused with
ordinary assembly or ADR 0025 recovery, and the matrix-wide cap keeps the
exception deliberately finite.

Operators must retain the private verdict and event files until the recovery
operation has been published and verified. If either file is unavailable or
fails normal validation before publication, this ADR provides no recovery
path. A different failure class, a second affected slot, or evidence of more
than one attempt requires a separate decision.
