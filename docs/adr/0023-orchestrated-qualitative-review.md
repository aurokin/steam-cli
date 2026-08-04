# ADR 0023: orchestrated qualitative review packages

Status: accepted 2026-08-04

## Context

Benchmark campaigns retain privacy-cleared qualitative projections and accept
strict imported judgments, but the repository previously exposed no safe path
from a completed benchmark to those imports. Hand-building target hashes,
judge envelopes, and agreement artifacts across repeated observations is both
error-prone and difficult to audit. Putting model invocation inside the runner
would also widen the runner's provider, credential, retry, and prompt boundary.

## Decision

The runner prepares an immutable private review package outside the matrix
directory. Each case is the exact machine-readable input supplied verbatim to
an external judge invocation: it combines the hash-bound calibrated prompt,
parser contract, blinded projection, presentation metadata, and target hashes.
The package includes a separate JSON response schema for structured output.
Neither artifact contains the candidate route, deterministic outcomes,
commands, traces, or protocol data.

Model invocation remains external to the repository runner. The configured
judge identity, model, and effort are selected by the orchestrator, while every
invocation receives only one prepared case. No ad hoc wrapper prompt is
permitted. Each call uses a fresh empty `HOME` and an isolated `CODEX_HOME`
containing only copied authentication. The pinned Codex 0.146.0 invocation
disables user configuration, rules, hooks, apps, plugins, MCP servers, and web
search; minimizes the inherited shell environment; and selects the same named
permission boundary as the generator without its Python or source read roots.
That boundary denies host root and temporary paths, allows only minimal
platform files plus the empty workspace, and disables network access. The
package ledger binds the matrix manifest, every case digest, the
response-schema digest, and an explicit policy that usage accounting is
unavailable. Import requires the operator to attest to that exact versioned
isolation profile because the runner cannot observe an external process.

The assembler accepts one exact-rubric verdict response whose work-item and
projection hashes must match the prepared case, creates the existing
`steam-agent-eval-judgment/0.1` envelope, and imports it through the existing
privacy and policy validator. Its append-only private operation records the
case and artifact digests, externally measured duration, unavailable usage
state, and an attempt count of one through three. Three means the initial call
plus at most two retries, and retries are permitted only for transport failure
or structurally invalid output. A valid `uncertain` verdict or disagreement is
never retryable.

After one configured judgment per judge identity exists for every case, the
resolver derives agreement outcomes without a model call. Unanimous `pass` or
`fail` is retained; any disagreement or `uncertain` verdict becomes
`unresolved`. It imports the existing adjudication schema and records another
append-only operation. The review lock is always acquired before the matrix
lock, and the matrix conflict check, operation publication, and target import
occur under both. An interrupted import resumes from the bound operation
before consulting any disposable response file, without regenerating model
output.

Review packages, cases, operations, and schemas are private and bounded. They
are operational material rather than benchmark acceptance evidence; the
matrix's judgment and adjudication directories remain the authoritative
qualitative artifacts.

## Consequences

Qualitative review becomes repeatable without adding network or model calls to
the runner. Exact prompt, projection, parser, response schema, judge settings,
and operational retry provenance are inspectable independently. The workflow
cannot claim token or monetary cost because the external subscription does not
expose trustworthy usage; it records that absence instead of inventing a
number.

The workflow deliberately does not resolve genuine judge disagreement. A
future human-adjudication policy would require separate calibration and a new
accepted contract rather than retries against the same model policy.
