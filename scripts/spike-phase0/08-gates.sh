#!/usr/bin/env bash
# Lease-gate probes (session-model validation): logind idle, game/RemotePlay
# process detection. Read-only; run while idle AND while a game runs to get
# both polarities on record.
. "$(dirname "$0")/lib.sh"

SESSION=$(loginctl list-sessions --no-legend | awk '$4=="seat0"{print $1; exit}')
evidence 08-gates idle-hint info "$(loginctl show-session "$SESSION" -p IdleHint -p IdleSinceHint 2>&1)"
evidence 08-gates steam-children info "$(pgrep -x steam >/dev/null && ps --ppid "$(pgrep -x steam | head -1)" -o comm= | sort -u | tr '\n' ' ' || echo client-not-running)"
evidence 08-gates game-running info "$(pgrep -f 'reaper SteamLaunch' >/dev/null && echo yes || echo no) (reaper wrapper = launched game on linux)"
# -f, not -x: "streaming_client" exceeds pgrep's 15-char comm-name limit.
evidence 08-gates remote-play info "$(pgrep -f '[s]treaming_client' >/dev/null && echo yes || echo no)"
evidence 08-gates downloading info "$(ls "$MAIN_LIB/steamapps/downloading" 2>/dev/null | head -5 || echo empty)"
