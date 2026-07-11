# Steam account data lifecycle

Status: M2 policy boundary; implementation and live validation in progress

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

### AUR-627 owned-library synchronization

AUR-627 may persist only normalized fields required for the visible-owned query,
such as AppID, display metadata deliberately requested from the endpoint,
reported playtime, the target profile, retrieval time, request flags, provider,
support level, and evidence relationships. The exact fields remain subject to
live contract fixtures and the AUR-627 schema review.

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
2. **Delete one profile's Steam Data:** delete that profile's normalized owned
   observations, evidence links, sync/probe history, profile metadata, and
   credential reference without deleting unrelated profiles or M1 machine
   observations.
3. **Delete all Web API Steam Data:** delete every account/provider record,
   probe record, credential reference, and locally stored key managed by Steam
   Agent. This is the local termination path required before ceasing API use.
4. **Delete the complete local store:** document deletion of the application
   data directory for users who also want to remove M1 machine data and every
   other local record.

A candidate surface is `auth remove`, `profiles delete --delete-steam-data`,
and `data delete --provider steam-web-api --all`. These spellings are proposed,
not an implemented contract; the behavior above is the gate.

The current AUR-620 checkpoint implements logical removal of one account alias,
its probe rows, and the locally managed credential. It is not the all-provider
termination path above and makes no physical-erasure claim for SQLite pages.
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

Data export, if added, is a separate explicit action. Deletion must not create
an export or diagnostic copy as a side effect.

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
