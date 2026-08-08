#!/usr/bin/env bash
# Detached Phase 0 sequence (main-library mode, free titles only).
# Launch on herb:  systemd-run --user --collect --unit spike-runner \
#                    bash ~/workspace/spike-phase0/scripts/runner.sh REDACTED
# Progress: evidence JSONL + $SPIKE_DIR/runner.log; DONE marker on completion.
set -uo pipefail
DIR="$(dirname "$0")"
. "$DIR/lib.sh"
set +e   # lib.sh sets -e; the runner must survive individual step failures
ACCOUNT="${1:?usage: runner.sh <account>}"
LOG="$SPIKE_DIR/runner.log"
step() { echo "[$(date -u +%FT%TZ)] === $*" >> "$LOG"; }

rm -f "$SPIKE_DIR/RUNNER_DONE"
step "auth check"
bash "$DIR/03-guard-login.sh" "$ACCOUNT" check >> "$LOG" 2>&1
grep -q '"token-valid","result":"pass"' <(tail -1 "$EVIDENCE_DIR/03-guard-login.jsonl") \
  || { step "ABORT: auth_required — re-seed with 03-guard-login.sh, then relaunch"; touch "$SPIKE_DIR/RUNNER_DONE"; exit 1; }

# Cycle 1: Spacewar (tiny, fast).
step "04 Spacewar";  bash "$DIR/04-install-adoption.sh" "$ACCOUNT" 480 "Spacewar" >> "$LOG" 2>&1
step "05 Spacewar";  bash "$DIR/05-uninstall.sh"        "$ACCOUNT" 480 "Spacewar" >> "$LOG" 2>&1
# Cycle 2: Aperture Desk Job (realistic size).
step "04 Desk Job";  bash "$DIR/04-install-adoption.sh" "$ACCOUNT" 1902490 "Desk Job" >> "$LOG" 2>&1
step "07 kill-matrix Desk Job"; bash "$DIR/07-kill-matrix.sh" "$ACCOUNT" 1902490 "Desk Job" >> "$LOG" 2>&1
step "05 Desk Job";  bash "$DIR/05-uninstall.sh"        "$ACCOUNT" 1902490 "Desk Job" >> "$LOG" 2>&1
# Cycles 3-4: Spacewar repeats for the uninstall reliability count.
for n in 3 4; do
  step "04 Spacewar c$n"; bash "$DIR/04-install-adoption.sh" "$ACCOUNT" 480 "Spacewar" >> "$LOG" 2>&1
  step "05 Spacewar c$n"; bash "$DIR/05-uninstall.sh"        "$ACCOUNT" 480 "Spacewar" >> "$LOG" 2>&1
done
step "06 coverage"; bash "$DIR/06-coverage.sh" "$ACCOUNT" >> "$LOG" 2>&1

# Leave the machine as found: client running.
pgrep -x steam >/dev/null || steam_start
step "done"
touch "$SPIKE_DIR/RUNNER_DONE"
