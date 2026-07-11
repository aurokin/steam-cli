# Steam CLI

Steam CLI is a local-first game intelligence and operations layer
for agents helping people understand their Steam library, choose what to play,
evaluate purchases, check compatibility, prepare safe client actions, and find
games for a group.

The project is not an autonomous recommendation chatbot. Its job is to collect
evidence, preserve provenance and uncertainty, apply explicit constraints, and
return stable machine-readable results that an agent can reason over.

## Status

**The M1 installed-library tracer bullet is implemented and accepted.** It
scans local Steam metadata without credentials, stores
complete observations in SQLite, and exposes deterministic installed-game
queries. Owned games, wishlists, pricing, recommendations, compatibility, and
Steam actions remain design work.

## Install and develop

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

Install the command as a local uv tool:

```text
uv tool install .
steam-agent --version
```

For development from a checkout:

```text
uv sync --dev
uv run steam-agent --help
uv run steam-agent --version
uv run python -m steam_agent --help
uv run pytest
uv build
```

## M1 usage

Steam Agent checks the standard Steam directory for the current operating
system. Override it for a nonstandard or test installation with either the
command option or environment variable:

```text
uv run steam-agent sync installed --machine local --steam-root "/path/to/Steam"
STEAM_AGENT_STEAM_ROOT="/path/to/Steam" uv run steam-agent sync installed --machine local
uv run steam-agent games query --scope installed --machine local
```

The Steam root is the directory containing `steamapps/libraryfolders.vdf`, not
a particular library's `steamapps/common` directory. Additional library roots
are discovered from that file. Steam files are read only and are never repaired
or changed.

JSON is the default output. `--format table` provides a compact human view. Put
the shared option before the command or the leaf override after it:

```text
uv run steam-agent --format table status
uv run steam-agent games query --scope installed --format table
```

Game-query tables retain completeness and warning rows, so stale or unavailable
evidence is never presented as a confirmed empty library.

Installed-game JSON omits local filesystem paths by default. Use
`--include-paths` only when the caller needs them and the output will remain
private:

```text
uv run steam-agent games query --scope installed --include-paths
```

The SQLite database does contain local library, install, and manifest paths.
Its default location is:

| Platform | Data directory |
| --- | --- |
| macOS | `~/Library/Application Support/steam-agent/` |
| Windows | `%LOCALAPPDATA%\steam-agent\` |
| Linux | `$XDG_DATA_HOME/steam-agent/`, or `~/.local/share/steam-agent/` |

The database filename is `steam-agent.sqlite3`. Override the directory with
`--data-dir PATH` or `STEAM_AGENT_DATA_DIR`; keep it private and out of the
repository.

A partial scan records its diagnostics but does not replace the last complete
installed-game projection for that machine. This is intentional: subsequent
queries continue returning the last known-good result. See the
[M1 execution plan](docs/design/m1-execution.md) and
[CLI contract](docs/design/cli-contract.md) for exact status and exit behavior.

The current working direction is:

```text
documented and provisional providers + local Steam/system observations
                              |
                              v
                    normalized evidence store
                              |
                              v
              deterministic filters and ranking dimensions
                              |
                              v
                 CLI JSON contract for calling agents
```

The implemented first vertical slice is narrower than the end vision:

> Given this computer, return its locally installed Steam games with honest
> completeness information, without requiring a Steam account credential.

That slice validates local scanning, storage promotion, path privacy, and the
agent-facing output envelope before account/provider and recommendation logic
are added.

## Design principles

- Evidence before recommendations.
- Hard eligibility is separate from subjective ranking.
- `unknown` is distinct from `false`, empty, and inaccessible.
- Owned, available, installed, and playable-now are different facts.
- Every provider is replaceable and identified by support level.
- The core remains useful offline from its cache.
- JSON is an agent contract; human output is a view over the same result.
- Secrets never appear in arguments, logs, reports, or committed files.
- SteamDB is a human reference link, never a scraped runtime dependency.

## Design documents

- [Project governance and the repo/Linear boundary](docs/project-governance.md)
- [Product questions](docs/design/product-questions.md)
- [Evidence and provider matrix](docs/design/evidence-matrix.md)
- [Provisional architecture](docs/design/architecture.md)
- [CLI and JSON contract](docs/design/cli-contract.md)
- [Historical pricing strategy](docs/design/pricing-strategy.md)
- [Actions and automation boundaries](docs/design/actions.md)
- [Existing tool evaluation](docs/design/existing-tools.md)
- [Historical research-backed validation sequence](docs/design/roadmap.md)
- [M1 execution plan and Linear work graph](docs/design/m1-execution.md)
- [Decision register](docs/adr/README.md)
- [Original research handoff](steam-library-agent-research-handoff.md)

The handoff is retained as source material, not as an accepted specification.
The design documents call out places where current research corrected it.

## Deliberately unresolved

- Long-term storage schema and migration policy beyond the implemented M1 tables.
- Whether wishlist access is reliable enough for the first supported release.
- Which source may legally and reliably provide historical prices.
- Which Steam actions may be executed rather than planned/opened for a human.
- How much of the Steam catalog should be enriched locally.
- Whether high-trust SteamKit/SteamCMD access is worth supporting.
- Whether an MCP wrapper is useful after the CLI contract stabilizes.

See the decision register before turning any of these into an ADR.
