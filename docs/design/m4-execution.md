# M4 next-to-play and preference execution plan

Status: active 2026-07-11

## Outcome and sequence

M4 answers a bounded set of recommendation questions without turning inferred
behavior into user intent:

- what should I play or resume next;
- what is plausibly finishable under the evidence available;
- which backlog candidates satisfy explicit constraints; and
- which wishlist candidates combine direct preference evidence with M3 deal
  evidence.

Linear sequence:

1. AUR-621 captures explicit feedback, per-game user overrides, and durable
   feature rules.
2. AUR-630 captures normalized activity and optional achievement evidence.
3. AUR-637 applies versioned, deterministic play recipes after three-valued
   eligibility gates.
4. AUR-642 joins direct wishlist preference dimensions to the accepted M3 deal
   snapshot without reinterpreting price evidence.

AUR-621 and AUR-630 are independent foundations. Both gate AUR-637. AUR-642
also depends on the accepted M3 wishlist and deal boundary. M3 user testing is
not an M4 prerequisite. The common-question evaluation track in AUR-652 is
cross-milestone and non-blocking.

AUR-637 is implemented through `recommendations query` and
`recommendations/0.1`. Its normal-CI tracer constructs a temporary SQLite
profile, freezes the clock, repeats the query byte-for-byte, changes direct
feedback, exercises known non-game and unknown classification gates, failed
last-attempt plus stale last-good evidence, no-eligible and deletion outcomes,
JSON/table views, strict expressions, and fresh installed-wheel behavior. The
R01-R10 deterministic eval scenarios are active; natural-language judging
remains opt-in.

M4 does not add compatibility assessment, group recommendations, broad store
discovery, full price-event history, Steam actions, or natural-language model
inference inside the CLI.

## Evidence boundary

### Explicit local evidence

User-authored sentiment, finished/stopped state, snoozes, session/remaining-time
overrides, per-game feature assertions, and profile feature rules are explicit
local evidence. They are never inferred from ownership, wishlist membership,
playtime, achievements, or a calling agent's prose.

Durable profile state is account-scoped and uses stable Steam application
identity rather than a title. Temporary query constraints such as installed
only, time available tonight, unknown handling, or a one-call override remain
in command context and are never persisted as a side effect.

The CLI word `abandon` records the user-authored state `user_abandoned`; it is
not the same vocabulary as an abandoned synchronization. Finished,
user-abandoned, and active/unset are mutually exclusive. Snooze is a separate
time-bounded gate. Clearing or replacing explicit state is itself explicit.

Feature assertions and rules use a bounded `user:<slug>` namespace. They are
exact-match user vocabulary, not a claim that Steam or Steam Agent understands
the feature semantically. Missing candidate evidence produces `unknown`, never
an inferred pass or fail.

### Steam activity and achievements

