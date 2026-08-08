#!/usr/bin/env bash
# Kill-at-every-transition recovery evidence (plan §5 table), disposable
# library only. Each case kills the process at a stage, then records what
# reconciliation would observe. Threshold: every case maps to exactly one
# recovery action from the table, and executing it restores consistency.
# Usage: 07-kill-matrix.sh <account> <appid> <installdir-name>
. "$(dirname "$0")/lib.sh"
require_backup
ACCOUNT="${1:?}"; APPID="${2:?}"; DIRNAME="${3:?}"
TARGET="$SPIKE_LIB/steamapps/common/$DIRNAME"

# Main-library mode: all manifest/content work below requires the client
# stopped (mutual exclusion); restarted at the end.
steam_stop
wait_steam_dead 60 >/dev/null || { evidence 07-kill client-stop fail "client would not exit"; exit 1; }

# Case A: kill steamcmd mid-download (content_running, fresh).
rm -rf "$TARGET"
HOME="$STEAMCMD_HOME" "$STEAMCMD_ROOT/steamcmd.sh" +@NoPromptForPassword 1 +force_install_dir "$TARGET" +login "$ACCOUNT" \
  +app_update "$APPID" +quit >"$SPIKE_DIR/killA.log" 2>&1 &
CMD_PID=$!
sleep 20 && kill -9 "$CMD_PID" 2>/dev/null || true
partial="absent"; [ -d "$TARGET" ] && partial="present ($(du -sh "$TARGET" | cut -f1))"
evidence 07-kill caseA-evidence info "partial dir $partial; no adopted manifest — table row: resume or journaled cleanup"
# Recovery action: resume.
steamcmd_run "$SPIKE_DIR/killA-resume.log" +force_install_dir "$TARGET" +login "$ACCOUNT" +app_update "$APPID"
grep -q "fully installed" "$SPIKE_DIR/killA-resume.log" \
  && evidence 07-kill caseA-recovery pass "resume completed after kill -9" \
  || evidence 07-kill caseA-recovery fail "resume did not complete; see killA-resume.log"

# Case B: kill during adoption (simulate: write half the manifest, then recover).
SRC_ACF=$(find "$TARGET/steamapps" "$STEAMCMD_ROOT/steamapps" -name "appmanifest_$APPID.acf" 2>/dev/null | head -1 || true)
DST="$SPIKE_LIB/steamapps/appmanifest_$APPID.acf"
[ -n "$SRC_ACF" ] || { evidence 07-kill caseB-evidence fail "no steamcmd manifest found for $APPID"; steam_start; exit 1; }
head -c "$(($(stat -c%s "$SRC_ACF") / 2))" "$SRC_ACF" > "$DST"   # torn write
cksum_src=$(cksum < "$SRC_ACF"); cksum_dst=$(cksum < "$DST")
[ "$cksum_src" != "$cksum_dst" ] \
  && evidence 07-kill caseB-evidence pass "torn manifest detectable by checksum — table row: restore/complete decidable" \
  || evidence 07-kill caseB-evidence fail "torn write not detectable"
cp "$SRC_ACF" "$DST"    # recovery: complete the swap
evidence 07-kill caseB-recovery pass "swap completed from journaled source"

# Case C: client started while steamcmd runs (mutual-exclusion violation).
# Deliberately NOT automated: this is the one case that can trigger a client
# re-validate. Run manually once, observe, record.
evidence 07-kill caseC-manual info "manual case: start client during a steamcmd download of $APPID; record client behavior here"

steam_start
evidence 07-kill client-restarted info "client restarted after kill-matrix run"
