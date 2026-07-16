# Steam CLI

Steam CLI provides `steam-agent`, a local-first command-line interface for
agents and people who want truthful answers about a Steam library. It collects
bounded evidence, preserves provenance and uncertainty, and returns stable JSON
for questions about ownership, deals, recommendations, compatibility, groups,
and local storage.

It is not an autonomous Steam client. It never launches, installs, moves, or
uninstalls games. Operation plans are instructions for a person to carry out in
Steam.

## Project status

M1 through M7 are implemented and accepted. The current CLI can:

| Area | Current capability |
| --- | --- |
| Installed library | Read local Steam metadata and preserve the last complete scan |
| Account library | Synchronize visible-owned games with truthful capability and freshness |
| Wishlist and deals | Compare attributed cached price evidence in an explicit country/store context |
| What to play | Apply deterministic recipes to cached evidence and explicit preferences |
| Compatibility | Assess declared requirements for one machine or Steam Deck without performance promises |
| Discovery and groups | Rank a bounded known-game universe and preserve unknown ownership or missing copies |
| Local operations | Observe and rank storage evidence, then generate inert human-executed plans |

Provider coverage is deliberately conservative. Missing, inaccessible, stale,
and false are different results; unsupported evidence remains unknown.

## Quick start

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

Install from a checkout:

```text
uv tool install .
steam-agent --version
steam-agent status
```

Read the installed library without credentials:

```text
steam-agent sync installed --machine local
steam-agent games query --scope installed --machine local --format table
```

Steam files are read only. The sync writes normalized observations only to the
tool's private SQLite database. For a nonstandard Steam installation, pass the
directory containing `steamapps/libraryfolders.vdf`:

```text
steam-agent sync installed --machine local --steam-root "/path/to/Steam"
```

JSON is the default output. Local filesystem paths and account identifiers are
redacted unless a command provides an explicit opt-in.

Continue with the [user guide](docs/user-guide.md) for account setup, provider
credentials, deals, recommendations, compatibility, group decisions, safe
operation plans, data locations, and deletion.

## Develop

Set up a checkout:

```text
uv sync --dev --locked
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for code layout, focused tests,
migrations, fixtures, documentation, and decision records. The exact full gate
lives in [docs/testing.md](docs/testing.md).

## Documentation

Start with the audience-specific entry point:

- [User guide](docs/user-guide.md) — current commands, privacy, and limitations.
- [Contributor guide](CONTRIBUTING.md) — development and validation workflow.
- [Documentation map](docs/README.md) — source-of-truth and lifecycle rules.
- [Design map](docs/design/README.md) — architecture, contracts, policies, and
  historical acceptance records.
- [ADR index](docs/adr/README.md) — accepted decisions and open questions.

The detailed [CLI contract](docs/design/cli-contract.md) is canonical for
machine behavior. Milestone execution documents are historical acceptance
records, not the current navigation path.

## Safety and maturity

The project is usable from a local checkout but is not yet presented as a
stable public distribution. Provider access can require personal credentials,
acknowledgment of local retention, and explicit network synchronization.
Queries that claim to be cache-only do not access providers or resolve secrets.

The durable boundary is evidence and planning: `steam-agent` may read approved
local/provider sources and mutate its own cache or explicit preferences, but it
does not mutate Steam or execute generated plans.
