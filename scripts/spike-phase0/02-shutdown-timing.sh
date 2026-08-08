#!/usr/bin/env bash
# Spike line 5: `steam -shutdown` + process-tree exit detection, N cycles.
# Threshold: every cycle detects full exit in < 60s; restart succeeds.
# Interrupts the running client — run only in an approved window.
. "$(dirname "$0")/lib.sh"
CYCLES="${CYCLES:-5}"

for i in $(seq 1 "$CYCLES"); do
  if ! steam_proc_tree_alive; then
    steam_start ; sleep 45
  fi
  steam_stop
  elapsed=$(wait_steam_dead 60) \
    && evidence 02-shutdown "cycle-$i-exit" pass "full tree exit in ${elapsed}s" \
    || { evidence 02-shutdown "cycle-$i-exit" fail "tree still alive after 60s: $(pgrep -ax steam; pgrep -ax steamwebhelper)"; exit 1; }
  steam_start
  for t in $(seq 1 90); do pgrep -x steam >/dev/null && break; sleep 1; done
  pgrep -x steam >/dev/null \
    && evidence 02-shutdown "cycle-$i-restart" pass "client back in ${t}s" \
    || { evidence 02-shutdown "cycle-$i-restart" fail "client not back after 90s"; exit 1; }
  sleep 20
done
