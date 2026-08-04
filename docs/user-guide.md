# User guide

Status: maintained guide to the current M1–M7 command surface.

This guide assumes `steam-agent` was installed from a source checkout with
`uv tool install .`. Contributors running inside the checkout should prefix the
same examples with `uv run`.

## First local result

Check local prerequisites without contacting a provider, then scan Steam's
local metadata:

```text
steam-agent doctor --offline
steam-agent sync installed --machine local
steam-agent games query --scope installed --machine local --format table
```

For a nonstandard installation, pass the Steam root—the directory containing
`steamapps/libraryfolders.vdf`—or set `STEAM_AGENT_STEAM_ROOT`:

```text
steam-agent sync installed --machine local --steam-root "/path/to/Steam"
```

The scanner reads Steam files and discovers additional libraries from
`libraryfolders.vdf`; it does not repair or modify them. A partial or failed
scan records its diagnostics but does not replace the last complete installed
projection.

## Output and local data

JSON is the default. Add `--format table` for a human summary. Automation must
inspect `completeness.status`: exit code 0 can still describe partial, stale, or
unavailable evidence.

Installed-game output omits filesystem paths by default. Use `--include-paths`
only when the result will remain private:

```text
steam-agent games query --scope installed --machine local --include-paths
```

The private database is named `steam-agent.sqlite3` under the platform data
directory:

