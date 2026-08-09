---
name: steam-agent
description: Answer questions about a user's Steam library from the local-first `steam-agent` CLI. Use for finding owned, installed, or wishlisted games; checking playtime, multiplayer declarations, compatibility, deals, group fit, recommendations, and storage candidates; or explaining the CLI's evidence and safety boundaries.
---

# Use Steam Agent

Use `steam-agent` as the evidence source. Prefer its cache-only reads and keep the answer within the evidence returned by the CLI.

## Query safely

1. Start with `steam-agent --help` when the intent-to-command mapping is unclear. Use `<family> <leaf> --help` for exact options.
2. Put global `--data-dir` before the command. Request `--format json` and inspect the returned context, completeness, warnings, and evidence fields before answering.
3. Run only the smallest read that answers the question. Do not sync, authenticate, probe a provider, or change Steam Agent's local state unless the user explicitly asks to acquire or change that evidence. Never do those things in a read-only evaluation.
4. Never launch, install, uninstall, move, verify/repair, or otherwise change Steam from this skill. `steam-agent` cannot do it, and `operations plan` emits instructions for a human rather than authorizing execution. Execution exists only in a separate, separately provisioned CLI with its own skill; it is out of scope here and must not be invoked from this one.
5. State what the evidence supports and what remains unknown, stale, partial, inaccessible, or unsupported. Do not collapse any of those states into `false` or an empty result.

## Choose the command

- Find library membership or installation: `games query`. Use `--scope library` to join visible-owned and installed evidence, `--scope installed` for this machine, `--scope wishlist` for wishlist membership, and `--scope owned --playtime zero` for owned games with zero recorded playtime.
- Check multiplayer declarations or filter explicit candidates: `discovery query`. For `--scope appids`, repeat `--appid` once per candidate and set `--limit` to the candidate count. Add `--require-mode online_co_op` only when filtering to positive declared support.
- Recommend an owned game: `recommendations query`. Use `--recipe resume/0.1` to continue something or `--recipe preference-fit/0.1 --require installed=true` for an installed choice.
- Rank cached wishlist fit: `recommendations wishlist`.
- Rank cached wishlist deals: `deals query --scope wishlist`; keep country, store class, provider attribution, and freshness visible.
- Assess an explicit target: `compatibility assess APPID --target machine:ALIAS`; report bounded compatibility evidence, not promised performance or frame rate.
- Rank group fit: `group recommend`. Use `group ownership` or `group eligibility` when the user wants copies or hard eligibility for explicit AppIDs.
- Rank reclaim-space or travel candidates: `storage rank`. A reclaim candidate is not proof that uninstalling is safe, backed up, or recoverable. `reclaim_bytes` is what an uninstall frees, not what the title occupies — a `residual_content` gate means a Proton prefix, shader cache, or Workshop content stays behind, and `operations plan uninstall` reports how much. Never present the reclaim figure as the space recovered when that gate is present.

- Prepare an operation for a human or for the broker: `operations plan launch|install|uninstall|move|verify|backup APPID --account ALIAS --machine MACHINE` (move also needs `--destination-library-ordinal`). The plan is inert: it returns risks, preconditions, and instructions, and authorizes nothing. Use `operations observe --machine ALIAS` for current local installed state.

Supply the account, machine, country, and language context required by the leaf help. Prefer configured aliases from the user's environment; do not expose raw account identifiers or private filesystem paths.

## Preserve evidence boundaries

- Treat declared multiplayer categories as positive-only, three-valued evidence. Absence is unknown, not evidence that a mode is unsupported.
- Exact numeric player counts are currently unsupported. Say so directly instead of deriving a count from multiplayer categories.
- Separate hard eligibility from subjective ranking. A high score cannot override a failed or unknown hard gate.
- Keep ownership, wishlist membership, and installation state separate. Installation is machine-specific.
- Do not follow returned links automatically. Distinguish human-open references from agent-readable or automated-ingest evidence. An `uninstall` plan's `ui_instructions` include a `steam://uninstall/<appid>` shortcut for the human: relay it as text. `steam://` is a supported Valve mechanism, so quoting one is unremarkable — activating any URI from this skill crosses the read-only boundary.
- When the cache lacks sufficient evidence, name the missing or stale capability. Ask before proposing any command that would contact a provider or persist new evidence.
