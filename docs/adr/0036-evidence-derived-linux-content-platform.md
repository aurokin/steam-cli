# ADR 0036: Evidence-derived Linux content platform

Status: accepted 2026-11-20 by the maintainer under the owner's delegated
[decision authority](../../AGENTS.md#decision-authority). It is implemented and
live-qualified on one Linux host.

## Context

A Linux Steam library holds native installs and Windows-depot installs that
run through Proton. On Herb, 40 of 71 installed manifests recorded Windows
depots. The Linux content plane ran every `app_update` with SteamCMD's native
platform. For a Proton install, a `validate` pass on the wrong platform can
fetch a different depot set into the live directory, and adoption would then
rewrite the client's manifest to match it. The
[host support record](../design/agent-host-support.md) required keeping such
titles unsupported "rather than guessing the platform".

Steam's client manifest does not name a platform, but its `InstalledDepots`
does list depot IDs. The client's binary `appinfo.vdf` cache gives each depot's
`config.oslist`. Some DLC depots appear under the base app (tagged
`dlcappid`) and others only under the DLC's own entry. Phase 0 had already shown that SteamCMD serves Windows depots on
Linux under `@sSteamCmdForcePlatformType windows`.

## Decision

Linux install, update, and verify derive the SteamCMD platform from local
evidence and never from names, Proton prefixes, or compatibility-tool settings.
Native titles keep the unchanged SteamCMD command; Windows-depot titles add an
explicit `+@sSteamCmdForcePlatformType windows`. The rules below are checked at
preflight, at intake (refusing before intent is recorded, so an unqualified
title never holds the single active slot), at run time (deferring before any
client effect), and again after the client exits and before SteamCMD starts
(aborting before any content write if the selection changed).

**Evidence sources**

- The cache is the library's own `appinfo.vdf` when the library is a client
  root. Otherwise it is the cache of the one client whose `libraryfolders.vdf`
  lists the library: the `STEAM_AGENT_STEAM_ROOT` client when that is set, or
  else the one discovered installed client. Relative library entries resolve
  against that client's root. Another client's cache is never evidence, and a
  client root whose own cache is missing refuses.
- The installed depot set comes from the client manifest's `InstalledDepots`,
  read case-insensitively. Each depot needs a valid manifest ID. Without a
  client manifest, or under a queued one that records no depots, a SteamCMD
  manifest nested in the target counts only when it is newer than the client
  manifest and sits beside installed files (non-empty regular files outside
  `steamapps/`, links not followed).
- The cache, manifests, and markers are read without memory mapping and fail
  closed on anything malformed: unsupported formats, truncated, duplicate, or
  trailing data, wrong types, undecodable keys, and duplicate keys in any
  casing. A `public_only` placeholder record (no access token) counts as
  missing metadata, not as an app with no depots. In the cache,
  duplicate keys refuse within the sections the classifier reads (`common`,
  `depots`, `extended`); Valve's own data repeats keys elsewhere (for example
  install registry entries), and those keep the last value.

**Classifying an existing install**

- Each installed depot is classified by every description of it: its base
  entry, the DLC it names, and the definition its `depotfromapp` names (one
  hop only; a chain refuses). A depot the base app describes without a DLC tag
  is a base-game depot and its entry is authoritative; listed DLC are searched,
  and must be cached, only for a depot the base app does not describe.
  Conflicting descriptions refuse.
- `linux,windows` depots are shared. A depot one platform shares with macOS
  counts for that platform only alongside a depot exclusive to it.
