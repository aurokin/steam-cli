# Steam account data lifecycle

Status: M2 policy boundary; capability evidence collected, persistence
implementation in progress

This document governs Steam account data obtained through Valve's Web API. It
does not change the accepted M1 local installed-library contract. It is the
privacy and retention gate for AUR-620 and for the later persistent
owned-library slice in AUR-627.

## Terms basis

The primary source is Valve's
[Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms), verified
on 2026-07-11. Valve labels that page "Last updated July 2010" and reserves the
right to change the terms, API, and compatibility. Reverify the page before an
account-data milestone is accepted and periodically before a release.

The terms require, among other things, that an application:

- retrieves an end user's Steam Data only when that user requests it;
- informs the user what Steam Data is stored and the country or countries in
  which it is stored;
- keeps its Web API key confidential and does not intercept or store a Steam
  password;
- presents Steam Data on an as-is basis with the applicable warranty and
  liability disclaimers;
- stays within the published limit of 100,000 Web API calls per day; and
- deletes copies of Steam Data when terminating use of the Web API.

The adapter enforces a persisted, cross-process minimum interval of one second
between user-key requests. The budget lives in the platform-default local data
store and is shared across every Steam Agent `--data-dir` profile for that OS
user. That local ceiling is below 100,000 requests per day, but it cannot
account for other applications or devices using the same key; callers remain
responsible for provider-wide usage.

This is an engineering policy, not legal advice. A public distribution must
publish an appropriate privacy notice at its canonical application location;
this repository document records the product behavior that notice must match.

## Retrieval and consent boundary

Steam Agent retrieves account data only in response to an explicit account
probe or synchronization requested by the local user or an agent acting for
that user. Listing local configuration or capabilities must not make a network
request. There is no implicit background sync, telemetry upload, or hosted
Steam Agent service.

Configuring a profile, storing a key, making a Steam profile public, or finding
public information does not authorize retrieval of friends, household members,
or unrelated accounts. Each target profile and capability is a separate
request. M2 is read-only and must not change Steam privacy settings or any other
account state.

Steam passwords, browser cookies, session tokens, and publisher credentials are
outside the core M2 trust mode. Steam Agent uses a user Web API key for the
documented account capability. The key:

- is provided through an approved secret input and storage backend, never an
  argument;
- is sent only to the fixed Valve HTTPS API origin in the `x-webapi-key`
  header, never in a URL query;
- is never written to SQLite, response JSON, logs, fixtures, diagnostics,
  exception text, or retained HTTP request/response material; and
- is never transmitted to a Steam Agent server or shared with another user.

The user who registers the key remains responsible for it under Valve's terms
and can revoke it through Steam. Removing a local credential does not revoke it
at Valve; both actions must be explained distinctly.

## Storage location and countries

Account data is local-only. Normalized records live in the same platform-native
Steam Agent data directory described in the README unless the user selects a
different directory. The project does not select, infer, or transmit a storage
country: the data is stored in the country or countries where the user's
device, selected data directory, filesystem replicas, and user-controlled
backups reside. The CLI must disclose this before the first persistent account
sync. A user who cannot accept those locations must not enable persistence.

Steam Agent does not control cloud backup, disk snapshots, filesystem
replication, or copies made by other tools. Deletion output must remind the user
that those copies follow the other tool's retention policy.

## Data retained by stage

### AUR-620 capability probe

An account capability probe may retain only the minimum redacted operational
metadata needed to explain capability state:

- a user-chosen local profile identifier and the target SteamID64;
- credential backend and an opaque credential reference, never the key;
- provider, method/capability, requested non-secret flags, and target profile;
- probe time, coarse result classification, and retry/freshness metadata; and
- a schema or fixture-shape version used to interpret the result.

The HTTP response body, response headers that may contain sensitive values,
profile/game payload, and raw authentication error are processed in memory and
discarded. They are not stored as raw evidence, cache entries, debug files, or
content hashes. Test fixtures must be synthetic or manually redacted and must
never be generated by blindly committing a live response.

Live probes on 2026-07-11 established three redacted outcomes without retaining
response bodies, headers, keys, Steam identifiers, or game data:

