# Testing and acceptance

The deterministic acceptance spine makes no provider requests. It runs on
Python 3.12 and 3.13 in GitHub Actions and checks the locked development
environment, Ruff, pytest, source/wheel builds, and the installed command rather
than only importing the checkout.

Run the same gate locally:

```text
uv sync --dev --locked
uv run ruff check .
uv run pytest -q
uv build
uv run python scripts/package_smoke.py
```

The package smoke creates an isolated virtual environment and temporary data
directory. It installs the wheel against the locked runtime dependency export,
checks the entry point and implemented milestone help, validates fresh-profile
typed-unavailable queries including M7 observation, verifies the complete
source migration set and database reopen, and checks that secret-like
command-line input is rejected without being echoed.

The normal suite executes every deterministic assertion in the active M4, M5,
and M7 common-question scenarios. It also checks the schema `0.3` corpus
invariants and the matrix plan, resume, inspection, comparison, judgment, and
adjudication artifact contracts without making provider requests.
Natural-language answer judging remains opt-in; deterministic evidence, gate,
ordering, and truth-state oracles are part of CI.
The opt-in qualitative workflow prepares exact route-blind model inputs and a
structured response schema but never invokes a judge. Normal tests cover
private package bounds, stale-input rejection, existing judgment-envelope
validation, the initial-plus-two structural retry cap, deterministic agreement
resolution, and the Codex structured-output subset. The latter recursively
requires explicit types for `const` and `enum` properties, strict required-only
object shapes, supported keywords, and bounded values. Supersession tests prove
that a terminal package's original assets and attempts remain immutable, its
matrix registry and append-only tombstone authorize exactly one incident-bound
destination under locks, every judge slot moves together, and any copied root,
observed output, operation,
import, partial replacement, concurrent mutation, or repeated supersession
fails closed. Malformed identifiers, optional symlinks, protocol-identity
changes, and attestation file swaps also have focused regressions.

The suite also validates the repository skill's structure and the diagnostic
skill-track boundary: sealed source binding, exact App Server inventory,
explicit native skill input with unchanged user text, private workspace copy,
exclusive benchmark configuration, and report identity. Live model execution
remains opt-in.

Repository documentation checks also require local Markdown links to resolve,
every design document to declare its status near the top, and committed
Markdown to omit personal home-directory paths. These checks make navigation
and authority drift visible in the normal test gate.

Live provider checks are intentionally not CI tests. Account contents, prices,
provisional response shapes, credentials, and provider quotas are external and
time-varying. Milestone acceptance records coarse live results separately and
must never commit credentials or personal response bodies as fixtures.
Before qualitative judge slots run for a new response schema, Codex version,
model route, or isolation profile, one opt-in non-slot canary must exercise the
exact package schema bytes and canonical host-isolated profile. It uses no
candidate case, must complete with exactly one tool-free schema-valid agent
message, and retains only a private bounded attestation. The operator separately
attests to the external Codex, model, effort, and isolation identities that the
runner cannot observe. A pass or bounded failure is terminal for that package;
a failure stops it without consuming a judge attempt. This provider check
complements rather than replaces the deterministic subset validator.
