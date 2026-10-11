# ADR 0033: explicit agent queries, ephemeral acquisition and recovery results

Status: **Accepted, 2026-10-11.** Implements the owner's core-agent delivery
direction without changing the planner/executor authority boundary.

## Decision

Keep the two host-local entry points and accepted schema `0.1` responses.
Add bounded exact AppID filtering to cached games queries; filtering changes
items, never the scope snapshot's completeness or provenance. Wishlist names
are opt-in, batch-resolved from the selected account/machine cache, with their
own evidence. Missing names remain null and no enrichment is acquired.

Steam owned, wishlist, activity and achievement refreshes accept an explicit
`stored | process-env` credential source. Stored remains the default. The
environment source reads only `STEAM_WEB_API_KEY` at authorized acquisition,
never falls back, creates no credential reference, and persists no key. Native
children exclude that variable. Provider acquisition remains explicit.

Broker `--format json` selects `broker-result/0.1`: durable receipt, invocation
attempt, retry advice and nested legacy result are separate. Default successful
wire output is preserved, including desktop dispatch. Retry advice never
authorizes automatic replay. `inspect --request-id` is read-only and can neither
insert nor expire an operation. Advisory preflight creates no intent or nonce
and reports unsupported, blocked or unknown prerequisites honestly; it does
not reserve a lease. The expected-machine alias guard prevents routing mistakes,
including preflight, but is not host attestation.

Lost desktop responses expose an opaque job handle. Discovery/collection reopens
the original owned task and validates source, canonical SID, token mode, exact
interpreter/action/working directory and idle state before cleanup. Queued,
running, foreign and unknown tasks are retained. Collection never submits work.

Readers refuse unsupported ledger schema, state, executable operation or future
database version before writable journal setup or index migration. Every
mutation checks again inside its transaction, and read projections preserve
unsupported state as unavailable. Existing older terminal-index definitions can
still upgrade under the accepted contract; no ledger snapshot is rolled back.

## Evidence and alternatives

Focused cache/filter/name tests preserve account/machine isolation, last-good
and unknown states. Injected HTTP canaries cover successful and failed refreshes
for all four routes, checking stdout/stderr, files, SQLite and live WAL. Actual
subprocess tests cover broker kill points, exact request binding, competing
callers, shared execution locks and surviving writers. Installed-wheel smoke
exercises original-request lookup, terminal reuse and uncertain dispatch recovery.

Implicit environment fallback would make credential selection unpredictable.
Changing all default result shapes would break existing agents. Retrying under
a new ID after uncertainty would risk a second effect. These alternatives are
rejected; additive selection and original-request recovery provide a migration.

## Consequences and reversal

Direct request IDs retain their accepted permanent binding; legacy intake's
expiry semantics remain distinct. New results and filters are opt-in. Readers
can continue using existing commands, and callers can adopt the richer result
one workflow at a time. Native desktop, game and content qualification remain
separate from deterministic contract tests; current evidence is in the
[host record](../design/agent-host-support.md).
