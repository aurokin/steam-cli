# Steam Agent evaluation corpus

This directory contains synthetic, versioned common-question scenarios. It is
a contract corpus, not captured user data and not a live-provider benchmark.

- `schema/scenario-0.1.json` and `schema/scenario-0.2.json` define the scenario
  format; each scenario names the version it validates against.
- `scenarios/m2/` covers the identity, identifier opt-in, and data-deletion
  boundaries, including the credential refusal probe.
- `scenarios/m3/` covers accepted deal-question behavior.
- `scenarios/m4/` contains active deterministic recommendation questions for
  the accepted `recommendations/0.1` command and recipe contracts.
- `scenarios/m5/` covers accepted target-specific compatibility boundaries.
- `scenarios/m6/` covers bounded discovery and three-valued group ownership,
  copy certainty, mode, and player-count evidence.
- `scenarios/m7/` covers local-operation truth, storage ranking, and inert-plan
  boundaries without filesystem, provider, browser, or client access.
- `runner/` is the opt-in agent-execution runner: it materializes fixtures
  into a real `--data-dir` cache, drives one turn through the Codex App
  Server protocol, and grades the transcript deterministically. Run it with
  `uv run python -m evals.runner --family m7` (requires a local `codex`
  binary). Normal CI covers only its materializer and grader.
- `results/` is reserved for generated traces, answers, and judge reports and
  is ignored by Git.

Each scenario keeps four concerns distinct: expected deterministic behavior, a
tool-use policy, a fact rubric, and an opt-in qualitative answer rubric. Normal
CI schema- and privacy-validates every scenario without network access or a
model API. Executable deterministic CLI coverage spans every family: oracle
modules for M3, M4, M5, and M7, and the materializer round trip for M2 and
M6 contract scenarios. See the
[evaluation strategy](../docs/design/evaluation-strategy.md) for scoring,
privacy, volatility, and future judge rules.
