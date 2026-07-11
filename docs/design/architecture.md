# Provisional architecture

Status: option under evaluation, not an accepted ADR

## Architectural shape

```text
provider adapters          local scanners          user-authored facts
       |                         |                         |
       +-------------------------+-------------------------+
                                 v
                    immutable/raw observations
                                 v
             normalized game/offer/operations graph
                                 v
             constraints, set operations, ranking recipes
                                 v
               stable JSON envelope + optional human view
```

The architecture separates acquisition from interpretation. Synchronization can
fail or go stale without changing query semantics, and an agent can inspect why
a derived answer exists.

The graph spans games/releases, external identities, products/offers, licenses,
accounts/people, machines, installs, sessions, saves, mods, media, and evidence.
Steam AppID remains a strong Steam identity but not the universal game key.

## Proposed components

### Provider adapters

Each adapter owns authentication, pacing, retries, raw response validation, and
capability reporting. Core logic never depends directly on a provider response
shape. Documented and provisional adapters are visibly different.

### Raw cache and observations

Raw responses are retained with timestamps and request context for debugging and
renormalization. Sensitive raw data may use a separate retention policy. A sync
writes a new observation set and only promotes it after validation.

### Normalized store

SQLite is the leading option for the canonical cache because joins, group set
operations, provenance, price snapshots, and migrations outgrow a collection of
JSON files quickly. JSON remains the import/export and stdout contract.

Use platform-native data locations rather than a notes directory: config,
durable data, disposable cache, and state/logs have different lifecycles. The
[XDG Base Directory specification](https://specifications.freedesktop.org/basedir/latest/)
is the Unix baseline; a cross-platform resolver should map macOS and Windows
appropriately.

Start with SQLite's rollback journal for the single-writer CLI. WAL is not a
default requirement, and should only be enabled if concurrent interfaces need it
and `doctor` can verify a safe SQLite build. SQLite documents a rare WAL-reset
corruption issue fixed in 3.51.3 and selected backports, which makes a casual
“always enable WAL” default inappropriate. See [SQLite WAL](https://sqlite.org/wal.html).

The schema should model more than `games(appid)`. Steam AppIDs include DLC,
software, demos, and other item types; offers may be packages or bundles. Likely
entity groups are:

- catalog items and relationships
- people/accounts and system profiles
- ownership/availability observations
- installs and local launch observations
- wishlist state and explicit user feedback
- store metadata, reviews, tags, and accessibility declarations
- offers, packages/bundles, price observations, and attributed history
- evidence/claims plus derived assessments

Exact tables are deliberately deferred until sample payload probes exist.

### Query engine

Queries have four explicit stages:

1. Select candidate scope, such as owned, installed, wishlist, store, or a group.
2. Evaluate hard constraints with three-valued outcomes.
3. Rank eligible/conditional candidates with named, versioned dimensions.
4. Attach reasons, tradeoffs, evidence, freshness, and completeness.

The first implementation need not contain embeddings or an LLM. Tags, set
operations, explicit preferences, and deterministic scoring can establish the
contract. Semantic retrieval can later produce candidate evidence without
becoming the sole explanation.

### Interfaces

The CLI is the first interface. An MCP server, library API, or daemon may wrap
the same application/query layer later; none should implement independent
provider semantics.

An optional action planner sits above the same graph. Executors are separate,
feature-gated adapters; a read provider never implicitly becomes authorized to
mutate a client or account. See `actions.md`.

## Multi-profile from the start

Group and “works on my computer” questions require multiple people and systems.
Even if v1 syncs one Steam account and one machine, identity should not be baked
in as global singleton columns. Profiles also make redaction, deletion, and
data-export boundaries clearer.

## Trust modes

- **Core**: Web API key, public/visible account data, local manifests/system,
  local price snapshots.
- **Optional provider**: explicit third-party keys and terms acceptance.
- **High trust**: SteamKit/SteamCMD or session-backed private data, isolated and
  never required for core operation.

## Non-goals for the first slice

- Free-form natural-language querying inside the CLI.
- Scraping SteamDB or bulk scraping Steam store pages.
- A universal PC benchmark or guaranteed frame-rate prediction.
- Automated purchasing, wishlist modification, or account mutation.
- Agent browser ingestion of pages whose providers permit manual viewing only.
- A black-box taste model with one unexplained score.
- An MCP implementation before the CLI schema is exercised.

## Language decision

M1 uses Python 3.12+, uv, and Hatchling as accepted in
[ADR 0001](../adr/0001-python-uv-packaging.md). The packaging spike completed
the decision gate described below: the likely hard work is normalization,
explainable ranking, data analysis, and iteration; isolated CLI installation is
available through `uv tool install`, and Python has a Tier 1 MCP SDK. TypeScript
remains a credible future alternative if MCP becomes the product, and Go may be
worth reevaluating if a zero-runtime single binary becomes a requirement.

The criteria used for that decision, and for any future reevaluation, were:

- single-command install and cross-platform packaging
- HTTP, SQLite, migrations, and VDF parsing maturity
- typed JSON/schema ergonomics
- startup time and binary/environment footprint
- testability of adapters and CLI golden output
- future library/MCP reuse without coupling the core to MCP

References: [Python CLI packaging](https://packaging.python.org/en/latest/guides/creating-command-line-tools/),
[uv tools](https://docs.astral.sh/uv/concepts/tools/), and
[official MCP SDKs](https://modelcontextprotocol.io/docs/sdk).
