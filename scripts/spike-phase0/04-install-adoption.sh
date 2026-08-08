#!/usr/bin/env bash
# Spike line 1: install into the disposable library via force_install_dir,
# adopt the single Valve-written manifest, restart client, verify adoption
# without re-download. Threshold: every tested title adopts cleanly; any
# failure fails the ladder rung.
# Usage: 04-install-adoption.sh <account> <appid> <installdir-name>
# Suggested free tiny titles: 1902490 "Aperture Desk Job", 480 "Spacewar".
. "$(dirname "$0")/lib.sh"
require_backup
ACCOUNT="${1:?}"; APPID="${2:?}"; DIRNAME="${3:?}"
TARGET="$SPIKE_LIB/steamapps/common/$DIRNAME"
mkdir -p "$SPIKE_LIB/steamapps/common"

# 1. Download with steamcmd (client may keep running; different library).
steamcmd_run "$SPIKE_DIR/install-$APPID.log" \
  +force_install_dir "$TARGET" +login "$ACCOUNT" +app_update "$APPID"
grep -q "fully installed" "$SPIKE_DIR/install-$APPID.log" \
  && evidence 04-adoption "download-$APPID" pass "steamcmd reports fully installed" \
  || { evidence 04-adoption "download-$APPID" fail "see install-$APPID.log"; exit 1; }

# 2. Locate the Valve-written manifest (steamcmd layout differs by version).
SRC_ACF=$(find "$TARGET/steamapps" "$STEAMCMD_ROOT/steamapps" -name "appmanifest_$APPID.acf" 2>/dev/null | head -1 || true)
[ -n "$SRC_ACF" ] \
  && evidence 04-adoption "manifest-located-$APPID" pass "$SRC_ACF" \
  || { evidence 04-adoption "manifest-located-$APPID" fail "no appmanifest written by steamcmd"; exit 1; }

# 3. Client-stopped adoption of exactly one file (installdir patched).
steam_stop
wait_steam_dead 60 >/dev/null || { evidence 04-adoption client-stop fail "client would not exit"; exit 1; }
python3 - "$SRC_ACF" "$SPIKE_LIB/steamapps/appmanifest_$APPID.acf" "$DIRNAME" <<'EOF'
import re, sys
src, dst, dirname = sys.argv[1:4]
text = open(src).read()
text = re.sub(r'("installdir"\s+")[^"]*(")', rf'\g<1>{dirname}\g<2>', text)
open(dst, "w").write(text)
EOF
evidence 04-adoption "manifest-adopted-$APPID" pass "single-file adoption done"

# 4. Restart, then watch for adoption vs re-download.
steam_start ; sleep 60
if ls "$SPIKE_LIB/steamapps/downloading/$APPID" >/dev/null 2>&1; then
  evidence 04-adoption "client-verdict-$APPID" fail "client is RE-DOWNLOADING: adoption not accepted"
else
  state=$(grep -oP '"StateFlags"\s+"\K[0-9]+' "$SPIKE_LIB/steamapps/appmanifest_$APPID.acf" 2>/dev/null || echo missing)
  [ "$state" = "4" ] \
    && evidence 04-adoption "client-verdict-$APPID" pass "StateFlags=4, no downloading dir: client adopted" \
    || evidence 04-adoption "client-verdict-$APPID" fail "StateFlags=$state after restart — inspect manually"
fi