| Platform | Default directory |
| --- | --- |
| macOS | `~/Library/Application Support/steam-agent/` |
| Windows | `%LOCALAPPDATA%\steam-agent\` |
| Linux | `$XDG_DATA_HOME/steam-agent/`, or `~/.local/share/steam-agent/` |

Override it with `--data-dir PATH` or `STEAM_AGENT_DATA_DIR`. Platform path
resolution is implemented on macOS, Windows, and Linux; continuous integration
currently exercises Ubuntu, so reports from the other platforms are valuable.

## Add an account

Local discovery redacts account names and identifiers by default:

```text
steam-agent accounts discover
steam-agent accounts configure --from-local-most-recent --alias primary
steam-agent accounts status --alias primary
```

If discovery is ambiguous, rerun it with `--include-identifiers`, then select a
listed identity with `accounts configure --steam-id64 ID --alias primary`.

Bring your own key from Valve's official
[Steam Web API key page](https://steamcommunity.com/dev/apikey) after reviewing
the [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).
Store it through the hidden interactive prompt. Keys are never accepted as
command arguments, and Steam passwords, cookies, Guard codes, and session
credentials are not supported:

```text
steam-agent auth set steam-web-api
steam-agent auth status steam-web-api
steam-agent owned probe --account primary
```

The default credential backend is the native OS store. On POSIX, the explicit
`--backend file --yes-file-risk` fallback is permission-protected but
unencrypted; there is no automatic downgrade.

The first persistent account sync requires a versioned local-storage
acknowledgment:

```text
steam-agent sync owned --account primary --acknowledge-local-storage
steam-agent sync catalog --account primary --machine local
steam-agent games query --scope library --account primary --machine local --format table
```

An owned capability probe is a network request but does not store a library.
`sync owned` is a separate network request and retains only its normalized,
documented fields. A private or inaccessible inventory remains unavailable; it
is not reported as an empty library.

## Common workflows

Synchronization commands acquire local or provider evidence and may write the
tool's database. Query, ranking, assessment, and planning commands below read
the cache only unless the [CLI contract](design/cli-contract.md) says otherwise.

### Choose a command by intent

This is the read-only, cache-only question map. It does not acquire evidence or
change Steam; use the setup and synchronization workflows elsewhere in this
guide when a result reports missing or stale evidence.

| Question | Command leaf |
| --- | --- |
| Find owned, installed, or wishlist games | `games query` |
| Filter candidates by declared multiplayer evidence | `discovery query` |
| Choose what to play next | `recommendations query` |
| Rank wishlist fit | `recommendations wishlist` |
| Ask whether a game will work on an explicit target | `compatibility assess` |
| Rank group fit or required copies | `group recommend` |
| Inspect group copies or hard eligibility for explicit AppIDs | `group ownership` or `group eligibility` |
| Rank candidates for reclaiming space or travel | `storage rank` |
| Rank cached wishlist deals | `deals query` |

`games query --scope library` joins visible-owned and installed games, while
`--scope wishlist` reports wishlist membership rather than ownership.
`discovery query --scope appids` requires one repeated `--appid` option per
candidate. Its multiplayer declarations are positive-only, three-valued cached
evidence: a matching declaration can pass a mode filter, while absence remains
unknown. Exact numeric player counts from providers are unsupported; do not
interpret a declared multiplayer mode as a minimum or maximum player count.

### Wishlist deals

```text
steam-agent sync wishlist --account primary --acknowledge-local-storage
steam-agent sync prices --scope wishlist --account primary --country US --provider auto
steam-agent deals query --scope wishlist --account primary --country US --format table
```

Country and store class are part of the evidence context. A missing price is
unknown, not free. The query returns provider attribution and fallback state;
it does not follow returned links. The accepted price/deal sync currently
supports only country `US` with USD evidence; other countries are rejected.

### Decide what to play

```text
steam-agent feedback rate --account primary APPID --value liked
steam-agent sync activity --account primary --acknowledge-local-storage
steam-agent recommendations query --account primary --recipe resume/0.1 --format table
steam-agent recommendations query --account primary --recipe finishability/0.1 --time-minutes 360 --unknown include
```

Feedback and preference rules are explicit local state, separate from inferred
activity. Recommendations use versioned deterministic recipes and preserve
hard-gate unknowns.

### Check compatibility

```text
steam-agent sync system --machine local --acknowledge-local-storage
steam-agent sync compatibility --scope library --account primary --machine local --country US --language english --acknowledge-local-storage
steam-agent compatibility assess APPID --account primary --target machine:local --country US --language english --explain
```

Assessments compare bounded declared evidence. They do not promise CPU/GPU
performance or frame rate, and returned manual references are not opened.

### Discover and choose for a group

```text
steam-agent sync app-facts --scope known --account primary --machine local --country US --language english --max-items 20 --acknowledge-local-storage
steam-agent discovery query --scope known --limit 100 --account primary --machine local --country US --language english
steam-agent profiles create synthetic:guest --acknowledge-group-storage --acknowledge-backups
steam-agent group recommend --scope known --limit 10 --member account:primary --member synthetic:guest --context-account primary --context-machine local --country US --language english --mode online_coop --objective min-copies
```

Discovery never expands beyond the authorized cached universe. Group output
uses request-local member ordinals and reports missing copies as a range when
ownership is unknown or stale.

### Inspect storage and build a safe plan

```text
steam-agent operations observe --machine local
steam-agent storage rank --recipe reclaim-space/0.1 --machine local --target-bytes 20000000000 --limit 10
steam-agent operations plan verify APPID --account primary --machine local
```

Storage ranking proves only the evidence it reports. It does not prove a game
is safe to uninstall, backed up, current, or downloadable on time. Plans are
inert: they return official references and instructions but never open Steam,
launch a process, change a file, or claim completion.

## Network, persistence, and Steam effects

| Command family | Network | Writes Steam Agent state | Changes Steam |
| --- | --- | --- | --- |
| `doctor --offline`, `status`, `capabilities`, query/assess/rank/plan, `operations observe` | No | No acquisition; database maintenance depends on the command contract | Never |
| `sync installed`, `sync system` | Local reads only | Writes normalized observations | Never |
| Provider `probe` and `sync` commands | Explicitly yes | Probes retain coarse state; syncs retain bounded normalized evidence | Never |
| `feedback`, `preferences`, synthetic group facts | No | Explicit local mutation | Never |
| `data delete`, `auth remove`, profile/fact deletion | No provider request | Deletes selected local state | Never |

`operations observe` reads only the promoted SQLite installed snapshot; it does
not scan Steam files. Exact exceptions and retention are indexed by the
[CLI contract](design/cli-contract.md) and
[Steam data lifecycle policy](design/steam-data-lifecycle.md).

## Scoped deletion and a full local reset

Normal deletion is explicit and scoped. These are representative examples, not
a complete list of retained capability state:

```text
steam-agent data delete --provider steam-web-api --account primary --yes
steam-agent data delete --provider gg-deals --all --yes
steam-agent data delete --provider local-system --machine local --yes
steam-agent auth remove steam-web-api --yes
steam-agent accounts remove --alias primary --yes
```

Removing account data does not revoke a key at Valve. Removing the database
file does not necessarily remove a native-keyring credential, and no local
deletion can erase user-controlled backups or snapshots. Remove credentials
with `auth remove` before manually discarding a complete data directory.

For a complete local reset:

1. Remove every configured credential with `auth remove` for
   `steam-web-api`, `isthereanydeal`, `steamgriddb`, and `gg-deals`. This clears
   provider-scoped native-keyring or fallback-file entries.
2. Stop all `steam-agent` processes.
3. Manually delete the selected Steam Agent data directory, including the
   SQLite database and any SQLite sidecar files. This discards installed
   observations, accounts, provider evidence, feedback/preferences, system
   profiles, synthetic profiles, and group facts in that profile.
4. If custom `--data-dir` profiles were used, also remove the platform-default
   Steam Agent directory after checking it for other profiles; the shared
   provider request budget is stored there.
5. Remove user-controlled copies, backups, or snapshots separately if desired.

This reset affects Steam Agent only. It does not revoke provider keys, delete
Steam account data at Valve, or change Steam client state.

## Troubleshooting

- **Steam root unavailable:** run `doctor --offline`; pass `--steam-root` if
  Steam is not in the platform default location.
- **Account discovery ambiguous:** opt in to `accounts discover
  --include-identifiers`, then configure one returned SteamID64.
- **Credential backend unavailable:** repair/unlock the native store or, on
  POSIX only, consciously select the protected-file fallback.
- **Acknowledgment required:** rerun the exact sync with the displayed
  `--acknowledge-...` option after reviewing its disclosure.
- **`NOT_SYNCED`, stale, partial, failed, running, or abandoned:** inspect the
  completeness and warning fields. Queries preserve those distinctions and may
  continue using a last-good projection.
- **Provider or locale failure:** retain the requested country/language and
  provider warning when reporting it; do not reinterpret missing evidence.
- **Database or migration error:** preserve the database and error output for
  diagnosis. Do not edit or delete migration files already applied.

Run `steam-agent COMMAND --help` for executable syntax. The
[CLI contract](design/cli-contract.md) defines stable machine semantics, while
the [evidence matrix](design/evidence-matrix.md) records provider limitations.