The official public Web API host and existing user key are used through fixed
HTTPS requests. Valve documents `GetRecentlyPlayedGames` and `GetOwnedGames`
under [`IPlayerService`](https://partner.steamgames.com/doc/webapi/iplayerservice?language=english),
and player achievements/game schemas under
[`ISteamUserStats`](https://partner.steamgames.com/doc/webapi/ISteamUserStats?l=english).
The [Web API overview](https://partner.steamgames.com/doc/webapi_overview)
defines the public interface and authentication model.

Activity retains normalized current facts only: AppID, nullable lifetime and
platform playtime, nullable disconnected/Deck playtime where returned,
nullable last-played time, and nullable recent-window minutes. Recent-window
membership is not a session log and does not prove an exact play date.

Achievements are bounded optional enrichment. Per-AppID outcomes distinguish
ready, profile-private, unsupported/no-stats, authentication failure, rate
limit, provider failure, invalid response, and unevaluated. A private or
unsupported result is not zero progress. Player state joins schema only on
AppID plus stable achievement API name. Locked hidden achievement titles and
descriptions are suppressed from normal output.

Steam activity does not prove session length, interruptibility, remaining
completion time, preference, or finishability. Achievement percentage is an
inspectable factor, not game progress truth.

### Metadata limitation

The accepted repository has no supported cross-game trait/tag or release
source. Until a separate provider boundary is approved, preference propagation
between games remains unknown. Valve's documented
[`appreviews` endpoint](https://partner.steamgames.com/doc/store/getreviews)
may provide an attributed aggregate review dimension without retaining review
text or author data. AUR-642 may use direct per-AppID feedback, explicit user
feature assertions/rules, this separate review summary, and M3 deal evidence,
but must not claim that an unseen wishlist game fits the user's taste from
unsupported metadata.

ITAD metadata remains deferred under the M3 public-project/private-approval
condition even though it could provide exact AppID-mapped tags and release
facts. Local `appcache/appinfo.vdf` may be researched later as an explicit
local heuristic; M4 acceptance does not depend on its reverse-engineered
format. IGDB, RAWG, and SteamSpy are not M4 dependencies.

## Retention, privacy, and deletion

- Activity, player-achievement, and recent-play responses retain no raw body.
- Activity current facts are fresh for six hours; recent-window facts are fresh
  for one hour and unusable as current after 24 hours. Normalized last-good rows
  and attempt lineage are hard-deleted within seven days.
- Player achievement state is fresh for six hours and hard-deleted within seven
  days. Public localized schema facts may remain for 30 days; a negative
  no-schema result remains for at most seven days.
- Explicit feedback and profile rules remain until the user clears them or
  deletes the account. They are local user-authored state, not provider cache.
- Account-scoped and all-Steam-data deletion remove M4 activity,
  achievements, feedback, rules, evidence, and attempt lineage for the deleted
  account. Price-provider deletion does not remove local feedback.
- A new versioned disclosure is required before the first persistent M4
  provider sync. It does not authorize background collection or another
  account.

## Recommendation contract

Recommendation queries are cache-only and read one atomic candidate/evidence
snapshot. Retrieval and ranking remain separate operations.

Every hard constraint returns `pass`, `fail`, or `unknown`. Any fail excludes a
candidate before scoring. Unknown handling is an explicit query policy and
remains visible even when a conditional candidate is included. A named query
override records the original and effective outcome; it never rewrites
evidence.

Recipes have immutable identifiers such as `resume/0.1`,
`finishability/0.1`, `preference-fit/0.1`, and `wishlist-fit/0.1`. They use
bounded integer components and deterministic tie-breaking. Results return all
component inputs, evidence identifiers, positive and negative factors,
tradeoffs, exclusions, unknowns, total score, and recipe version. Confidence
and completeness are separate from fit score.

Deal value remains the accepted M3 `deal-evidence/0.1` dimension. M4 never
folds deal value into preference fit or treats missing prices as a play
eligibility failure.

AUR-642 is implemented with `sync reviews --scope wishlist` and
`recommendations wishlist` under recipe `wishlist-fit/0.1`. Default review
syncs converge in bounded batches, explicit limits refresh a deterministic
prefix, retryable provider failures stop fanout with a persisted cooldown, and
nonretryable per-AppID failures do not block later subjects. The query reuses
the accepted M3 deal normalization rather than recomputing price meaning.
Direct rating, snooze, play-state, trait, and rule lineage remain field-specific.
Review aggregates and their typed manual reference are report-only. No release
or compatibility provider is activated.

## Acceptance harness

Normal CI must cover:

- migration, account isolation, idempotent feedback changes, clearing, snooze
  boundaries, and deletion;
- strict provider schemas, valid empty activity, privacy/no-stats achievement
  states, hidden achievements, request failures, and last-good promotion;
- bounded achievement scheduling and truthful unevaluated candidates;
- pass/fail/unknown gates, explicit overrides, integer recipe arithmetic,
  stable ties, input-order invariance, and every positive/negative factor;
- stale and missing evidence, no eligible candidates, direct-feedback changes,
  wishlist/deal separation, redaction, table safety, migrations, and installed
  package behavior.

Opt-in live acceptance records only coarse aggregates and states. Personal
titles, AppIDs, feedback, SteamID64, keys, and raw bodies are not acceptance
artifacts. Live achievement privacy or unsupported responses are acceptable
when represented truthfully.

The cross-milestone eval strategy separately tests common user questions. M4
acceptance requires deterministic recipe/oracle scenarios; natural-language
model judges, real-user prompts, and M3 prose evaluation remain non-blocking.
