# ADR 0025: qualitative review measurement amendments

Status: accepted 2026-08-05

## Context

Two completed qualitative-review campaigns exposed narrow measurement failures
that do not invalidate their subject evidence. In a discovery slot, an external
invocation was interrupted after producing a valid verdict but before the
private operation recorded its duration. In a skill slot, a valid operation and
judgment were retained, but the recorded duration is known to be unreliable.
Discarding either verdict would turn a timing failure into an outcome reroll;
inventing a duration would make the measurement false.

The existing review package, fixed judge roster, initial-plus-two attempt limit,
canonical operation path, append-only publisher, and scoring contract already
provide the required boundaries. A general correction journal, new staging
area, dynamic filenames, or replacement publisher would add recovery behavior
that these two cases do not need.

## Decision

Each matrix may contain at most one private, mode-`0600`, append-only
`review-measurement-amendment.json`. Its fixed schema and filename bind the
matrix manifest, matrix-local review-package registry and destination, external
attempt-ledger digest, case and judge slot, canary attestation, authorized and
affected attempts, and any retained operation and judgment artifact. The runner
loads and fully validates the canonical amendment once while holding the matrix
and review locks, then passes that same parsed document and digest through
validation, assembly, and agreement resolution. It never rereads unvalidated
raw amendment bytes later in the operation.

Exactly two amendment classes are accepted:

- `interrupted_attempt_duration_unavailable` applies only when attempt 1 has no
  operation or judgment. Attempt 1 remains consumed. The amendment authorizes
  only attempt 2, whose explicit unavailable-duration operation is published at
  the existing canonical operation pathname. It does not create attempt 3.
- `recorded_duration_unreliable` applies only to a skill-track slot with one
  valid retained operation and judgment. Both artifacts remain byte-for-byte
  immutable. The amendment binds them and permits only a same-attempt resume;
  it cannot select a new attempt or verdict.

An amended duration is represented as unavailable and is non-authoritative.
Agreement resolution and benchmark scoring continue to use the retained
verdicts independently of timing. A normal package still requires an integer
duration. A second amendment, a different slot or class, a changed ledger,
package, case, canary, operation, or artifact, or an unauthorized attempt fails
closed.

## Consequences

The two observed timing failures can be represented truthfully without
rewriting evidence, increasing retry budgets, or rerunning subject evals. The
fixed artifact makes the exception inspectable and content-bound while reusing
the existing atomic exclusive publisher and canonical operation layout.

This is deliberately not a generalized repair mechanism. A different failure
class, multiple affected slots, changed verdict evidence, or a need to replace
an operation requires a separate decision. Future review protocols may remove
this compatibility path after the affected matrices no longer need validation.
