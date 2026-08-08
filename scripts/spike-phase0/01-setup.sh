#!/usr/bin/env bash
# One-time setup: metadata backup, vendored steamcmd, disposable library dir.
# MANUAL STEP AFTER THIS SCRIPT: in the Steam client UI (Settings -> Storage),
# add $SPIKE_DIR/library as a library folder. The spike never edits
# libraryfolders.vdf itself.
. "$(dirname "$0")/lib.sh"

# 1. Full metadata backup (manifests + config), before anything else.
backup="$SPIKE_DIR/backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
tar -czf "$backup" -C "$MAIN_LIB" \
  steamapps/libraryfolders.vdf config/libraryfolders.vdf \
  $(cd "$MAIN_LIB" && ls steamapps/appmanifest_*.acf) 2>/dev/null
evidence 01-setup metadata-backup pass "$backup ($(du -h "$backup" | cut -f1))"

# 2. Vendored steamcmd from Valve's tarball (no AUR dependency, isolated root).
if [ ! -x "$STEAMCMD_ROOT/steamcmd.sh" ]; then
  mkdir -p "$STEAMCMD_ROOT"
  curl -fsSL "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" \
    | tar -xz -C "$STEAMCMD_ROOT"
fi
steamcmd_run "$SPIKE_DIR/steamcmd-selftest.log" +login anonymous \
  && evidence 01-setup steamcmd-bootstrap pass "anonymous login ok" \
  || evidence 01-setup steamcmd-bootstrap fail "see steamcmd-selftest.log"

# 3. Disposable library directory (registered manually via client UI).
mkdir -p "$DISPOSABLE_LIB"
evidence 01-setup disposable-library info "$DISPOSABLE_LIB created; register it in Steam UI now"
