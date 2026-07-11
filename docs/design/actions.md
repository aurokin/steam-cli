# Actions and automation boundaries

Status: working safety and policy model, verified 2026-07-10

The expanded product is an answerable game-state graph with an optional action
planner. Read access does not imply permission to mutate Steam, and technical
feasibility does not imply a supported automation contract.

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
- `confirmation`: none, explicit, interactive human only;
- target account, machine, app/product, and postcondition evidence.

A documented Steamworks method may still be unusable because it is intended for
the developer's running game rather than a general consumer administrator.

## Operation classes

| Class | Examples | Initial behavior |
| --- | --- | --- |
| Inspect | Owned/installed/running/download state, local saves/screenshots, disk/system | Read official APIs or versioned local parsers |
| Plan | Launch/install/uninstall/move/verify/update, backup, mod changes | Produce a short-lived plan with risks, size/bandwidth, target, and verification |
| Open | Exact Steam/store/support/family/workshop UI | Return/open official UI for the human; do not claim the UI action completed |
| Execute local reversible | Local watchlist/feedback, save snapshot | Feature-gated, auditable, idempotent where possible |
| Execute local costly/destructive | Install, move, uninstall, delete/restore saves or mods | Deferred until supported mechanism and policy basis; explicit confirmation |
| Remote mutation | Wishlist, Workshop subscription, friend/invite/chat, family controls | Human UI initially |
| Financial/security | Purchase, cart, trade, market, key redemption, refund, account/privacy | Interactive human only |

The documented `steam://run/<AppID>` path is the clearest potential consumer
launch primitive, but remains opt-in and policy-reviewed. Older install,
uninstall, validation, client-console, and command-line mechanisms are not a
stable supported consumer administration API.

## Initial action boundary

- Read official Web APIs and read-only OS/local observations.
- Never write Steam ACF/VDF/client files or manipulate internal IPC.
- Never capture Steam passwords, Guard codes, session cookies, or client tokens.
- For install/uninstall/update/move/verify, initially return a plan and the exact
  Steam UI destination/instructions.
- Keep cloud conflicts, Workshop publishing/deletion, family changes, social
  messages, and financial operations human-controlled.
- A plan must include capability/policy status, dry-run detail, account/machine,
  idempotency key, expiration, confirmation class, rollback, and postcondition.

## Useful read-only local adapters

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