- the configured primary account and key produced `ready`;
- a deliberately invalid key produced `authentication_failed`; and
- a syntactically valid but nonexistent SteamID64 produced
  `data_inaccessible`.

The last result validates the inaccessible/ambiguous classification only. It is
not evidence that a profile is private, and the CLI must not relabel that result
as a privacy diagnosis. The provider can use the same observable boundary for
an unknown identity, visibility restrictions, or other behavior it does not
document precisely.

### AUR-627 owned-library synchronization

AUR-627 may persist only the following normalized fields for the current
last-known-good visible-owned projection:

- the local immutable account row ID and Steam AppID;
- total reported playtime in minutes, preserving missing separately from zero;
- an inclusion basis of `default_owned_set` or `played_free_only`;
- the sync/evidence relationship, provider, documented support level, retrieval
  time, and the two requested `include_played_free_games` flag values; and
- coarse sync status, counts, timestamps, and typed failure metadata that
  contains no provider response text.

The owned request does not retain names, icons, last-played time, platform-
specific playtime, or other additive fields. Names and application types come
from independently sourced catalog or accepted local evidence. Adding another
owned field requires an active use case and a lifecycle review rather than
silently retaining every field Valve adds.

The sync obtains a default set with `include_played_free_games=false` and an
expanded set with it set to `true`. An AppID present only in the valid expanded
set is classified `played_free_only`; an AppID in the default set is classified
`default_owned_set`. The default set must be a subset of the expanded set before
promotion. Neither value proves purchase method, current price, license kind,
or `free=false`, and the expanded set still omits unplayed free entitlements.
If either request fails or the pair is inconsistent, the run does not promote.

M2 retains one current last-known-good per-game projection and coarse sync-run
history. A successful promotion removes superseded per-game account
observations and their account-scoped evidence. Failed, inaccessible, partial,
or malformed attempts retain the previous projection but do not retain game
payloads. Longitudinal playtime history is outside M2 and requires a separate
purpose and retention decision.

Raw Steam account response bodies are not retained by default. Adding raw-body
retention, hosted storage, analytics, or another account-data purpose requires
a new threat/terms review and an explicit update to this policy and the decision
register before implementation.

Installed-library observations from M1 are local machine evidence and are not
proof of account ownership. They remain independently deletable from account
data.

## Visibility and accuracy limits

All Steam Web API results are presented as provided, as available, and without
a guarantee of accuracy or completeness. In particular:

- `GetOwnedGames` returns games only when owned-game/game-detail data is visible
  to the requester;
- individually private games may be omitted even when general game details are
  public;
- an inaccessible or ambiguous provider response is not a confirmed empty
  library and must not be labeled as one;
- `include_played_free_games=true` includes free games the user has played; it
  does not establish every free license or every never-played free-to-play game;
- the API key owner and the queried SteamID64 are separate identities; and
- a previously successful result can become stale after privacy, ownership, or
  provider behavior changes.

Capability and query contracts must preserve ready, authentication failure,
inaccessible/ambiguous, empty, partial, stale, provider failure, and unsupported
states wherever the provider evidence supports the distinction. They must not
invent a more specific privacy diagnosis from an undocumented response shape.

## Deletion and revocation

Before persistent account observations are accepted, the CLI must provide and
test user-facing operations with these semantics (exact command spelling is
owned by the CLI contract):

1. **Remove credential:** delete the selected local key from its credential
   backend and its opaque reference. Do not imply that Valve revoked the key.
2. **Delete one profile's Steam Data:** delete that target profile's normalized
   owned observations, evidence links, sync/probe history, and profile metadata
   without deleting unrelated profiles or M1 machine observations. The Steam
   Web API key is shared by the local data profile and independent of the
   queried SteamID64, so this operation does not delete the shared key or its
   credential reference.
3. **Delete all Web API Steam Data:** delete every account/provider record,
   probe record, credential reference, and locally stored key managed by Steam
   Agent. This is the local termination path required before ceasing API use.
4. **Delete the complete local store:** document deletion of the application
   data directory for users who also want to remove M1 machine data and every
   other local record.

