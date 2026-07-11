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
queries. **The M2 truthful-account-inventory milestone is implemented and
accepted:** local account selection, secure key storage, live provider
classification, durable visible-owned synchronization, joined owned/installed
and bounded catalog queries, truthful freshness, and transactional deletion are
available. Wishlists, pricing, recommendations, compatibility, and Steam
actions remain proposed design work behind later activation checkpoints.

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
uv run python scripts/package_smoke.py
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

## M2 truthful account inventory

M2 can discover a local Steam account without returning account names or
identifiers, configure the uniquely most-recent account under a local alias,
report the account/credential/probe axes independently, and synchronize the
visible owned library:

```text
uv run steam-agent accounts discover
uv run steam-agent accounts configure --from-local-most-recent --alias primary
uv run steam-agent accounts status --alias primary
uv run steam-agent auth status steam-web-api
uv run steam-agent owned capability --account primary
uv run steam-agent sync owned --account primary --acknowledge-local-storage
uv run steam-agent sync catalog --account primary --machine local
uv run steam-agent games query --scope owned --account primary
uv run steam-agent games query --scope library --account primary --machine local
```

If discovery is ambiguous, rerun it with `--include-identifiers`, then configure
one of the listed local identities with `--steam-id64 ID`. Identifiers appear
only after that explicit opt-in.

Identifiers remain redacted unless `accounts status --include-identifiers` is
explicitly requested. Store a user Web API key through the hidden interactive
prompt; it is never accepted on the command line:

```text
uv run steam-agent auth set steam-web-api
uv run steam-agent owned probe --account primary
```

The same hidden-input boundary can preconfigure optional provider keys without
activating their later data adapters:

```text
uv run steam-agent auth set isthereanydeal
uv run steam-agent auth set steamgriddb
uv run steam-agent auth set gg-deals
uv run steam-agent auth status isthereanydeal
uv run steam-agent auth probe steamgriddb
uv run steam-agent auth probe gg-deals
```

Third-party probes are separate, explicit, read-only calls. They use one known
Steam AppID, retain no response body, and do not fetch or persist price/artwork
data. ITAD live use remains gated on a canonical public project URL or private
approval; its API key can still be stored and resolved locally now. OAuth client
secrets are not needed for public pricing endpoints and are not stored.

The default backend is the native OS credential store. POSIX users can select
the unencrypted permission-protected fallback only with both `--backend file`
and `--yes-file-risk`. There is no automatic downgrade. An owned capability
probe is an explicit network operation; it discards the response body and
persists only coarse probe state. It does not synchronize or persist games.
See the [credential ADR](docs/adr/0003-credential-storage.md) and
[Steam data lifecycle policy](docs/design/steam-data-lifecycle.md).

`sync owned` is a separate explicit network operation. Its first persistent
run requires the versioned local-storage acknowledgment shown above. It compares
the documented default and played-free-expanded results, stores only normalized
AppID, optional name, lifetime playtime, inclusion basis, and provenance, and
promotes only a complete valid pair. Failed or inaccessible attempts preserve
the last-good snapshot. Per-account deletion preserves the shared key; the
all-provider termination path also removes the locally managed key/reference:

```text
uv run steam-agent data delete --provider steam-web-api --account primary --yes
uv run steam-agent data delete --provider steam-web-api --all --yes
```

Deleting one account also removes its catalog attempt and demand history.
Public catalog facts are retained only when another account or installed-machine
projection still needs the AppID; retained facts no longer reference the
deleted account. The shared Web API key remains configured.

`sync catalog` uses Valve's documented ordered store catalog, but stores facts
only for AppIDs already observed in the selected owned/installed projections.
Because Valve provides no supported arbitrary-AppID filter, retrieval may scan
several pages even though persistence stays demand-bounded. The joined query
keeps application identity separate from package, bundle, and edition identity;
unsupported mappings remain explicitly unknown.

Catalog attempt status is scoped to the selected account, machine, and demanded
AppIDs. The query selects the newest relevant attempt independently per AppID
and reports the aggregate, so one successful AppID cannot hide another AppID's
failure. Retained catalog facts older than 24 hours are reported as stale; a
sync for unrelated demand does not change that status.
The same boundary applies to classification and provenance: another account's
newer shared catalog observation does not replace this subject's last-good fact.
A running refresh reports `SYNC_IN_PROGRESS` without degrading fresh last-good
facts. When there are no demanded AppIDs, catalog completeness is complete and
does not inherit an earlier attempt's status.
If a new AppID has no last-good fact yet, its running, abandoned, or failed
attempt is reported alongside `NOT_SYNCED` instead of being hidden behind a
generic unavailable result.

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
- [Testing and deterministic acceptance](docs/testing.md)
- [Product questions](docs/design/product-questions.md)
- [Evidence and provider matrix](docs/design/evidence-matrix.md)
- [Steam account data lifecycle and deletion gates](docs/design/steam-data-lifecycle.md)
- [Provisional architecture](docs/design/architecture.md)
- [CLI and JSON contract](docs/design/cli-contract.md)
- [Historical pricing strategy](docs/design/pricing-strategy.md)
- [Actions and automation boundaries](docs/design/actions.md)
- [Existing tool evaluation](docs/design/existing-tools.md)
- [Historical research-backed validation sequence](docs/design/roadmap.md)
- [M1 execution plan and Linear work graph](docs/design/m1-execution.md)
- [M2 truthful account inventory execution and evidence](docs/design/m2-execution.md)
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
