# M6 discovery, household, and groups execution plan

Status: historical acceptance record; accepted 2026-07-12

This file preserves M6 scope and evidence at acceptance time. Use the
[user guide](../user-guide.md) and [CLI contract](cli-contract.md) for current
behavior across M1–M7.

## Outcome and sequence

M6 answers three bounded questions without crawling Steam or inventing social,
mechanics, player-count, or health facts:

1. Which already-known or explicitly named games have useful declared genre,
   release, and multiplayer-mode evidence?
2. What is the ownership union/intersection and possible missing-copy range for
   an explicitly selected set of configured accounts or synthetic profiles?
3. Which eligible bounded candidates best fit separate no-purchase,
   minimum-copy, and preference-fit objectives?

Linear sequence:

1. AUR-634 evolves the existing appdetails projection and exposes multiplayer
   evidence before ranking.
2. AUR-639 adds user-authored synthetic/family facts and a cache-only group
   eligibility engine.
3. AUR-640 adds immutable `group-fit/0.1` ranking over those accepted inputs.

## Candidate-universe boundary

Candidate authorization is scoped independently from reusable public fact rows.
For one selected account/machine/locale context, the universe is exactly its
visible-owned and wishlist AppIDs, that machine's installed AppIDs, and explicit
AppIDs demanded in the same context. `known` means only that scoped union; an
identity is never enumerable merely because another account demanded it. A
group query may union only the explicitly selected members and copy sources.
No store-wide enrichment, store search, popularity expansion, related-game
traversal, friend import, or background crawl is allowed. Queries reject
out-of-universe AppIDs and never fetch implicitly.

`IStoreService/GetAppList` remains a catalog identity/change signal. Its
`last_modified` can reflect any information or price change and is never a
maintenance or game-health fact.

## Declared app facts 0.2

The approved fixed-host provisional appdetails request already downloads
categories, genres, and release fields. M6 evolves the single normalized
projection to `declared-app-facts/0.2`; it does not create a second provider
lifecycle or duplicate requests. Existing 0.1 rows remain readable and fill new
fields as unknown until refreshed.

Persist category and genre numeric IDs as identities scoped to the provisional
`steam_store_appdetails` source and projection version, not canonical
cross-source IDs. Localized labels/date text are bounded display-only values.
Release stores `coming_soon: present|absent|unknown` and localized date text;
`absent` never becomes a universal released, purchasable, or available verdict.
No universal timestamp is parsed.

Positive provisional category mappings distinguish:

- broad multiplayer, co-op, PvP, and shared/split-screen declarations;
- exact online PvP, shared/split-screen PvP, online co-op,
  shared/split-screen co-op, and Remote Play Together declarations.

Unknown numeric IDs survive. Missing/empty category evidence does not prove a
mode false. Broad or exact positive declarations can pass their own gates;
otherwise the state is unknown unless an explicit user-authored absent assertion
exists. A broad flag never proves an exact mode. Remote Play Together is never
inferred from another category.

Steam genres are broad localized store genres, not tags or mechanics. Steam
tags have no approved public ingestion endpoint under this project's boundary;
mechanic exclusions can use only explicit `user:<slug>` assertions. Exact
player limits are likewise user-declared or unknown. The optional documented
current-player API is deferred: a Steam-connected population snapshot would not
prove matchmaking health or maintenance.

## Household and copy eligibility

A participant is exactly one configured account alias with its own accepted
data lifecycle, or one explicit synthetic profile. A copy source is separately
and explicitly selected and may be a non-playing configured/synthetic profile.
There is no friend/family enumeration and no third profile kind. Machine output
uses request-local member/source ordinals; it omits aliases, SteamID64, provider
IDs, names, ages, relationships, and raw policy prose. Aliases are privacy
canaries because a synthetic alias may itself contain a real name.

Visible-owned presence is positive evidence; omission remains unknown. Synthetic
ownership and family availability are user-authored facts with explicit
present/absent/unknown state. Family availability never becomes an authoritative
Steam fact and cannot create a copy without a mapped lending copy source.

Union and intersection use three-valued logic and retain every per-participant
state. Exact local/online/co-op/PvP/Remote Play modes never substitute for one
another. Missing player limits make the count gate unknown, except that an exact
multiplayer mode establishes the semantic two-player floor.

Concurrent-copy calculation is deterministic bipartite matching between
participants and distinct entitlement/copy-source identities. A mapped
`from-profile` present edge is known, unknown is possible, and absent supplies
no edge. One source cannot concurrently serve its owner and a borrower. For
required copies `R`, known-edge maximum matching `K`, and known-plus-unknown
matching `U`, the range is `min=max(0,R-U)`, `max=max(0,R-K)`.
`no-purchase` is guaranteed only when `K >= R`; `U >= R` alone is conditional.
Local same-device and Remote Play Together use `R=1`; LAN/online use the
participant count. Compatibility and user-policy suitability remain separate
dimensions and never rewrite ownership, mode, count, or copy results.

Synthetic profiles, ownership assertions, mapped family shares, player-limit,
preference, trait, and generic user-policy assertions are durable user-authored
facts under a new synthetic/group storage disclosure. Creation requires explicit
acknowledgment; ordinary output warns that backups may retain deleted data.
Aliases cannot collide across configured accounts and synthetic profiles.
Create/query/clear/delete operations are idempotent, and SQLite secure-delete
behavior is tested. Group eligibility and ranking results are never persisted.
Deleting a synthetic profile cascades only its assertions. Account or
all-Steam deletion removes that account's edges but never deletes unrelated
synthetic facts; dependent results become unknown rather than absent.

