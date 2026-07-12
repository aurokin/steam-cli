# Steam Agent evaluation corpus

This directory contains synthetic, versioned common-question scenarios. It is
a contract corpus, not captured user data and not a live-provider benchmark.

- `schema/scenario-0.1.json` defines the scenario format.
- `scenarios/m3/` covers accepted deal-question behavior.
- `scenarios/m4/` proposes recommendation questions for promotion as M4
  command and recipe contracts are accepted.
- `results/` is reserved for generated traces, answers, and judge reports and
  is ignored by Git.

Each scenario keeps four concerns distinct: a deterministic CLI oracle, a
tool-use policy, a fact rubric, and an opt-in qualitative answer rubric. Normal
CI validates the corpus without network access or a model API. See the
[evaluation strategy](../docs/design/evaluation-strategy.md) for scoring,
privacy, volatility, and future judge rules.
