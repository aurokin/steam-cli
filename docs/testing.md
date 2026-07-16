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
and M7 common-question scenarios. Natural-language answer judging remains
opt-in; deterministic evidence, gate, ordering, and truth-state oracles are
part of CI.

Repository documentation checks also require local Markdown links to resolve,
every design document to declare its status near the top, and committed
Markdown to omit personal home-directory paths. These checks make navigation
and authority drift visible in the normal test gate.

Live provider checks are intentionally not CI tests. Account contents, prices,
provisional response shapes, credentials, and provider quotas are external and
time-varying. Milestone acceptance records coarse live results separately and
must never commit credentials or personal response bodies as fixtures.
