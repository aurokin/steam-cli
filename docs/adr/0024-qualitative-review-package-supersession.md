# ADR 0024: qualitative review package supersession

Status: accepted 2026-08-04

## Context

The first external invocations of the qualitative-review packages prepared by
ADR 0023 were rejected by the provider before inference. The response schema
was valid Draft 2020-12 JSON Schema, but the root `schema` property used a
string `const` without an explicit `type`. Codex's structured-output API
rejected that shape before producing an agent message. A live isolated canary
confirmed that adding `type: string` to that property removes the observed
provider rejection; the provider accepted the existing typeless verdict enum.
Local package validation and the native-Codex preflight did not exercise that
provider boundary.

The failed requests still count against the initial-plus-two limit of their
original package. Editing that package, erasing the requests, or retrying only
the affected slots would make the review history unverifiable. Re-running the
completed subject matrices would also be misleading: their inputs, outputs,
reports, projections, prompts, parsers, rubrics, and route settings were not
involved in the failure.

## Decision

### Supersession replaces a defective instrument, not a verdict

A legacy `0.1` private review package may be superseded by package protocol
`0.2` only when a package-wide defect caused every attempted request to fail
before inference and none of the package's requests produced a valid verdict,
judgment operation, imported judgment, adjudication, or other observed review
outcome. Supersession is forbidden after any valid model output, including an
`uncertain` verdict, and after any disagreement, post-inference timeout or
failure, operation publication, or artifact import. It cannot be used to seek a
different score or to extend an exhausted retry budget.

The original package becomes terminal. Its existing assets remain byte-for-byte
unchanged, and one append-only private supersession tombstone records that
terminal state. Its requests remain attempts under its own initial-plus-two
limit. A replacement package has a distinct content-bound identity, schema and
case digests, and package-local initial-plus-two limit.
That new limit does not erase or reclassify the old requests; it applies to a
new version of the external review instrument. Every configured judge slot in
the package moves to the replacement together. A resolver must never combine
cases, operations, judgments, or attempt accounting from superseded and
replacement packages.

This decision accepts only the `0.1` to `0.2` transition; superseding another
protocol version requires a new accepted contract. The corrected response
contract is version `0.2`. It retains the verdict semantics of `0.1`, adds the
provider-required root string type, and adopts a stricter local rule that every
string `const` and `enum` is explicitly typed.
The case and package-ledger contracts also advance to `0.2`, and invocation
operations advance to `0.3`; the retained
`steam-agent-eval-judgment/0.1` and adjudication contracts do not change. The
old `0.1` assets remain available only for validating and auditing the terminal
package. They are never rewritten in place.

### The transition is append-only and content-bound

The old package remains private and independently inspectable. Preparation
locks the old package and matrix, rechecks eligibility, and reserves one private
append-only `review-package.json` registry in the matrix before publishing the
supersession tombstone and replacement. The registry binds the manifest,
package, canonical destination hash, and supersession tombstone without storing
a private path. A crash may resume only that exact destination, package, and
incident; a copied root, different destination, or second replacement fails
closed. A separate private append-only
incident record binds the old package or ledger identity, matrix manifest,
response-schema digest, source revision, Codex version, and isolation-profile
identity. It records the known per-slot request counts and durations, a bounded
sanitized provider error, and explicit states for pre-inference rejection,
model output, operation publication, and artifact import. Raw response bodies,
reasoning, event text, credentials, account identifiers, thread identifiers,
and private paths are not retained in that record.

The registry is operational state, not subject evidence or a qualitative
outcome; matrix inspection and reporting tolerate it but never score it. The
replacement package identity and ledger bind the old ledger, old response
schema, complete incident record, operation and canary protocol versions, Codex
version, model, effort, and isolation identities by SHA-256. The tombstone
binds that package identity, the incident, and a hash of the one authorized
destination without retaining its private path. Creation fails
closed if the old package changed, the incident claims do not match its retained
state, the replacement reuses the old protocol or schema identity, any old
operation or imported artifact exists, only part of the package is selected,
or the old package was already superseded. Public reporting may disclose bounded request
totals, the sanitized failure class, the absence of inference and imports, and
the old and new package identities. It must not expose private operational
material.

### Static validation and one live canary precede judge slots

Package preparation validates the response schema against both Draft 2020-12
and the pinned Codex structured-output subset before publishing any case. The
subset check recursively requires explicit types for properties constrained by
`const` or `enum`, strict objects with `additionalProperties: false` and every
property required, supported keywords and types, and the existing document,
array, and string bounds. A schema that cannot pass this deterministic check
cannot become a package asset.

Before any slot in a package is invoked, the operator runs one package-specific
non-slot live canary with the exact copied native executable, response-schema
bytes, model and effort, and host-isolated invocation profile that the judges
will use. The canary uses no subject/candidate evidence or projection, only
fixed synthetic transport scaffolding. It must complete successfully with exactly one
tool-free agent message whose JSON validates against the response schema. Its
private attestation retains only bounded identities, digests, counts, timing,
and terminal pass or failure state. The operator explicitly attests that the
external invocation used native Codex 0.146, GPT-5.6-Sol at xhigh effort, and
the exact isolation profile; the runner validates the returned artifacts but
cannot independently observe that external process. Transport, provider, or
structural failure is recorded once and permanently aborts the package before
a judge slot without consuming a slot attempt. A canary is never reused across
packages. A successful canary proves provider acceptance of that exact boundary,
not the correctness of later verdicts.

### Subject evidence is retained

Supersession reuses the completed matrix and its exact qualitative projections.
It does not rerun subject evals because neither the subject input nor any
candidate output changed. Adding explicit string types is semantically
equivalent to the string-valued `const` and `enum` constraints in the rejected
schema. All qualitative judge slots run afresh under the replacement package;
agreement resolution and the benchmark report run only after that one package
has a complete judgment roster.

## Consequences

The recovery preserves the expensive subject observations while making the
failed external requests visible and non-retryable in their original package.
It adds a narrow escape hatch for a demonstrably invalid review instrument,
not a general reroll mechanism. Provider compatibility now has both a
deterministic guard and a live, source-free check before candidate judging.

Package supersession adds private provenance and validation work. If any
inference or review outcome had been observed, this decision would not permit
recovery; a separately accepted evaluation design would be required.
