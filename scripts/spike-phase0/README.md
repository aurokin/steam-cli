# Phase 0 spike — herb.home.arpa

Acceptance evidence for [ADR 0027](../../docs/adr/0027-provisioned-execution.md)
per the [execution plan](../../docs/design/execution-plan.md) and
[Linux session model](../../docs/design/execution-linux-session-model.md).
Target: herb (CachyOS, KDE Wayland, native Steam, single library at
`~/.local/share/Steam`, 68 installed titles as of 2026-08-07). Herb drives a
CRT and must never be made headless — nothing here touches the display stack.

Scripts run ON herb as `auro` (copy the directory over, or run via
`ssh auro@herb.home.arpa`). Everything writes evidence JSONL to
`~/workspace/spike-phase0/evidence/`; the disposable library and the vendored
steamcmd live under `~/workspace/spike-phase0/` and never touch the main
library except where a script says so explicitly.

## Order and thresholds

| Step | Script | Interrupts client? | Owner present? | Pass threshold |
| --- | --- | --- | --- | --- |
| recon | `00-recon.sh` | no | no | informational |
| setup + backup | `01-setup.sh` | no | one manual UI step after (register disposable library in Steam Settings → Storage) | backup exists; steamcmd anonymous login ok |
| shutdown timing | `02-shutdown-timing.sh` | yes ×5 | no | 5/5 cycles: full tree exit < 60 s, restart < 90 s |
| Guard seed | `03-guard-login.sh <account>` | no | **yes** (password + Guard) | interactive login ok; re-run with `check` over following weeks to measure token longevity (spike line 4) |
| install + adoption | `04-install-adoption.sh <account> 1902490 "Desk Job"` (repeat with 2–3 small titles) | yes (one stop/start) | no | every title: steamcmd "fully installed", single-file adoption, client restart shows StateFlags=4 with NO re-download. Any failure = ladder rung 2 fails → stop and reassess |
| uninstall | `05-uninstall.sh` (≥10 install/uninstall cycles across titles) | yes | no | all cycles: content + manifest gone, client forgets the app. `app_uninstall` is the mechanism under test — flaky history; failure ships move/uninstall as `confirmed_with_residue`-style fallbacks |
| coverage | `06-coverage.sh <account>` | no | no | report the number; ≥ 90 % linux-depot-visible for steamcmd-as-primary, else per-app capability records carry the gap |
| kill matrix | `07-kill-matrix.sh` | yes | case C manual | every case maps to exactly one recovery action from the plan §5 table and that action restores consistency |
| gates | `08-gates.sh` (run idle AND mid-game) | no | mid-game run, yes | IdleHint tracks reality; game/RemotePlay/downloading detection correct in both polarities |

Not scripted (measured over time / by hand): update-after-adoption coherence
(wait for a real update to a spiked title, or use a beta-branch toggle on a
disposable install), reboot-mid-download recovery (run `04`, pull power,
re-run — evidence lands in the same JSONL), group-writable-library ACL test
(needs the `steam-broker` user to exist; comes with the identity bootstrap,
validate item 1 of the session model).

## Results so far (evidence in `results-herb/`)

2026-08-07 — 00/01/08/02 run remotely: metadata backup taken (12K), vendored
steamcmd bootstraps and anonymous-login OK, and shutdown timing PASSED 5/5
far under threshold (full tree exit 3–4 s, restart detected in 2 s; restart
detection = process spawn, not client readiness). Two findings folded back:
`streaming_client` must be matched with `pgrep -f` (15-char comm limit made
the Remote Play gate silently useless), and steamcmd leaked logs into
`~/.local/share/Steam/logs` via `~/.steam` links — root isolation requires a
separate HOME (the broker identity provides this; noted in the session
model). Remaining: 03 (owner), library registration (owner), then 04–07 and
the mid-game 08 run.

2026-08-07 (later) — offline library registration FAILED twice: the client
strips script-added `libraryfolders.vdf` stanzas on start, with or without
`contentid` + in-library `libraryfolder.vdf` marker. Finding: library
registration is UI-only on the 2026 client. Owner-approved pivot: adoption /
uninstall / kill tests run in the MAIN library (`SPIKE_LIB` defaults to it)
with tiny free titles; the rail softens to "existing titles untouched".
Product implication is benign — the product path installs into existing
libraries and never needed agent-created ones.

2026-08-08 — full sequence completed (run 3; runs 1–2 died to tooling bugs
and the shared-HOME credential clobber, both now fixed and documented).
Results: **adoption PASS 4/4 cycles** (Spacewar ×3 + Desk Job 4.2G:
steamcmd download → single-manifest adoption → client indexes at
StateFlags=4, zero re-download; steamcmd writes its manifest under the
force_install_dir target, so locating it is deterministic).
**app_uninstall FAIL 4/4** — silent no-op with valid auth; content and
manifests untouched. Uninstall needs a different mechanism (decision
pending). **Kill matrix PASS** (resume after kill -9; torn-manifest
checksum detection + swap recovery). **Coverage: 70/70 servable (gate PASS at
100 %)** — Proton-aware re-read of the same app_info data: 34 native Linux
depots, 36 Windows-depot/Proton (servable via
`@sSteamCmdForcePlatformType windows`; a sampled Proton install remains to
confirm the override path). Table: `results-herb/coverage-v2.tsv`. Auth findings: private
STEAMCMD_HOME is mandatory (client clobbers a shared config.vdf); fail-fast
auth with runner abort prevents failed-login cascades. Residual state:
Desk Job (4.2G) and Spacewar remain installed and client-adopted — benign
free titles; removal awaits an approved uninstall mechanism. Client left
running.

## Safety rails

- `01-setup.sh` takes a full metadata backup first; destructive scripts
  refuse to run without one (`require_backup`).
- Main-library mode (owner-approved): scripts touch only the spiked free
  titles' manifests and directories; existing titles are never written.
- Steam Guard: `03` is the only interactive step; everything after uses the
  cached token and fails fast to an `auth_required` evidence line, never a
  retry loop.

## Lifecycle

Temporary Phase 0 tooling: kept while the token-longevity clock and the
remaining manual spike lines run; removed once the Phase 1 executor
supersedes it. Not part of the product surface.
