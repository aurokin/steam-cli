# Product questions

Status: working design, 2026-07-10

The product goal is not a collection of one-off reports. It is a composable
evidence system that lets an agent choose a candidate set, apply hard
constraints, rank eligible candidates, and explain the result.

## Question families

| Family | Examples | Evidence required |
| --- | --- | --- |
| Inventory | What do I own? What is installed? | Visible ownership, app type, local manifests, license/availability source, freshness |
| Library filtering | Which owned games support controllers, macOS, or local co-op? | Platform, input, multiplayer, language, and accessibility fields |
| Next to play | What should I play tonight? What should I resume? | Availability, install state, recency, playtime, achievements, explicit preferences, session constraints |
| Backlog | What is unplayed, abandoned, or finishable this weekend? | Playtime, recency, completion evidence, length estimate or user override, install size |
| Compatibility | What works well on this system? | System profile, declared OS support and requirements, Deck evidence where relevant, launchers/anti-cheat, local observations |
| Mood and time | Something relaxing I can play for 30 minutes? | Tags, save/interruptibility, session estimate, accessibility and difficulty options, user feedback |
| Discovery | What is like X but without crafting? | Candidate catalog, weighted tags/mechanics, explicit positive and negative examples, exclusions |
| Wishlist | What fits me now? | Wishlist state, preference fit, compatibility, reviews, price and release state |
| Deals | Is this a good deal or should I wait? | Region, currency, exact edition/package, current price, local snapshots, attributed history, timestamp |
| Multiplayer/group | What can four of us play without buying copies? | Per-person availability, privacy completeness, exact multiplayer mode/count, Remote Play Together, system/input constraints |
| Achievements | What am I close to finishing? | Schema, unlocks, timestamps, global rarity, playtime; rarity alone is not effort |
| Accessibility | What meets these non-negotiable needs? | Developer-declared features and user requirements represented as pass/fail/unknown |
| Household/family | Do we need another copy? What is child-appropriate? | Copy ownership, family availability, share eligibility, parental rules; most are not public API facts |
| Current health | Is multiplayer alive or the game maintained? | Review windows, announcements/updates, player snapshot, service evidence with strong caveats |
| Data quality | Why is this missing? How sure are you? | Capabilities, source freshness, privacy failures, conflicts, unsupported fields |
| Ready now | What is licensed, installed, updated, and launchable with this input/device? | License source, family-copy state, install/update/download state, runtime/login/online requirements |
| Install/storage | What should I install for a trip? What can I remove to free 100 GB? | Drive space, install/download size, saves/mods/shaders, update queue, rollback and bandwidth |
| Saves/media | Which machine has the newest save? What needs backup? | Local save/cache evidence, cloud scope caveats, screenshots/recordings, timestamps and conflicts |
| Mods/Workshop | Which mod update broke this save? Can this setup be reproduced? | Subscriptions, manifests, dependency/order/config snapshots, game-specific manager evidence |
| Client action | Launch this; prepare an install; open the correct family/support page | Capability/policy status, target account/machine, action plan, confirmation and postcondition |

Useful cross-cutting questions include uninstall candidates, duplicate
edition/bundle purchases, newly compatible games, games that gained required
accessibility features, and group choices that minimize missing copies.

## Facts that must remain distinct

- `owned`, `family_available`, `installed`, `playable_now`, and `purchasable`
- `supports_os`, `meets_minimum`, `likely_good_experience`, and `deck_status`
- `local_multiplayer`, `local_coop`, `online_coop`, `online_pvp`, and
  `remote_play_together`
- Steam price, cross-store price, provider historical low, and locally observed
  price
- explicit preference, behavioral signal, provider claim, and derived inference

Every constraint has one of `pass`, `fail`, or `unknown`. Inaccessible and stale
data add separate state; they do not silently become `unknown` or an empty list.

## Compatibility is an assessment, not a fact

“Will this run well?” has no trustworthy generic source. A defensible answer
combines several evidence layers:

1. Hard platform gates: OS, architecture, VR or required device.
2. Publisher claims: usually unstructured minimum/recommended requirements.
3. Target-specific evidence: Valve's Deck review is strong only for Valve's
   fixed target and criteria.
4. Runtime risks: anti-cheat, launcher, account, network, and compatibility layer.
5. Local evidence: installed state, prior successful launches, user observations.

The result should say `compatible`, `incompatible`, `conditional`, or `unknown`,
then explain each layer. It should not promise a resolution or frame rate that
the evidence does not support.

## Preference evidence

Strong signals include explicit ratings, repeated sessions, recent sustained
play, completion, and explicit “more like this” examples. Owning, wishlisting,
zero playtime, or a short session are ambiguous.

The CLI therefore needs local feedback Steam does not know: liked/disliked,
finished/abandoned, avoided mechanics, tolerated launchers, accessibility needs,
motion-sickness constraints, and temporary “not now” snoozes.

## Product boundary

The CLI may provide versioned ranking recipes such as `deal`, `resume`,
`finishability`, `group_fit`, and `preference_fit`. It must also return the
component scores, matched evidence, tradeoffs, and model/rule version. Natural
language interpretation belongs to the calling agent.

The broader architecture may support action planning, but questions and actions
stay separate. “What should I uninstall?” is an evidence/ranking query; actually
uninstalling is a costly mutation with a new authorization and verification step.
