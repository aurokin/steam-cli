# ADR 0012: bounded discovery universe and explicit group evidence

Status: accepted for active M6 on 2026-07-12

## Context

Discovery can easily become an unbounded catalog crawl or an inference engine
over undocumented Steam tags, social relationships, family availability, and
game health. The accepted local store already contains demand-bounded owned,
installed, wishlist, catalog, review, preference, compatibility, and
provisional appdetails evidence.

The appdetails request used by M5 already retrieves category, genre, and release
fields. A second discovery projection would duplicate provider requests,
cooldowns, retention, deletion, and last-good logic for identical response
bytes.

## Decision

M6 structurally bounds discovery per selected account, machine, and locale to
that account's visible-owned/wishlist identities, that machine's installed
identities, and AppIDs explicitly demanded in that context. Public fact rows may
be reused after authorization, but demand from one account never makes an
identity enumerable by another. It never expands through search, popularity,
similarity, friends, or the full catalog.

Evolve the single provisional projection to `declared-app-facts/0.2` and keep
0.1 readable. A new disclosure is required before persisting 0.2; migration is
offline and never rewrites legacy payloads as newly consented. Store provisional
source/version-scoped numeric category/genre identity and bounded localized
display text. Add positive-only exact and broad multiplayer declarations plus
`coming_soon: present|absent|unknown`; absence never proves availability. No
automated tags/mechanics, exact player counts, maintenance verdict, or health
verdict are derived under current sources.

Group members and any non-playing copy sources are explicit configured accounts
or synthetic profiles only.
Ownership/family facts remain three-valued and user-attributed where synthetic.
Missing-copy output is a range derived by deterministic matching over distinct
copy sources: with required copies `R`, known matching `K`, and
known-plus-unknown matching `U`, the range is `{min:max(0,R-U),
max:max(0,R-K)}`. Family availability cannot invent a concurrent copy.
Compatibility and user policy are separate dimensions. Eligibility and ranking
are pure cache-only results and are not persisted.

Ranking uses `group-fit/0.1` over the bounded cache and emits no-purchase,
minimum-copy, and preference-fit objectives separately. Guaranteed,
conditional, and insufficient copy states are never obscured by preference.
Like-X similarity names its genre/category basis; mechanic exclusion is
supported only by explicit user traits.

## Consequences

M6 remains useful for owned/wishlist/explicit candidates and truthful group
copy questions, but it is not a general Steam search engine. Many exact modes,
counts, family/concurrency states, mechanics, and health questions remain
unknown. Better documented sources or explicit user facts can extend those
dimensions without changing the universe or matching semantics.

One provider lifecycle avoids duplicate provisional requests and preserves M5
last-good/deletion hardening. Schema evolution and the expanded disclosure need
migration, legacy-row, consent, locale, retention, and no-network migration
tests before the first app-facts tracer is accepted.

Valve documents that Remote Play Together uses the host's installed copy and
that the capability can be independently disabled; therefore it is a one-host
topology and is never inferred from local multiplayer. Valve's Steam Families
FAQ describes one simultaneous player per owned copy and selection among
multiple owners, while noting share-ineligible cases; actual family eligibility
therefore remains an explicit local assertion, never inferred enumeration.

References:

- <https://partner.steamgames.com/doc/features/remoteplay>
- <https://help.steampowered.com/en/faqs/view/054C-3167-DD7F-49D4>