A candidate surface is `auth remove`, `profiles delete --delete-steam-data`,
and `data delete --provider steam-web-api --all`. These spellings are proposed,
not an implemented contract; the behavior above is the gate.

The current AUR-620 checkpoint has separate commands for logical removal of one
account alias (with its probe rows) and for removal of the locally managed
credential. Neither is the all-provider termination path above, and neither
makes a physical-erasure claim for SQLite pages.
Therefore AUR-620 remains in progress and AUR-627 may not persist owned-game
observations. Physical-erasure/rebuild behavior and all-account deletion are
acceptance gates for that persistent slice, not properties of the current
checkpoint.

Deletion must be transactional and idempotent, reject ambiguous profile
targets, and report what categories were removed without echoing deleted data.
Tests must cover multiple profiles, a missing or locked credential backend,
interrupted deletion, SQLite residual-page handling, and preservation of
out-of-scope machine data. Where SQLite cannot guarantee physical erasure from
the existing file, the implementation must use an appropriate secure-delete or
database-rebuild strategy and state its limits. User-controlled backups and
snapshots require separate deletion by the user.

The account-data schema must use an immutable account-row foreign key for owned
observations, current projections, sync subjects, and evidence subjects. A
SteamID hidden only inside JSON is not an acceptable deletion index. Shared
credential metadata remains scoped to the local data profile, not to a queried
account. The all-Steam-data path removes every Steam account subject before it
removes that shared credential reference and locally managed key.

SQLite deletion provides bounded best effort, not a promise of forensic
erasure. Sensitive account tables use `secure_delete`, and a successful delete
compacts or rebuilds the database while preserving out-of-scope M1 and other
provider data. The command reports logical deletion and compaction/rebuild
outcomes separately. Flash translation layers, filesystem journals, snapshots,
cloud backups, and prior copies remain outside the application's control. A
locked credential store, interrupted rebuild, or failed key deletion produces
an incomplete typed result and must never be reported as successful
termination.

Data export, if added, is a separate explicit action. Deletion must not create
an export or diagnostic copy as a side effect.

## First persistent sync disclosure

Before the first owned sync for a local data profile, the CLI must stop with a
typed disclosure-required result until the user explicitly acknowledges the
versioned policy. The disclosure states:

- the exact normalized fields and current-projection retention rule above;
- that the response is Valve-provided as-is and visible-owned is not complete
  license truth;
- the individually-private and played-free limitations;
- the selected local data directory and that storage countries follow the
  device, user-selected filesystem, replicas, and backups rather than a country
  selected by Steam Agent;
- how per-profile deletion differs from all-Steam-data termination and Valve
  key revocation; and
- that physical deletion cannot cover user-controlled backups, snapshots, or
  storage-media remapping.

Acknowledgment stores only the policy version and timestamp. It is not consent
for background synchronization, another account, another Steam capability, or
hosted processing. A material field, purpose, retention, or storage-location
change requires a new policy version and acknowledgment.

## Milestone gates

### AUR-620 may close only when

- the account/credential capability contract is implemented and redacted;
- probes retain no raw response body and make no request merely to list status;
- key confidentiality and request-only behavior have adversarial tests;
- live or safely redacted evidence validates the provider's ready,
  authentication-failure, and inaccessible/ambiguous behavior; and
- user-facing disclosure matches this document.

For local development, this repository policy is the disclosure source. Public
binary/package distribution remains blocked until a canonical privacy and
as-is notice is published at the application domain and matches this policy.

### AUR-627 may persist owned data only when

- AUR-620 is accepted;
- the normalized stored-field set and last-good promotion behavior are reviewed;
- per-profile and all-account deletion operations above are implemented and
  tested;
- the first-sync disclosure identifies local fields and user-controlled storage
  countries/backup implications; and
- as-is, visibility, individually-private, empty, stale, and played-free-game
  limitations appear in the stable agent contract and user documentation.

Until those gates pass, live owned responses may be used only for an explicit,
redacted, memory-only capability probe. They must not be written to the durable
evidence store.
