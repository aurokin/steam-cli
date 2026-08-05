# Agent Instructions

Steam CLI is a local-first, evidence-preserving CLI for Steam library questions.
M1–M7 are accepted. Preserve their schema `0.1` contracts and safety boundaries.

## Non-negotiable boundaries

- M7 is read-only: observe, rank, and emit human-open plans. Never launch,
  install, uninstall, mutate Steam state, or execute a generated plan.
- Preserve the M1 last-good rule: partial or failed scans do not replace a
  complete installed projection.
- Keep `unknown`, `false`, empty, inaccessible, and stale distinct. Separate
  hard eligibility from subjective ranking.
- Keep queries cache-only where their contract says so. Attach provider,
  retrieval time, context, and support level to normalized evidence.
- Omit private filesystem paths and account identifiers from normal output.
  Keep personal paths, identifiers, secrets, and raw responses out of fixtures,
  logs, reports, and committed files.
- Keep JSON deterministic and diagnostics on stderr. Preserve the distinction
  between `return_url`, `open_for_human`, `agent_read`, and `automated_ingest`.
- Do not treat research or proposed design as accepted. Record consequential
  decisions and evidence in [docs/adr/README.md](docs/adr/README.md).

## Test changes

```text
uv run ruff check .
uv run pytest -q
```

Use a narrow test such as `uv run pytest -q tests/test_storage.py` while
iterating, then run the complete gate in [docs/testing.md](docs/testing.md).
Add new SQLite migrations; do not rewrite an existing migration.

## Find the right documentation

| Need | Canonical source |
| --- | --- |
| Product status and first use | [README.md](README.md) |
| User workflows and privacy | [docs/user-guide.md](docs/user-guide.md) |
| Development workflow | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Documentation map and authority | [docs/README.md](docs/README.md) |
| Current design map | [docs/design/README.md](docs/design/README.md) |
| Command and JSON behavior | [docs/design/cli-contract.md](docs/design/cli-contract.md) |
| Testing and acceptance | [docs/testing.md](docs/testing.md) |
| Model evaluation corpus and runner | [evals/README.md](evals/README.md) |
| Decisions and ADR threshold | [docs/adr/README.md](docs/adr/README.md) |

When documentation changes, update one canonical page and link to it. Label
working, proposed, deferred, historical, and accepted material explicitly.
