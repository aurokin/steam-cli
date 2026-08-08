#!/usr/bin/env bash
# OWNER-PRESENT step: seed steamcmd's cached credentials (Steam Guard prompt).
# Usage: 03-guard-login.sh <steam_account_name>   (password/Guard interactive)
# Afterwards, 07-coverage.sh and 04-install-adoption.sh run unattended.
# Records the login date so token longevity (spike line 4) can be measured by
# re-running this script's check mode later: 03-guard-login.sh <account> check
. "$(dirname "$0")/lib.sh"
ACCOUNT="${1:?usage: 03-guard-login.sh <account> [check]}"

if [ "${2:-}" = "check" ]; then
  # Non-interactive: succeeds only if the cached token is still valid.
  # Output to file THEN grep: piping would let pipefail surface a timeout kill
  # as an auth failure even when the login line already succeeded.
  timeout 300 env HOME="$STEAMCMD_HOME" "$STEAMCMD_ROOT/steamcmd.sh" +@NoPromptForPassword 1 +login "$ACCOUNT" +quit </dev/null > "$SPIKE_DIR/auth-check.log" 2>&1 || true
  if grep -aq "to Steam Public.*OK" "$SPIKE_DIR/auth-check.log"; then
    evidence 03-guard-login token-valid pass "cached token still valid"
  else
    evidence 03-guard-login token-valid fail "re-auth required (see auth-check.log)"
  fi
  exit 0
fi

HOME="$STEAMCMD_HOME" "$STEAMCMD_ROOT/steamcmd.sh" +login "$ACCOUNT" +quit
evidence 03-guard-login seeded info "interactive login completed for $ACCOUNT; longevity clock starts now"
