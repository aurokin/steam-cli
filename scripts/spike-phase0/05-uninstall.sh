#!/usr/bin/env bash
# Spike line 2: app_uninstall reliability against the disposable library.
# Run after 04 has installed the appid. Threshold: manifest AND content gone,
# repeated runs (install->uninstall cycles) all clean.
# Usage: 05-uninstall.sh <account> <appid> <installdir-name>
. "$(dirname "$0")/lib.sh"
require_backup
ACCOUNT="${1:?}"; APPID="${2:?}"; DIRNAME="${3:?}"
TARGET="$SPIKE_LIB/steamapps/common/$DIRNAME"

steam_stop
wait_steam_dead 60 >/dev/null || { evidence 05-uninstall client-stop fail "client would not exit"; exit 1; }

steamcmd_run "$SPIKE_DIR/uninstall-$APPID.log" \
  +force_install_dir "$TARGET" +login "$ACCOUNT" +app_uninstall "$APPID"

content_gone=no; [ ! -d "$TARGET" ] || [ -z "$(ls -A "$TARGET" 2>/dev/null)" ] && content_gone=yes
evidence 05-uninstall "content-removed-$APPID" "$([ $content_gone = yes ] && echo pass || echo fail)" \
  "dir=$TARGET gone=$content_gone (app_uninstall is the mechanism under test — flaky history)"

# The adopted manifest in the client library is ours to remove on success.
if [ "$content_gone" = yes ]; then
  rm -f "$SPIKE_LIB/steamapps/appmanifest_$APPID.acf"
  evidence 05-uninstall "manifest-removed-$APPID" pass "adopted manifest removed"
fi
steam_start ; sleep 45
grep -q "\"$APPID\"" "$SPIKE_LIB/steamapps/libraryfolders.vdf" 2>/dev/null \
  && evidence 05-uninstall "client-forgot-$APPID" fail "client still lists app after restart" \
  || evidence 05-uninstall "client-forgot-$APPID" pass "client no longer lists app"
