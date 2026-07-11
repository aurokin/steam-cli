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
directory. It installs the wheel, checks the entry point and wishlist help,
validates a fresh-profile typed-unavailable query, reopens the migrated database,
and verifies that secret-like command-line input is rejected without being
echoed.

Live provider checks are intentionally not CI tests. Account contents, prices,
provisional response shapes, credentials, and provider quotas are external and
time-varying. Milestone acceptance records coarse live results separately and
must never commit credentials or personal response bodies as fixtures.
