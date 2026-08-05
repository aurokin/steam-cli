# Contributing

This guide is the entry point for changing Steam CLI. Read the
[documentation map](docs/README.md) before changing a contract or policy, and
use the [user guide](docs/user-guide.md) to understand the experience exposed
by the CLI.

## Set up a checkout

Package metadata accepts Python 3.12 or newer; CI currently tests 3.12 and
3.13. Install [uv](https://docs.astral.sh/uv/), then create the locked
development environment:

```text
uv sync --dev --locked
uv run steam-agent --help
```

Use `uv run steam-agent ...` when testing the checkout. Installed users invoke
the same command as `steam-agent ...`.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/steam_agent/cli.py` | Command parsing, output, and exit behavior |
| `src/steam_agent/contracts.py` | Shared result envelopes and contract values |
| `src/steam_agent/storage.py` | SQLite connection, migrations, and projections |
| `src/steam_agent/migrations/` | Forward-only schema changes |
| `src/steam_agent/*query*.py` | Cache-only query composition |
| `src/steam_agent/steam_*.py` and provider modules | Bounded acquisition adapters |
| `tests/` | Deterministic unit, CLI, storage, and acceptance tests |
| [`evals/`](evals/README.md) | Common-question scenarios, deterministic oracles, and opt-in model runs |
| `docs/design/` | Current design and historical milestone records |
| `docs/adr/` | Accepted decisions and decision register |

## Change workflow

1. Identify the canonical contract, policy, or ADR from
   [docs/design/README.md](docs/design/README.md).
2. Write or update the narrow deterministic test that demonstrates the change.
3. Implement without weakening provenance, privacy, truth-state, or read-only
   boundaries.
4. Run the focused test and lint the changed path.
5. Run the complete acceptance gate before handing off the change.
6. Update the one canonical document affected by the behavior. Link to it from
   entry points instead of copying its rules.

Example focused checks:

```text
uv run pytest -q tests/test_operations_observe.py
uv run ruff check src/steam_agent/operations_observe.py tests/test_operations_observe.py
```

Run the exact complete gate in [docs/testing.md](docs/testing.md). That page is
canonical for the gate, what it proves, and why live provider checks are
separate.

## Data and contract rules

- Add a numbered migration for schema changes. Never edit an existing
  migration that another database may already have applied.
- Preserve complete last-good projections when a scan or provider attempt is
  partial, failed, abandoned, or still running.
- Keep query commands deterministic and cache-only when their CLI contract says
  so. Send diagnostics to stderr and structured results to stdout.
- Treat account IDs, personal paths, credentials, and raw provider bodies as
  private. Use synthetic identifiers, paths, provider payloads, and fixed
  clocks in tests and examples.
- Represent absent, unknown, false, inaccessible, stale, and empty states
  separately. Do not infer a hard eligibility result from ranking evidence.
- M7 remains read-only with respect to Steam. A plan may return an official URL
  or human instruction; it may not open or execute it.

Exact machine behavior belongs in the
[CLI contract](docs/design/cli-contract.md). Provider support and limitations
belong in the [evidence matrix](docs/design/evidence-matrix.md).

## Documentation and decisions

Every maintained document has one role and one canonical home. Keep entry
points short, link downward for detail, and label working or proposed content.
Do not rewrite accepted milestone records to describe later behavior.

Record a consequential choice in an ADR when it changes a durable schema,
external contract, privacy or retention rule, provider boundary, action class,
or an expensive-to-reverse architecture decision. The
[ADR index](docs/adr/README.md) defines the threshold and records open choices.

`steam-library-agent-research-handoff.md` and the research roadmap are source
material only. They cannot override accepted ADRs, the CLI contract, or tested
behavior.
