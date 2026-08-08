# Linux session model for provisioned execution (Item 0)

Status: draft for [ADR 0027](../adr/0027-provisioned-execution.md); every
"validate" note below is a Phase 0 line item. Nothing here is accepted until
the spike passes on the target machine.

Target machine: **herb.home.arpa** (CachyOS, KDE Wayland on seat0, native
Steam + ES-DE driving a CRT — the machine must never be made headless, which
this design respects by construction: the session helper requires the
graphical session; nothing disables it). Recon 2026-08-07 confirmed: logind
`IdleHint` reports correctly on this Wayland stack; single library at
`~/.local/share/Steam` (68 installed apps, 669 GB free); `steamapps/` is
`auro`-owned mode 755 (no group write — the shared-group ACL is a real
change, validate item 1); steamcmd not installed (the spike vendors Valve's
tarball into an isolated root). Spike tooling:
[`scripts/spike-phase0/`](../../scripts/spike-phase0/README.md).

## Identities and ownership

| Identity | Kind | Owns | Must never hold |
| --- | --- | --- | --- |
| desktop user (existing) | interactive login, autologin enabled | Steam client + config, game libraries, session helper unit | broker state, agent SSH key |
| `steam-broker` | system user, no login shell | `/var/lib/steam-broker/` (ledger, policy, adoption journal, steamcmd root + credential profile, Discord bot token), broker socket | interactive session, desktop user's keyring |
| `steam-agent-remote` | SSH-only restricted user | nothing; group membership granting connect access to the broker socket | library write, broker state read, sudo |

- Game libraries: owned by the desktop user; `steam-broker` gains write via a
  shared group + setgid library directories (validate: Steam tolerates group
  ownership on `steamapps/`; it historically does, as it only requires its
  own user writability).
- The Hermes agent's SSH key goes only in `steam-agent-remote`'s
  `authorized_keys`, with `restrict` options limiting it to the CLI
  entrypoint. The desktop user's SSH surface is unchanged.
- The broker socket (`/run/steam-broker/broker.sock`) is group-restricted to
  `steam-agent-remote` plus the desktop user. All requests are structured
  plan documents; the socket carries no shell.

## Processes

- **Broker**: systemd system service (`steam-broker.service`), runs as
  `steam-broker`. Executes all content-plane work (steamcmd needs no
  display). Holds the outbound Discord gateway connection for
  confirmations — no inbound ports. Phase 0 finding (2026-08-07): an
  "isolated steamcmd root" is NOT isolated while sharing the desktop user's
  HOME — steamcmd followed `~/.steam` links and wrote logs under
  `~/.local/share/Steam/logs`. Escalated 2026-08-08: under a shared HOME
  steamcmd stores its credential cache (`ConnectCache`) in the CLIENT's
  `config.vdf`, and the running client rewrites that file and clobbers the
  entry — observed as a cached token dying 3 seconds after a passing auth
  check. A private HOME for steamcmd is mandatory, not hygiene. The
  `steam-broker` identity provides this inherently; the spike simulates it
  with `HOME=$SPIKE_DIR/steamcmd-home`.
- **Session helper**: systemd *user* service in the desktop user's graphical
  session (`steam-session-helper.service`, `WantedBy=graphical-session.target`).
  The only component that starts/stops the Steam client and dispatches
  launches, because the client requires the graphical session. It accepts
  commands solely from the broker socket, performs only: `steam -silent`
  start, `steam -shutdown` + process-tree-exit wait, `-applaunch` dispatch,
  and client/process state reports. It holds no policy and makes no
  decisions.
- Machine posture for unattended windows: autologin to the desktop user with
  the screen locked. Validate: client download behavior and session-helper
  lifecycle across lock, logout (graphical-session stops — broker must treat
  "no session" as a failed lease gate for client-plane work, while
  steamcmd-only work may proceed), and reboot.

## Lease gates (Linux mechanics)

| Gate | Source of truth |
| --- | --- |
| no running game | process tree under the Steam client (children of `steam`), plus `steamapps/appmanifest` state cross-check |
| no Remote Play session | `streaming_client` process presence — match on full command line (`pgrep -f`), not comm name: the name exceeds the kernel's 15-char comm limit (found in Phase 0) |
| no in-flight client download | client running + `downloading/` activity; fails closed when client state unknown |
| idle threshold | logind `IdleHint`/`IdleSinceHint` via `loginctl`; validate on the machine's Wayland/X11 stack — if unreliable, fall back to input-device activity timing |
| session state | logind session class/state for the desktop user |
| maintenance window | broker policy file |

All gates fail closed; `unknown` defers the operation.

Phase 0 finding (2026-08-07): the client silently strips offline-added
`libraryfolders.vdf` stanzas on startup (tested with and without `contentid`
and the in-library `libraryfolder.vdf` marker). Library registration is
UI-only. Product consequence: the executor installs into existing registered
libraries only; creating libraries stays a human UI action.

## Validate in Phase 0 (summary)

1. Group-writable library accepted by client and steamcmd.
2. Session helper survives lock; behavior on logout/login; broker correctly
   downgrades to content-plane-only when the graphical session is absent.
3. `steam -shutdown` + full process-tree exit detection (including
   `steamwebhelper` children) is reliable; measure worst-case exit time.
4. logind idle reporting on this desktop stack.
5. Restricted SSH entrypoint: `steam-agent-remote` can submit and query plans
   and nothing else.
6. Broker Discord gateway reconnect behavior across network loss.

## Portability seams

The broker/ledger/lease logic is platform-free. Linux-specific code is
confined to: identity/ACL bootstrap, the session helper unit, gate probes
(logind, process tree), and socket paths. macOS (launchd, no logind) and
Windows (interactive-session download constraint, registry state, service
model) each get their own session-model document behind their own gate.
