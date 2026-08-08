#!/usr/bin/env bash
# Read-only machine facts. Safe to run any time.
. "$(dirname "$0")/lib.sh"

evidence 00-recon os info "$(grep PRETTY /etc/os-release; uname -r)"
evidence 00-recon session info "$(loginctl show-session "$(loginctl list-sessions --no-legend | awk '$4=="seat0"{print $1; exit}')" -p Type -p State -p IdleHint 2>&1)"
evidence 00-recon steam-running info "$(pgrep -x steam >/dev/null && echo yes || echo no)"
evidence 00-recon libraries info "$(grep -E '"path"' "$MAIN_LIB/steamapps/libraryfolders.vdf" 2>/dev/null || echo none)"
evidence 00-recon installed-count info "$(ls "$MAIN_LIB"/steamapps/appmanifest_*.acf 2>/dev/null | wc -l)"
evidence 00-recon library-perms info "$(ls -ld "$MAIN_LIB/steamapps")"
evidence 00-recon disk info "$(df -h "$MAIN_LIB" | tail -1)"
