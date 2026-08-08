# Linux session model for broker execution (Item 0)

Status: accepted with [ADR 0027](../adr/0027-provisioned-execution.md)
(Phase 0 spike passed on the target machine, 2026-08-07/08) and re-scoped to
the single-identity model by
[ADR 0028](../adr/0028-trusted-manager-execution.md).

Target machine: the always-on gaming PC (CachyOS, KDE Wayland on seat0,
native Steam + ES-DE driving a CRT — the machine must never be made
headless, which this design respects by construction: the client plane
requires the graphical session; nothing disables it). Recon 2026-08-07
confirmed: logind `IdleHint` reports correctly on this Wayland stack; a
single library under the desktop user's `~/.local/share/Steam`;
`steamapps/` is desktop-user-owned mode 755; steamcmd is vendored from
Valve's tarball into an isolated root. Spike tooling:
[`scripts/spike-phase0/`](../../scripts/spike-phase0/README.md).

## Identity and ownership (single identity, ADR 0028)

The broker runs as the desktop user and is driven by the trusted manager
agent over SSH as that same user. There is no dedicated broker OS user, no
restricted agent SSH user, and no broker socket.

| Concern | Where it lives |
| --- | --- |
| Steam client, config, game libraries | desktop user (unchanged) |
| Broker state: ledger, policy, adoption journal, logs | broker state dir (`~/.local/state/steam-broker`, mode 0700) |
| steamcmd root + credential cache | private steamcmd HOME under the state dir (`HOME` environment override) |

- The private steamcmd HOME is mandatory, not hygiene (Phase 0 finding,
  2026-08-08): under a shared HOME steamcmd stores its credential cache
  (`ConnectCache`) in the CLIENT's `config.vdf`, and the running client
  rewrites that file and clobbers the entry — observed as a cached token
  dying 3 seconds after a passing auth check. The spike's
  `HOME=<spike>/steamcmd-home` simulation is the permanent design.
- Phase 0 also showed an "isolated steamcmd root" is NOT isolated while
  sharing the desktop user's HOME — steamcmd followed `~/.steam` links and
  wrote logs under the client's own tree.

## Processes

- **Broker**: the `steam-agent-broker` CLI, invoked over SSH in the desktop
  user's session. Content-plane work (steamcmd) needs no display; client
  lifecycle (`steam -silent` via `systemd-run --user`, `steam -shutdown` +
  process-tree-exit wait) runs directly in the desktop user's session —
  there is no separate session-helper service (ADR 0028).
- Machine posture for unattended windows: autologin to the desktop user with
  the screen locked. When the graphical session is absent, client-plane
  gates fail closed while steamcmd-only work may proceed.

## Lease gates (Linux mechanics)

| Gate | Source of truth |
| --- | --- |
| no running game | process tree under the Steam client (children of `steam`), plus `steamapps/appmanifest` state cross-check |
| no Remote Play session | `streaming_client` process presence — match on full command line (`pgrep -f`), not comm name: the name exceeds the kernel's 15-char comm limit (found in Phase 0) |
| no in-flight client download | client running + `downloading/` activity; fails closed when client state unknown |
| idle threshold | logind `IdleHint`/`IdleSinceHint` via `loginctl`; validated on this Wayland stack |
| session state | logind session class/state for the desktop user |
| maintenance window | broker policy file (future scheduler work) |

All gates fail closed; `unknown` defers the operation.

Phase 0 finding (2026-08-07): the client silently strips offline-added
`libraryfolders.vdf` stanzas on startup (tested with and without `contentid`
and the in-library `libraryfolder.vdf` marker). Library registration is
UI-only. Product consequence: the executor installs into existing registered
libraries only; creating libraries stays a human UI action.

## Portability seams

The broker/ledger/lease logic is platform-free. Linux-specific code is
confined to gate probes (logind, process tree) and client lifecycle
(`systemd-run --user`, `-shutdown` wait). macOS (launchd, no logind) and
Windows (interactive-session download constraint, registry state, service
model) each get their own session-model document behind their own gate.