- Only Linux depots selects native, and only Windows depots selects Windows.
  When every installed depot is shared, only the app's preferred platform is
  considered: native when its `common.oslist` lists Linux, otherwise Windows.
  It qualifies only when every base-game depot a default install of that
  platform would receive is installed, directly or as a `SharedDepots` entry.
  `optional`, unreleased (no public manifest), DLC, non-English language
  (SteamCMD's default language is English), other-realm (Steam China), Steam
  Deck, low-violence, and high-quality-audio depots are not part of a default
  global desktop install. Architecture-specific depots are judged per architecture:
  only one architecture's are installed, so every depot of at least one
  architecture the platform receives must be installed. The same check applies when only
  `SharedDepots` name the platform.
  A missing depot never moves the choice to the other platform, because
  absence has too many causes (beta branches, architectures, macOS installs)
  to infer a platform from.
- Every selection requires the app's `oslist` to list the chosen platform.
- A Windows selection also needs installed files (or staged bytes from a
  Windows run its owning marker still records), cached metadata for every
  DLC the app lists or the manifest names, and no `osarch` on any Windows or
  shared depot description of the app, its DLC, or its source apps.
  SteamCMD's bitness under the forced platform is unqualified.

**New installs and the broker's own partial downloads**

- A new install, or one with no recorded depots and nothing in its directory,
  is native only when the app's `oslist` lists Linux, the same rule its later
  updates face. A new Proton install is refused.
- The broker's inode-bound ownership marker records the platform and any
  manifest-named DLC of its run. Files that no manifest records, including
  staged downloads, are the broker's own partial download only when an owning
  marker vouches for them; the marker then supplies the platform, still
  subject to the Windows gate. A marker binds every later run on its
  directory, fresh or resumed, and every selection there must agree with it.
  A marker created by a run that provably wrote nothing retires: policy
  denied it before SteamCMD started, or SteamCMD reported `auth_required` in a
  directory that started empty and stayed empty. In a populated directory an
  in-place write cannot be ruled out, so the marker stays.
  Markers written before these fields came from native-only brokers.

**Results**

- Refusals carry closed `content_platform_<reason>` codes in JSON results.
- When a resumed operation defers on platform evidence, it restores the client
  it stopped and stays retryable.
- Executable classes, grants, adoption, recovery, and Windows content gating
  are unchanged.

## Alternatives and consequences

Refusing every non-native title would be safe, but it would leave verify
unavailable for most of a typical Proton library. Always running native would
keep the depot-swap risk. Choosing a platform from the presence of
`compatdata` or a Proton tool mapping would be a guess: prefixes outlive
platform changes, and the client config on Herb held no mapping.

The reader supports `appinfo.vdf` versions 28 and 29 and fails closed on any
other format. Every check reads the client's local cache; metadata Valve
changes after the client last refreshed (for example, a new
architecture-specific depot while the client stays offline) stays invisible
until it refreshes. Comparing SteamCMD's own current metadata before each run
would close that gap at the cost of a networked lookup per operation; it is
not done. A stale cache can still describe old depot restrictions; a
changed depot set then shows up as an unknown depot and defers. Reverting means
removing the override; operations run under it remain ordinary ledger rows.

Review record: 66 Diffwarden passes (Codex `gpt-6.2-sol`) and nine
five-lens Opus review rounds with reproduction-based verification. The last
Opus rounds confirmed no further defects. The final Diffwarden passes were
still finding about two issues each, almost all in corrupt or unusual on-disk
states (hand-edited caches, interrupted runs across renamed or linked
directories), and each was fixed with a test. More such cases likely remain.
Every one found so far failed closed or was made to: the residual risk is
over-refusal, plus the accepted cache-staleness gap above.

## Evidence

Synthetic v28/v29 caches and manifests cover native, Windows, shared,
mixed, ambiguous shared-only, architecture, both DLC layouts, missing-depot,
new and partial installs, hostile keys, and corrupt caches. They also cover
override argv, intake refusal before intent, and run-time deferral for fresh
and resumed rows.

On Herb on 2026-11-20 UTC, the final classifier selected a platform for 55 of
71 installed manifests in under 0.3 s: 27 native and 28 Windows. It refused 16:
ten for uncached or placeholder DLC metadata, three for architecture-specific
Windows depots, one whose app `oslist` omits the installed platform, one
shared-only install missing a base depot, and one queued Windows-only install.
On Herb's cache, 5 DLC depots were listed under
the base app and 4 only under their DLC. A single listed DLC is cached as an
integer rather than a string, which the reader accepts.

The override is per invocation. SteamCMD's private configuration on Herb held
no persisted `ForcePlatformType` afterwards, and the native verifies that ran
after the Proton verify in the same SteamCMD home restored a Linux depot file
hash-identically.

With the client offline, a Zuma Deluxe verify (AppID 3330, Windows depot 3331)
ran twice with the override and confirmed, the second time on the final build.
Both runs kept the same depot manifest ID and build, and the client loaded the
adopted manifest. An architecture-specific title was refused at intake without
a ledger row. This covers one small Proton title, not every Proton title,
32/64-bit selection, or a new Proton install.
