# Shared helpers for Phase 0 spike scripts (run ON herb as auro).
# Every script sources this and emits evidence JSONL to $SPIKE_DIR/evidence/.

set -euo pipefail

SPIKE_DIR="${SPIKE_DIR:-$HOME/workspace/spike-phase0}"
EVIDENCE_DIR="$SPIKE_DIR/evidence"
STEAMCMD_ROOT="$SPIKE_DIR/steamcmd"
# steamcmd MUST run under its own HOME: with the desktop user's HOME it
# resolves ~/.steam into the client's tree and stores its credential cache in
# the CLIENT's config.vdf, which the running client rewrites and clobbers
# (Phase 0 finding, 2026-08-08 — cached token died 3s after a passing check).
STEAMCMD_HOME="$SPIKE_DIR/steamcmd-home"
mkdir -p "$STEAMCMD_HOME"
DISPOSABLE_LIB="$SPIKE_DIR/library"
MAIN_LIB="$HOME/.local/share/Steam"
# Target library for install/uninstall/kill tests. Default was the disposable
# library; the 2026 client strips offline-registered libraries (Phase 0
# finding), so main-library mode (SPIKE_LIB="$MAIN_LIB") is the approved path:
# existing titles untouched, free tiny titles only.
SPIKE_LIB="${SPIKE_LIB:-$MAIN_LIB}"
mkdir -p "$EVIDENCE_DIR"

evidence() { # evidence <script> <check> <result:pass|fail|info> <detail>
  printf '{"ts":"%s","script":"%s","check":"%s","result":"%s","detail":%s}\n' \
    "$(date -u +%FT%TZ)" "$1" "$2" "$3" "$(printf '%s' "$4" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
    | tee -a "$EVIDENCE_DIR/$1.jsonl"
}

steamcmd_run() { # steamcmd_run <output-file> <args...>; rc 9 = auth_required
  local out="$1"; shift
  timeout 1800 env HOME="$STEAMCMD_HOME" "$STEAMCMD_ROOT/steamcmd.sh" +@NoPromptForPassword 1 "$@" +quit </dev/null 2>&1 | tee "$out" || true
  if grep -aqE "Cached credentials not found|Invalid Password|FAILED \(Rate Limit|need two-factor" "$out"; then
    evidence auth auth-state fail "auth_required: steamcmd could not log in (see $(basename "$out")). Fail fast, no retry."
    return 9
  fi
  grep -aq "to Steam Public.*OK" "$out" || return 1
}

steam_proc_tree_alive() {
  pgrep -x steam >/dev/null || pgrep -x steamwebhelper >/dev/null
}

wait_steam_dead() { # wait_steam_dead <timeout_s>; echoes elapsed or "timeout"
  local t=0
  while steam_proc_tree_alive; do
    sleep 1; t=$((t + 1))
    [ "$t" -ge "$1" ] && { echo timeout; return 1; }
  done
  echo "$t"
}

steam_start() {
  # From the desktop session a plain spawn works; from SSH there is no
  # display env, so route through the user manager (inherits the graphical
  # session environment on KDE) — the same mechanism the session helper uses.
  if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    (setsid steam -silent >/dev/null 2>&1 &)
  else
    XDG_RUNTIME_DIR="/run/user/$(id -u)" systemd-run --user --collect \
      --unit "spike-steam-$(date +%s)" steam -silent >/dev/null 2>&1
  fi
}

steam_stop() {
  XDG_RUNTIME_DIR="/run/user/$(id -u)" steam -shutdown >/dev/null 2>&1 || true
}

require_backup() {
  ls "$SPIKE_DIR"/backup-*.tar.gz >/dev/null 2>&1 \
    || { echo "FATAL: no metadata backup found; run 01-setup.sh first" >&2; exit 1; }
}
