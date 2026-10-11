# Agent host support and remaining qualification

Status: working support and research record, verified 2026-10-11 UTC. Decisions
are accepted only where linked to an ADR; automated account mutation remains
unimplemented. Exact commands belong to the [CLI contract](cli-contract.md).

## Product shape

Use one package with two explicit entry points: `steam-agent` for evidence and
planning, `steam-agent-broker` for host-local execution. Agents can inspect
`steam-agent capabilities --all` for the command catalog and broker
`capabilities` for local provisioning and policy. No additional daemon, MCP
server, authentication service or transport is required. Invoke host-local
commands using an existing SSH connection; `--machine` labels cached evidence
and does not contact that host.

Account API workflows can start without the Steam desktop app or local Steam
files using [explicit SteamID64 setup](../user-guide.md#add-an-account). The
configured identity is a user assertion; provider keys and accessible data are
still required for refreshes, and setup does not attest a Steam login.

| Capability | Implementation | Qualification limit |
| --- | --- | --- |
| Owned games | Documented GetOwnedGames adapter and cache | Visible API inventory is not a complete license ledger; privacy remains distinct from empty |
| Wishlist | Existing provisional first-party wishlist adapter and cache | Official-hosted endpoint is incompletely documented; retain support/failure distinctions |
| Installed games | Local manifest scanner with last-good projection | Complete scans promote; partial/failed scans preserve prior complete state |
| Runtime observation | AppID environment attribution on Linux/Windows | Only readable, positively attributed processes; never proof of playability |
| Launch | Policy/allowlist, durable dispatch intent, Linux/Windows session adapters | Requires a usable same-user desktop session; game-action qualification remains separate |
| Stop game | Separate stop/force policy, fresh process identity checks | Manual launches can be observed; games without attribution cannot be stopped |
| Install/update/verify | Existing Linux SteamCMD content plane, explicit alias-to-login mapping | Windows content execution is deferred; no automatic platform/depot guess for Proton |
| Wishlist edits | Baseline-bound inert add/remove/reprioritize proposals | Human change and separate refresh; no write API or mutation implementation |
| Purchase assistance | Cached price/ownership/compatibility decision cards | Human checkout; license/edition/DLC/checkout total unknown |

The storage refactor keeps its public facade and moves pure value helpers into
`storage_values.py`. An injected aware clock controls retention and default
read time without changing explicit evidence timestamps. This is a bounded
refactor; it does not claim the entire storage/CLI surface has been decomposed.

## Platform acceptance checks

Deterministic tests cover both OS adapters and temporary file locks. Native
read-only observations should verify imports, lock contention, the installed
scanner, same-user/session attribution and unknown handling. A dependable
execution release additionally needs a chosen test title on each host:

1. Start from Steam manually, observe the AppID, request graceful stop, and
   confirm process exit without attributing shared client/Wine infrastructure.
2. Launch under an allow grant and repeat the same request ID: exactly one
   dispatch. Revoke permission during startup and prove no dispatch.
3. Exercise unavailable desktop/client, inaccessible process evidence, a
   windowless Windows process and timeout without force escalation.
4. On Linux, qualify install/verify and interrupted content recovery per title,
   including native versus Windows depots for Proton. Keep unsupported titles
   deferred rather than guessing the platform.
5. Qualify Windows content adoption, client shutdown/restart and interrupted
   recovery before lifting its content gate.

No production game actions or credentials are needed for the deterministic
suite. Live host checks must retain only coarse, redacted results.

## Native host observations

Read-only qualification on the Linux host found 69 installed manifests in one
library with no malformed/inaccessible manifest findings. Runtime observation
remained unknown because five same-user processes denied environment access;
there were no positive game AppIDs. Portable lock contention/reacquisition and
39 focused tests passed under Python 3.14.

On the Windows host, scanning found 167 manifests in two libraries with one
inaccessible-library warning. The SSH session was service session 0, so runtime
and client gates correctly remained unknown. Portable locks and the same 39
tests passed. No Windows game action was attempted while the owner was using
that host. An experimental one-shot desktop bridge has deterministic tests;
the subsequent native read-only qualification is recorded below.

On 2026-10-11, Steam-free native fixtures on Haste confirmed owned Windows job
completion waits for descendants, whole-job termination, and exclusion of the
Steam API environment variable. A noisy owned native job also confirmed bounded
head/tail capture and separate stdout/stderr without raw disk spools. Disposable
local NTFS fixtures confirmed stable
128-bit file identities, Unicode manifest adoption/recovery, and refusal of
multiply-linked files, reparse points and reparse ancestors. These fixtures did
not touch Steam or the owner's development processes. They qualify primitives,
not authenticated content execution or power-loss durability. The job primitive
uses documented Windows 10+ [job-list process creation](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
and [kill-on-close limits](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject).

A read-only desktop trial exposed and repaired an ACL flag overflow: pywin32
311's [SetFileSecurity wrapper](https://github.com/mhammond/pywin32/blob/b311/win32/src/win32security.i)
parses the protected-DACL DWORD through a signed C long. Native unsigned input
raised OverflowError; the equivalent signed bit representation succeeded.
Private jobs retain the same exact owner-user and SYSTEM permissions. A second
trial reached task registration but reported no worker receipt. Elevated SSH
scratch parents granted a logon SID unavailable to the non-elevated desktop
token; exact-user/SYSTEM provisioning of owned scratch resolved that fixture
prerequisite. A subsequent completed job exposed local wall time mislabeled as
UTC by the COM adapter. Conservative local-time conversion repaired collection.

Read-only dispatch and fresh exact-task receipt collection/cleanup then passed
on Haste using Python 3.14.7, pywin32 311 and psutil 7.2.2, from an isolated
staged package and a properly provisioned non-elevated interactive owner token.
The tested desktop module SHA-256 was
`14d887def8d4ec79147052ba8315f7f41e4696fe9a18f72a389291ec7dd04da6`.
This qualifies this read-only route, not launch/stop, logout races, arbitrary
titles or authenticated content. A failed original job without a receipt remains
retained; no token/elevation checks were relaxed. Package/interpreter parents
must be readable by the interactive user; dispatch does not rewrite arbitrary
parent permissions.

Native initialization of a disposable broker state directory also passed. It
protected only newly created ancestors and known broker files with exact
owner-user/SYSTEM permissions; an existing multiply-linked broker configuration
was refused before any parent or state permission change. The existing client,
development repository and global Python environment were not modified.

An owned hidden-window trial exposed a Windows attribution bug: psutil returned
`STEAMAPPID`, which the observer had filtered using Linux's exact-case rule.
Windows-only canonicalization now recognizes the three accepted variable names
case-insensitively and refuses conflicting aliases. Linux exact-case behavior
and all other environment exclusions remain unchanged.

Four Steam-free stop cases then passed under the same non-elevated interactive
owner token: graceful close of two hidden fixture windows; permission revocation
after the first close post; ignored-close timeout; and windowless refusal. None
requested force. The Python virtual-environment redirector required private
actual-child PID/creation-time reporting, stable descendant proof and a retained
native process handle; cleanup used only that exact child handle and the
original launcher handle. A fixed fixture window class excluded runtime helper
windows. This qualifies attributed native HWND mechanics, not arbitrary games,
saves, inaccessible global scans or a real Windows title. The script SHA-256 was
`858b3f16313713ea136b1d4ed160158d2ee7cf54748201f038fccb13fb2fac65`;
the runtime module SHA-256 was
`21febf85a52913dcdf4a2c2d0a05dccaa9b8233cc0cc402799ae7b8727dd07d1`.
The separate qualification task was proven owned and idle, then removed.

The final bounded-capture native rerun also passed after setup/close failure
repairs. The capture module SHA-256 was
`32c259a8af1de919d95d4a0136e1f45ffb69c93a671c8bfafdd5443617414094`;
the content-plane module SHA-256 was
`a7f4e2a57a79b6a0cbd76dfa68a30e9575f7f230b31b4209b843c4c591bfcab9`.
Separate deterministic tests exercise first/second reader-thread startup
failure, writer-close failure, lingering writers and a real adapter SIGKILL.
They establish refusal and output bounds, not native Steam content completion.

The Windows content candidate now composes owned job execution, NTFS-bound
ownership/effect markers and versioned adoption. It requires explicitly private
SteamCMD installation/cache and pre-provisioned directories. It is unreachable
through released Windows content dispatch and directly refuses execution or
reconciliation before POSIX filesystem/client effects. Remaining gates include
private-cache ACL/authentication isolation, safe target provisioning/collision
handling, content lease/download observations, a prepared title, interrupted
install/verify/restoration and installed-projection verification. No Windows
content support claim or power-loss durability claim follows from the fixtures.
The owner confirmed that no private SteamCMD cache is provisioned on Haste.
Authentication of that separate cache and an idle prepared-title test remain
external prerequisites; the existing Steam client login is not substituted.

The authorized Linux launch/stop check chose Super Hexagon (AppID 221640),
a small installed title. After the owner signed into Steam, a native XTEST
adapter with explicit KDE consent successfully entered offline mode, confirmed
by Steam's visible status. The earlier virtual-mouse adapter could hover the
correct menu item but failed to select it reliably.

The first broker launch reached the owner's existing profile chooser. Repeating
the same request ID returned the same operation without another dispatch. The
chooser exposed missing mouse selection, misleading disconnected-controller
status, missing X11 process metadata, and fullscreen stacking that obscured
desktop consent. Those launcher issues were repaired separately from this CLI;
65 launcher tests passed. An owner-installed, separately scoped XTEST helper
requires fresh PID/creation-time/UID/class evidence, actual input focus, pointer
readback and click-target ancestry. It expires and never claims UI success;
22 fake-backend tests passed. Separate popup windows without the required
identity/ancestry evidence are refused. KDE's
[persistent allowance](https://raw.githubusercontent.com/KDE/kwin/Plasma/6.7/src/plugins/eis/eisprompter.cpp)
is keyed by an advertised application name: other legacy X11 applications
claiming that name are included, rather than just the helper or interpreter
path. The helper's own identity gates remain separate.

The pending chooser was gracefully stopped through the broker without force.
Its original PID/creation-time identity exited, but the receipt correctly
remained terminal `unconfirmed` because unrelated inaccessible processes made
the completion scan incomplete. A deliberate new request loaded the repaired
chooser; its existing default profile reached Super Hexagon's actual main menu
while Steam was offline. Repeating that launch request reused its receipt.
No level was played. The game stop requested graceful termination for three
positive AppID identities; all three original PID/creation-time identities
independently exited and the game window disappeared. The broker again
correctly retained `unconfirmed` because the global completion scan remained
incomplete. Steam was restored online, confirmed by the Go Offline menu item
and absence of its offline status. No cloud conflict or sign-in dialog appeared.
Six discovered game-specific files were compared: none were added or removed,
five were unchanged, and only Steam Cloud synchronization metadata changed.
The locations were not exhaustive, so overall progress preservation remains
unverified. A fresh system-Python helper connection accepted distinct native
pointer motion without another permission prompt. A read-only configuration
check confirmed the named persistent `python3` allowance was saved and the
global prompt bypass was disabled. The checkbox click was not observed, and
permission after logging out was not tested.
Temporary test state, environments, screenshots and input processes were
removed; the scoped helper and launcher repairs remain installed.

[Valve's offline setup guidance](https://help.steampowered.com/en/faqs/view/0E18-319B-E34B-B2C8)
requires a prepared title with no outstanding first-run/update requirements.
Offline qualification must not quietly launch online to prepare a title while
another host is in use. Confirm client offline mode before launch, keep the
test at the main menu, record bounded save-file comparisons where available,
and restore online afterward. Leave any
[Steam Cloud conflict](https://help.steampowered.com/en/faqs/view/68D2-35AB-09A9-7678)
for the owner; do not automatically choose a save to overwrite.

Partial runtime observations may target only fresh positive AppID identities;
completion remains unknown when the subsequent scan is incomplete. A graceful
termination request cannot establish that a game saved progress.

## Research decisions

[GetOwnedGames](https://partner.steamgames.com/doc/webapi/IPlayerService#GetOwnedGames)
remains the supported visible-owned source. The live first-party
[API method inventory](https://api.steampowered.com/ISteamWebAPIUtil/GetSupportedAPIList/v1/)
lists wishlist reads; it does not establish a general consumer wishlist-write
or checkout API. Steamworks methods initialized inside a publisher's game
context are not a general personal-account administration interface.

[Valve's Linux runtime guidance](https://github.com/ValveSoftware/steam-runtime/blob/master/README.md)
provides client install/launch handoffs, not a transactional completion API.
No general documented consumer game-stop API was found; this implementation
uses explicit OS process evidence and termination requests instead.

[Aurelia](https://github.com/Drackrath/Aurelia) is a useful disposable comparison
candidate for launcher/depot/cloud behavior, not an accepted replacement.
Its [launch usage](https://github.com/Drackrath/Aurelia/wiki/Usage-Launching)
documents tracking/stopping its own launches. Evaluate without a real account
first; do not borrow credential sessions, direct-file mutation or success
claims without an independent contract and recovery review.

Wishlist editing and checkout should be evaluated separately from host control.
Investigate supported consumer interfaces and permission before an ADR; a
human cart/store handoff already satisfies purchase assistance without storing
browser cookies, passwords or payment credentials. Publisher microtransactions
are not consumer store checkout. Technical feasibility alone does not settle
permission under the [Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/)
and [Web API terms](https://steamcommunity.com/dev/apiterms).