Persisting `declared-app-facts/0.2` requires a new disclosure version before any
expanded sync. M5 consent never silently upgrades. Offline migration widens both
CHECK-constrained observation tables to accept 0.1 and 0.2 without rewriting old
payloads. M5 compatibility deserializes both identically, treating added fields
as unknown for 0.1. A failed or partial 0.2 refresh cannot displace a usable 0.1
last-good row. Account/provider deletion and 30-day pruning cover both versions.

## Query contracts

Planned tracer commands:

```text
steam-agent sync app-facts --scope known|library|wishlist|installed|appids --account ALIAS --machine MACHINE --country CC --language LANG [--appid APPID...] [--max-items N] [--acknowledge-local-storage]
steam-agent discovery query [APPID...] --scope known|library|wishlist|installed|appids --limit N --account ALIAS --machine MACHINE --country CC --language LANG [--require mode:MODE] [--format json|table]
steam-agent profiles create synthetic:ALIAS --acknowledge-group-storage --acknowledge-backups
steam-agent profiles get|delete synthetic:ALIAS [--yes]
steam-agent profiles list
steam-agent profiles clear-account account:ALIAS --yes
steam-agent ownership set PROFILE APPID owned|not_owned|unknown
steam-agent ownership clear PROFILE APPID --yes
steam-agent family set RECIPIENT APPID available|unavailable|unknown --source SOURCE
steam-agent family clear RECIPIENT APPID --source SOURCE --yes
steam-agent fact set PROFILE APPID players:min|players:max|trait:user:SLUG|policy:user:SLUG VALUE
steam-agent fact clear PROFILE APPID FACT --yes
steam-agent group ownership APPID... --member MEMBER... [--copy-source SOURCE...] --account ALIAS --machine MACHINE --country CC --language LANG
steam-agent group eligibility APPID... --member MEMBER... [--copy-source SOURCE...] --account ALIAS --machine MACHINE --country CC --language LANG --mode MODE [--host MEMBER] [--policy user:SLUG]
steam-agent group recommend --scope known|library|wishlist|installed|appids --limit N [--appid APPID...] --member MEMBER... [--copy-source SOURCE...] --context-account ALIAS --context-machine MACHINE --country CC --language LANG --mode MODE [--host MEMBER] --objective no-purchase|min-copies|preference-fit [--like MEMBER=APPID] [--dislike MEMBER=APPID] [--exclude-trait user:SLUG]
```

The final grammar may be compacted during tracer implementation, but explicit
members, sources, mode, objective, locale, candidate authorization, and bounds
cannot be inferred. Compatibility is omitted unless every participant has an
explicit target machine mapping. Policy aggregation is explicit (`all`, `any`,
or `host`); the initial tracer implements `all` only.

## Deterministic group-fit oracle

Every objective first preserves copy certainty: guaranteed, conditional, then
insufficient. It never lets preference hide purchase uncertainty.

- `no-purchase` includes only guaranteed `{min:0,max:0}` candidates.
- `min-copies` sorts by missing-copy maximum, minimum, eligibility state, then
  AppID.
- `preference-fit` sorts by least-member score, total score, then AppID after
  the certainty class.
- similarity is weighted Jaccard over source-scoped numeric IDs: genre weight 2,
  category weight 1. Positive and negative examples are scored separately.
- a missing seed yields unknown similarity. An exclusion applies only to an
  explicit matching user-authored trait; missing traits remain unknown.

## AUR-634 refinement

Under the approved no-scraping/no-reverse-engineered-cache boundary, automated
tags/mechanics, provider player limits, and a maintenance/health verdict are not
available. AUR-634 therefore accepts declared genres/modes/release plus explicit
unsupported/unknown health and count fields. Existing review aggregates remain
sentiment only. This is a deliberate truthful reduction, not a substitute
signal.

## Acceptance harness

The numbered oracle contains at least: M601 legacy 0.1 read; M602 consent upgrade
required; M603 offline table migration; M604 positive exact/broad category
ladders; M605 missing-category unknown; M606 exact-mode non-substitution; M607
locale isolation; M608 genres-not-tags; M609 coming-soon absent does not imply
available; M610 unknown player limits; M611 Kleene union/intersection; M612
copy-source deduplication and owner/borrower contention; M613 missing-copy
formula; M614 local/RPT host topology; M615 deletion/account isolation; M616
deterministic objective ordering; and M617 bounded cache-only/privacy tripwires.

Provider acceptance uses redacted fixtures and a tiny bounded public live probe;
it records no AppIDs, titles, labels, response bodies, account identifiers, or
credentials. Diffwarden and adversarial review run to zero valid issues before
M6 acceptance.

## Acceptance evidence

Accepted on 2026-07-12 with 1,375 repository tests, Ruff, source/wheel builds,
focused cache-only/privacy/migration oracles, and a final Diffwarden review with
zero findings. The tracer includes `declared-app-facts/0.2`, scoped explicit
demand, discovery queries, durable synthetic/account group facts, copy-range
eligibility, and all three deterministic `group-fit/0.1` objectives. Automated
tags/mechanics, provider player counts, health verdicts, social enumeration,
scraping, and implicit network queries remain unsupported.
