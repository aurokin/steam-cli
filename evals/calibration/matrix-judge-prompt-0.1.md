# Matrix qualitative judge prompt 0.1

For each blinded case, apply only the stated criterion requirement to the
candidate answer. Return exactly one verdict: `pass`, `fail`, or `uncertain`,
plus a rationale of at most 12 words.

Treat an omission as `fail` when the requirement explicitly demands that
behavior. Do not infer route or model identity. Do not use deterministic layer
outcomes, tool traces, reasoning, protocol data, or information outside the
privacy-cleared candidate and synthetic criterion.
