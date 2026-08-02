# Qualitative judge calibration

Status: **Verified 2026-08-02.**

`judge-v1-cases.json` contains 23 synthetic, route-blind cases covering the
eight fixed screen anchors. The original authored-criterion pairs are joined by
production-shaped cases for a fact-rubric hard-fail criterion, conditional
`support_if_claimed` selected evidence (correct, wrong, and omitted), and
`must_mention` selected evidence (correct and wrong). Each generated criterion
keeps its production source, exact requirement, evidence path, and selected
evidence. `judge-v1-labels.json` contains the frozen expected verdicts and binds
the case bytes by SHA-256. The current case-set digest is
`5254277a226655ab841bb42bb7ce05f561a6e9ba3ef22806906d7426909220fa`.

The expanded case set was reviewed independently by three fresh,
context-isolated GPT-Sol xhigh judges. All 69 decisions matched the frozen
labels, all three judges agreed on every case, and no decision was `uncertain`.
`judge-v1-results.json` records the exact verdicts and short rationales; its
SHA-256 digest is
`0e1ceef76cce638758029f64f374beec6806d5a934307b8c78fe8ee7a09013e2`.
The previously verified 16-case result does not calibrate the newly enforced
criterion sources or selected-evidence semantics and is not represented as
current evidence.

`matrix-judge-settings-0.1.json` records the configured model, reasoning effort,
prompt/parser versions, three-judgment roster size, and agreement-only
adjudication policy. Its exact bytes are bound into every configured judge in
the matrix campaign. The generic `matrix-judge/0.1` prompt and settings bytes
are unchanged. The verified review establishes that they correctly classify
this complete frozen set of production-shaped synthetic criteria.

Any completed result calibrates only clear pass/fail discrimination for its
exact case bytes. It does not establish inter-model judge agreement,
performance on subtle borderline answers, or correctness on any changed
prompt, parser, criterion, case set, label set, model, reasoning effort,
settings asset, or adjudication policy. Any such change requires a fresh,
separately identified calibration.
