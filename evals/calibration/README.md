# Qualitative judge calibration

Status: **Verified 2026-08-03.**

`judge-v1-cases.json` contains 31 synthetic, route-blind cases covering the
eight fixed screen anchors. The original authored-criterion pairs are joined by
production-shaped cases for a fact-rubric hard-fail criterion, conditional
`support_if_claimed` selected evidence (correct, wrong, and omitted), and
`must_mention` selected evidence (correct and wrong). New clear pass/fail pairs
cover an invented price, unknown misreported as free, a verbose but incomplete
answer, and prose-to-same-turn-claims-sidecar alignment. Each generated
criterion keeps its production source, exact requirement, evidence path, and,
when applicable, selected evidence or candidate projection. `judge-v1-labels.json`
contains the frozen expected verdicts and binds the exact case bytes by SHA-256.
The current case-set digest is
`b4894562f9994ceb340502b3b14a6239202479828436bf10e13c92a716ab2748`.

Three fresh, context-isolated GPT-Sol xhigh judges independently reviewed the
complete frozen case set. All 93 decisions matched the frozen labels, all three
judges agreed on every case, and no decision was `uncertain`.
`judge-v1-results.json` records their exact verdicts and short rationales; its
SHA-256 digest is
`31fadc6eade63a78a8d66022c36306ce6cf5225c36e0e2dc4a15cc6bf8483f89`.
No verdict from an earlier case set is represented as current evidence. The
unchanged prompt and parser SHA-256 digests remain
`671449c1329475b3753ffe30a017ad60152603efe6def833872eff8c428deec7` and
`658a8acdf97c7d681c2b78e68c853b73fe010c49631595c7f69f67575931be49`.

`matrix-judge-settings-0.1.json` records the configured model, reasoning effort,
prompt/parser versions, three-judgment roster size, and agreement-only
adjudication policy. Its exact bytes are bound into every configured judge in
the matrix campaign. The generic `matrix-judge/0.1` prompt and settings bytes
are unchanged. The verified review establishes their clear pass/fail
discrimination for this complete frozen set.

Any completed result calibrates only clear pass/fail discrimination for its
exact case bytes. It does not establish inter-model judge agreement,
performance on subtle borderline answers, or correctness on any changed
prompt, parser, criterion, case set, label set, model, reasoning effort,
settings asset, or adjudication policy. Any such change requires a fresh,
separately identified calibration.
