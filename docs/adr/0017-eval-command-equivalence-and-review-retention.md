# ADR 0017: bounded eval command alternatives and review-answer retention

Status: accepted 2026-07-31

## Context

The live agent runner previously required one exact semantic argument vector
for each scenario's required command. That is the safest default, but M4's
`--explain` flag is a bounded, scenario-equivalent form: it records the
request in response context without changing the recommendation facts asserted
by the scenario. Treating every extra flag as equivalent would weaken the
command oracle and could let future CLI behavior silently broaden accepted
evidence.

Failed tool, oracle, or claim grading also made qualitative calibration harder.
The runner hashed the entire answer whenever any deterministic layer failed,
even when every agent turn completed and the complete answer surface passed
the privacy gate. Retaining the whole failed trace would expose prompts,
commands, output, evidence documents, claims, or App Server metadata. Privacy
pattern checks also cannot recognize arbitrary data obtained through an
unlisted command, so answer retention cannot rely on privacy grading alone.

## Decision

Scenario schema `0.2` may declare `accepted_optional_options` on a required
command. It is an additive, bounded list of at most 16 exact objects with a
unique ASCII long-option name. Each object is either `{name}` for a valueless
flag or `{name, value}` for one exact nonempty value of at most 256 characters.
`--format`, duplicate names, names already present in required arguments, and
option-like values are invalid declarations. All undeclared semantic options,
values, duplicates, and positionals remain exact failures; the existing
transport normalization still permits one undeclared `--format json`. This
declares scenario-oracle equivalence only; it is not a product-wide assertion
that two CLI documents are byte-identical or permanently semantically
interchangeable.

Reports add `qualitative_review_answers`. It is a deterministic ordered list of
nonempty `{turn, text}` objects only when:

- every agent turn completed;
- the full answer and retained command surfaces passed privacy grading; and
- tool policy passed, or failed solely because required evidence was missing
  or unusable.

Any unlisted command, execution or activity violation, cache/network/mutation
violation, unexpected data directory, non-JSON command, or other tool-policy
failure makes the field null. This prevents arbitrary output obtained through
an unsafe tool from being laundered into retained prose.

The projection contains only sanitized user-visible agent prose. It never
copies prompts, commands, outputs, protocol events, CLI documents, claims, or
model metadata. A terminal JSON block is removed only when it validates as the
accepted claims/decline sidecar; malformed or unrecognized JSON remains visible
for review. Per-turn and aggregate byte budgets reuse the App Server input
limits. The field is untrusted qualitative-review material, never oracle or
claim evidence.

Existing full-trace retention is unchanged. Unless every deterministic safety
gate passes, prompts, turns, commands, outputs, documents, claims failures, and
metadata remain structural records with hashes and lengths.

## Consequences

M4 calibration can inspect a privacy-cleared answer when the only failure is
exact required-evidence capture, without accepting arbitrary command variants
or persisting the failed trace. Consumers must keep the qualitative field
visually and logically separate from accepted evidence because its facts may
be wrong and its scenario may have failed. A malformed sidecar can no longer
disappear from the visible review text.

The schema addition is opt-in and existing scenarios retain exact matching.
The report addition applies across the corpus but is null at unsafe boundaries.
Reversal is straightforward: remove declarations and the isolated report
projection; hashes and deterministic grading remain sufficient to reproduce
the prior retention behavior.
