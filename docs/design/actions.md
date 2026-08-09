# Actions and automation boundaries

Status: accepted M7 planner read/rank/inert-plan boundary; broker execution
classes accepted by ADR 0029-0031. External policy references last verified
2026-07-10.

The planner observes bounded local state, ranks evidence, and returns inert
human plans; the separately provisioned broker executes install/update,
verify, and launch under policy grants. Read access does not imply permission to mutate Steam, and
technical feasibility does not imply a supported automation contract. The
accepted planner boundary is
[ADR 0013](../adr/0013-m7-read-only-operation-plans.md); the execution
surface is [ADR 0027](../adr/0027-provisioned-execution.md) as re-scoped by
[ADR 0028](../adr/0028-trusted-manager-execution.md).

Valve's [Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/),
revised April 20, 2026, broadly restricts non-human-controlled automation
interacting with Steam content/services, subject to permitted Steam
functionalities. Official Web APIs remain the clearest automation basis.
Client, network, browser, SteamKit, IPC, and UI automation require a separate
policy determination or Valve permission; this document is a conservative
product boundary, not legal advice.

## Capability dimensions

A single `support_level` is insufficient. Every capability declares:

- `interface_status`: official documented, official context-limited,
  unofficial local, reverse-engineered, or human UI;
- `effect`: read, open, local mutation, account mutation, social, financial, or
  destructive;
- `auth_scope`: none, API key, local OS user, Steam session, or publisher app;
- `policy_status`: documented permission, unresolved, or prohibited;
- `confirmation`: none, policy grant (`allow` within limits, per ADR 0028),
  explicit, interactive human only;
- target account, machine, app/product, and postcondition evidence.

A documented Steamworks method may still be unusable because it is intended for
the developer's running game rather than a general consumer administrator.

## Policy vocabulary for future capabilities

The table classifies possible effects so later proposals can be reviewed. Only
the read-only observation, ranking, and inert-plan subset described in the next
section is implemented and accepted.

| Class | Examples | Initial behavior |
| --- | --- | --- |
| Inspect | Owned/installed/running/download state, local saves/screenshots, disk/system | Read official APIs or versioned local parsers |
| Plan | Launch/install/uninstall/move/verify/update, backup, mod changes | Produce a short-lived plan with risks, size/bandwidth, target, and verification |
| Open | Exact Steam/store/support/family/workshop UI | Return a typed official UI reference for the human; do not invoke it or claim the UI action completed |
| Execute local reversible | Local watchlist/feedback, save snapshot | Feature-gated, auditable, idempotent where possible |
| Execute local costly/destructive | Install, move, uninstall, delete/restore saves or mods | Install/update: broker per ADR 0027/0028, authorization per policy grant (`allow` within limits, `confirm`, `deny`); verify/repair: broker per ADR 0030, separately granted; launch: broker per ADR 0031, per-AppID allowlist, terminates `dispatched`; move: inert plan, human executes in Steam's storage UI (ADR 0029); uninstall and save/mod deletion: interactive human only |
| Remote mutation | Wishlist, Workshop subscription, friend/invite/chat, family controls | Human UI initially |
| Financial/security | Purchase, cart, trade, market, key redemption, refund, account/privacy | Interactive human only |

`steam://` URIs are supported Valve consumer entry points and this project
names them freely: uninstall plans hand the human `steam://uninstall/<appid>`
as instruction text. What is bounded is *activation*. The documented
`steam://run/<AppID>` path is the clearest potential consumer launch
primitive, but activating any URI belongs to the execution surface, so it
stays opt-in and policy-reviewed and the planner (ADR 0013) invokes none of
them. Coverage is the separate caveat: the older install, validation, and
client-console routes are not a stable supported consumer administration API,
so behavior per title is not contractual.

## Accepted M7 action boundary

- Read official Web APIs and read-only OS/local observations.
- Never write Steam ACF/VDF/client files or manipulate internal IPC.
- Never capture Steam passwords, Guard codes, session cookies, or client tokens.
- For launch/install/uninstall/move/verify/backup, return a plan and the exact
  Steam UI destination/instructions.
- Keep cloud conflicts, Workshop publishing/deletion, family changes, social
  messages, and financial operations human-controlled.
- A plan must include prohibited-execution capability/policy status,
  account/machine, deterministic plan identity, expiration, confirmation class,
  risks, rollback guidance, and unknown postconditions.

## Proposed read-only extensions

These adapters are not part of the accepted M7 surface unless the
[CLI contract](cli-contract.md) names them explicitly.

Versioned parsers may observe, with `unknown/stale` behavior:

- library folders and app manifests;
- running OS processes mapped to installed apps;
- download/update/workshop logs and transient directories;
- screenshots and recording metadata;
- local save/cache hints, never represented as authoritative Steam Cloud state;
- Proton prefixes/tools, shader caches, and per-game compatibility selections;
- non-Steam shortcuts and local collections/categories.

Steam Cloud APIs are app/OAuth scoped; they are not a general API for enumerating
every user's cloud saves. Steamworks friends, UGC, Remote Play, input, and
screenshots APIs are similarly often running-app or publisher-context surfaces.

## Additional action-oriented questions

- What is installed, licensed, updated, and ready to play now?
- What should be installed tonight for a trip tomorrow?
- Which uninstall frees enough space without risking an unsynced save or mod set?
- Which drive can fit a game, and what move plan is safest?
- What is downloading, queued, paused, or apparently stuck?
- Which machine has the newest save, recording, or screenshots?
- Which Workshop update or Proton override may explain a launch failure?
- Can the household run enough simultaneous copies, with the DLC a save expects?
- What official Steam UI should the user open to complete a guarded action?

Backups must distinguish installed game content from save/config/mod data. A
Steam content backup is not evidence that saves are protected.

## Adjacent integrations

The same contracts can eventually support other libraries without pretending
they expose equal capabilities:

- [Playnite plugin SDK](https://api.playnite.link/docs/tutorials/index.html) on Windows;
- [Heroic](https://github.com/Heroic-Games-Launcher/HeroicGamesLauncher) for Epic/GOG/Amazon and runners;
- [Lutris](https://github.com/lutris/lutris) for Linux libraries/runners;
- [PCGamingWiki API](https://www.pcgamingwiki.com/wiki/PCGamingWiki%3AAPI) for save/config/DRM/technical evidence;
- IGDB/RAWG for cross-store identity and metadata under their terms;
- ProtonDB as a community reference unless supported access permission exists.

This argues for provider-neutral internal game identities, with Steam AppID as
one external identity rather than the universal game key.
