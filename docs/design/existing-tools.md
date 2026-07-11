# Existing tool evaluation

Status: repository survey, verified 2026-07-10

The useful existing projects are young live-API proxies or exporters. They offer
interface, validation, and fixture ideas, but none has the durable temporal and
provenance model required here. The current recommendation is to borrow patterns,
not fork one as the foundation.

| Project | Useful ideas | Why not use as the foundation |
| --- | --- | --- |
| [jkiley129/steam-mcp](https://github.com/jkiley129/steam-mcp) | Concise tool vocabulary, cache freshness | Singleton account, filesystem JSON TTL cache, narrow model, no history |
| [FunnyEntity/steam_library_exporter](https://github.com/FunnyEntity/steam_library_exporter) | Provider field discovery and export fixtures | Extremely new, flat batch export, no incremental sync |
| [obrien-matthew/mcp-steam](https://github.com/obrien-matthew/mcp-steam) | Typed tools, achievements/stats, error handling | Python 3.14 floor, direct live calls, no durable temporal store |
| [imnotStealthy/steam-mcp](https://github.com/imnotStealthy/steam-mcp) | Broad capability grouping, validation, redaction, pacing | In-memory cache, formatted strings rather than durable structured evidence, no deal/history layer |
| [dsp/mcp-server-steam](https://github.com/dsp/mcp-server-steam) | Container distribution | Heavy and comparatively stale for a personal local CLI |

All five currently report MIT licenses; license and commit state must still be
rechecked immediately before code reuse. Existing schemas should be treated as
examples, not evidence that a provider contract is supported.

Specific patterns worth reproducing independently:

- clear capability grouping and tool descriptions;
- typed schemas and structured errors;
- request pacing, timeouts, and secret redaction;
- cache age surfaced in results;
- focused fixtures for Steam response shapes.

Patterns to avoid:

- direct provider calls from each CLI/MCP handler;
- a global singleton account;
- JSON files as a second mutable source of truth;
- silently swallowing cache corruption or write failures;
- returning only prose or a final score;
- choosing a high language/runtime floor without a demonstrated need.
