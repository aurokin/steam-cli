# Architecture

Status: accepted M1–M7 architecture, maintained as the implementation evolves.

Accepted decisions are recorded in the [ADR register](../adr/README.md). This
document describes how those decisions fit together; it does not accept future
providers or action executors.

## System shape

```text
explicit local/provider sync       explicit local facts and preferences
              |                                  |
              +------------------+---------------+
                                 v
                  validated, scoped observations
                                 |
                    transactional promotion
                                 v
                 SQLite last-good projections
                                 |
              cache-only joins, gates, and recipes
                                 v
             deterministic JSON + optional table view
                                 |
                 inert human operation plans
```

Acquisition is separate from interpretation. A provider or local scan can fail,
go stale, or become inaccessible without silently changing query semantics.
Queries can explain which promoted evidence, context, and versioned rule
produced a result.

## Acquisition and validation

Local scanners and provider adapters are invoked only by explicit commands.
Each adapter owns its authentication, pacing, response validation, support
level, and request context. Core queries do not depend directly on provider
response shapes.

An acquisition attempt is recorded independently from its last-good
projection. Complete valid observations are promoted transactionally. Partial,
failed, running, abandoned, malformed, or inaccessible attempts remain visible
but do not replace complete usable data. This is the original M1 last-good rule
applied consistently to later capabilities.

Retention is capability-specific. Current adapters store bounded normalized
fields and provenance rather than general raw-response archives. Credential
probes discard response bodies and persist only coarse state. The
[Steam data lifecycle policy](steam-data-lifecycle.md) and the capability ADRs
it links are the canonical retention, acknowledgment, and deletion index.

## SQLite evidence store

SQLite is the accepted local store; JSON is the stdout and interchange
contract. Forward-only numbered migrations evolve the schema. Platform-native
data directories keep the database outside the repository by default.

The store represents multiple accounts, machines, locales, provider contexts,
and evidence attempts even when a workflow exercises one of each. Steam AppID
is a Steam application identity, not a package, bundle, edition, license kind,
or universal cross-store game identity.

Key storage concepts are:

- subjects such as accounts, machines, applications, and synthetic profiles;
- scoped attempts with provider, retrieval time, support level, and context;
- normalized observations and their evidence lineage;
- promoted last-good projections used by queries;
- explicit local feedback, rules, and group facts kept separate from inferred
  behavior; and
- versioned derived results that are returned, not persisted as hidden truth.

Private paths and identifiers may exist in the local database when required for
the capability, but normal query output redacts them.

## Query and ranking layer

Cache-only commands do not acquire missing evidence or resolve credentials.
Some use explicitly query-only SQLite connections; others may perform normal
database migration or maintenance before reading the cache. Commands that rank
candidates use these stages:

1. select an authorized candidate universe;
2. evaluate hard gates with pass, fail, or unknown outcomes;
3. apply a named, versioned deterministic ranking recipe; and
4. return factors, tradeoffs, lineage, freshness, and completeness.

`unknown`, `false`, empty, inaccessible, and stale are distinct throughout the
store and response. Subjective evidence cannot turn an unknown hard gate into a
pass. Money uses integer minor units with currency and country context.

The [CLI contract](cli-contract.md) is canonical for command, envelope, schema,
error, and exit behavior. The [product questions](product-questions.md) define
the evidence distinctions required by user questions, not a promise that every
question is currently implemented.

## Interfaces and action boundary

The CLI is the supported interface. Command dispatch and rendering live in
`src/steam_agent/cli.py`; normalized result envelopes in `contracts.py`;
storage and migrations in `storage.py` and `migrations/`; bounded adapters in
the provider/local modules; and pure queries and ranking in their domain
modules.

M7 ends at local observation, storage ranking, and inert plans. Plans contain
human instructions and typed official references but do not open them, spawn a
client, touch Steam files, or claim an action completed. Read capability never
implies mutation authority. See the accepted
[action boundary](actions.md) and
[ADR 0013](../adr/0013-m7-read-only-operation-plans.md).

An MCP server, library API, daemon, semantic retriever, or another game-library
adapter could reuse the application/query layer in the future. None is accepted
merely by appearing here, and none should duplicate provider or truth-state
semantics.

## Security and privacy boundaries

- Credentials enter through hidden input and use provider-scoped native keyring
  entries or an explicit protected-file fallback.
- Secrets, raw authentication failures, account identifiers, and personal
  filesystem paths do not appear in normal output, fixtures, or logs.
- Cache-only commands do not make network requests, resolve secrets, open URLs,
  or collect new system state.
- Account/machine/provider deletion is transactional and scoped; shared public
  facts remain only when another retained subject needs them.
- Local deletion cannot claim to erase user-controlled backups or revoke a key
  at its provider.

## Deliberate non-goals

- Free-form natural-language interpretation inside the CLI.
- SteamDB scraping or unreviewed browser/client automation.
- A universal hardware benchmark or guaranteed frame-rate prediction.
- Automated launch, install, move, uninstall, purchase, wishlist, social, or
  account mutation.
- Hidden preference inference that overwrites explicit feedback.
- Treating manual-only references as approved automated-ingest sources.
