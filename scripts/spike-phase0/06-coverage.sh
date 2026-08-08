#!/usr/bin/env bash
# Spike line 3: what fraction of the owned library can steamcmd serve?
# Non-destructive: ONE steamcmd session prints app_info for every appid
# (per-appid logins would take ~30x longer). Real installs are sampled by 04.
# Output: coverage.tsv (appid <tab> classification) + summary evidence.
# Usage: 06-coverage.sh <account> [appid-file]   (default: installed manifests)
. "$(dirname "$0")/lib.sh"
ACCOUNT="${1:?}"
APPID_FILE="${2:-}"

if [ -z "$APPID_FILE" ]; then
  APPID_FILE="$SPIKE_DIR/appids.txt"
  ls "$MAIN_LIB"/steamapps/appmanifest_*.acf \
    | grep -oP 'appmanifest_\K[0-9]+' > "$APPID_FILE"
fi

args=()
while read -r appid; do args+=(+app_info_print "$appid"); done < "$APPID_FILE"

timeout 900 env HOME="$STEAMCMD_HOME" "$STEAMCMD_ROOT/steamcmd.sh" +@NoPromptForPassword 1 +login "$ACCOUNT" +app_info_update 1 \
  "${args[@]}" +quit </dev/null > "$SPIKE_DIR/appinfo-all.log" 2>&1 || true

python3 - "$SPIKE_DIR/appinfo-all.log" "$APPID_FILE" "$SPIKE_DIR/coverage.tsv" <<'EOF'
import re, sys
log, ids_f, out = sys.argv[1:4]
text = open(log, errors="replace").read()
ids = [line.strip() for line in open(ids_f) if line.strip()]
# app_info_print emits a KeyValues block starting with "<appid>" at column 0.
starts = {m.group(1): m.start() for m in re.finditer(r'^"(\d+)"$', text, re.M)}
bounds = sorted(starts.values()) + [len(text)]
rows, ok = [], 0
for appid in ids:
    if appid not in starts:
        rows.append((appid, "no-app-info")); continue
    seg = text[starts[appid]:bounds[bounds.index(starts[appid]) + 1]]
    oslists = " ".join(re.findall(r'"oslist"\s+"([^"]*)"', seg))
    if "linux" in oslists:
        cls = "linux-depot"
    elif "windows" in oslists or '"depots"' in seg:
        cls = "windows-depot-proton"   # servable via @sSteamCmdForcePlatformType windows
    else:
        cls = "no-depot-info"
    if cls != "no-depot-info": ok += 1
    rows.append((appid, cls))
with open(out, "w") as f:
    f.writelines(f"{a}\t{c}\n" for a, c in rows)
print(f"{ok}/{len(ids)}")
EOF
summary=$(python3 -c "
rows=[l.split('\t') for l in open('$SPIKE_DIR/coverage.tsv')]
ok=sum(1 for r in rows if r[1].strip()=='linux-depot-visible')
print(f'{ok}/{len(rows)}')")
evidence 06-coverage summary info "$summary appids show a steamcmd-visible linux depot; table in coverage.tsv. Gate: >=90% for steamcmd-as-primary, else per-app capability records carry the gap."
