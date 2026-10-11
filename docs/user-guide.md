# User guide

Status: maintained guide to the current M1–M7 and core-agent command surface.

For the short assistant workflow, read cached membership with an explicit
AppID, refresh missing evidence through an explicit credential source, and use
the broker's original request ID for action recovery. Wishlist changes and
purchase assistance are inert human handoffs. Exact syntax and evidence are
canonical in the [core agent contract](design/cli-contract.md#core-agent-queries-and-explicit-acquisition).

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
projection. A run is partial only when the scan could not see or trust
everything; a manifest correctly excluded from the projection — a paused
download, a leftover uninstalled entry — is reported as a warning on a complete
run and does not block promotion. The [CLI contract](design/cli-contract.md)
has the exact rule.

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

For a machine without the Steam desktop app, configure your SteamID64 directly:

```text
steam-agent accounts configure --steam-id64 STEAMID64 --alias primary
steam-agent accounts status --alias primary
```

This validates and stores the identifier locally without reading Steam files,
resolving credentials, or contacting a provider. It does not verify that the
account exists, authenticate a Steam login, or prove API access. Subsequent API
refreshes require the provider key and accessible account data. The
[account contract](design/cli-contract.md#implemented-m2-capability-gate-commands)
defines validation, provenance and identifier redaction.

On a gaming host, local discovery is an optional convenience and redacts
account names and identifiers by default:

```text
steam-agent accounts discover
steam-agent accounts configure --from-local-most-recent --alias primary
steam-agent accounts status --alias primary
```

If discovery is ambiguous, rerun it with `--include-identifiers`, then configure
the desired identity with `accounts configure --steam-id64 ID --alias primary`.

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
is safe to uninstall, backed up, current, or downloadable on time. Read
`reclaim_bytes` as what an uninstall frees, not what the title occupies: a
Proton prefix, shader cache, and subscribed Workshop content survive it. A
candidate measured to leave such content behind carries a `residual_content`
gate, and `operations plan uninstall APPID` reports how much. Plans are
inert: they return official references and instructions but never open Steam,
launch a process, change a file, or claim completion.

## Run or stop a game on a gaming host

Use `steam-agent-broker` on the target host, as its desktop user. The planner
continues to answer library questions and emit plans. Windows and Linux client
adapters are implemented; Windows install/repair is still gated. Review the
[host support record](design/agent-host-support.md) for qualification limits.
A cached `--machine` alias does not connect to another computer.

Provision a client-only broker and inspect its deny-all policy:

```text
steam-agent-broker init --library PATH --machine-id gaming-host --account primary
steam-agent-broker capabilities
steam-agent-broker policy
```

Edit the private broker `policy.toml` to grant the intended actions. Launch and
stop have separate AppID allowlists. Force stopping additionally needs
`[stop] allow_force = true` and `--force`; it never follows a timeout silently.
Policy syntax and exact outcomes are in the
[CLI contract](design/cli-contract.md#implemented-broker-execution-commands).

```text
steam-agent-broker runtime --appid 480
steam-agent-broker launch 480 --request-id evening-launch-001
steam-agent-broker stop 480 --request-id evening-stop-001
steam-agent-broker inspect OPERATION_ID
steam-agent-broker status
```

Under `allow`, a direct command authorizes and runs through the normal broker
gates. Under `confirm`, it returns a nonce and waits for approval:

```text
steam-agent-broker confirm NONCE --actor owner
steam-agent-broker run --operation-id OPERATION_ID
```

Retry the same request ID after a lost response; it returns the existing
operation and never replays a terminal action. Use a new ID for a deliberate
new attempt. `--request-only` prepares a request without running it. Legacy
planner JSON intake remains available through `request --account ALIAS`.

A launch receipt of `dispatched` says the client accepted the request. Runtime
observation can separately report an attributed OS process; neither proves the
game is playable. Stop works for manually Steam-started games when their
processes expose readable AppID evidence. Partial observations can target only positively attributed processes and
retain unknown completion. Unknown or ambiguous evidence is not permission to
guess a process name. Graceful stop cannot prove that a
game saved progress. A stop timeout after dispatch is terminal `unconfirmed`;
read the detail before choosing a new attempt.

Linux content install/update/verify additionally requires `--steamcmd PATH`
and explicit `--account ALIAS --steam-login LOGIN` provisioning. An alias is
not a Steam login. Verify can replace mods installed over official game files;
Steam Guard and first-run setup remain human interactions. Client-only launch
and stop need no SteamCMD credential cache.

If interrupted, run `reconcile` before another attempt. Durable dispatch intent
is never replayed; content recovery still follows its existing adoption journal.
Never delete broker state while an operation is active or recovery is pending.

### Interrupted-operation recovery

Inspect `status`, then run `reconcile` on the same host and broker state.
Reconciliation repairs existing durable work; it does not create a new request
or automatically resume a download. The current recovery behavior is:

| Durable state | Recovery behavior |
| --- | --- |
| `pending_confirmation`, `authorized` | No execution effects yet; preserve the request subject to its authorization expiry |
| `dispatch_pending` | Record terminal `unconfirmed`; the client may have received the request, so never replay it |
| `lease_acquired`, `client_stopping` | Restore the recorded client state, then abort; do not continue straight into content work |
| `content_running` | Defer while SteamCMD may still be running. Otherwise, a valid window becomes `interrupted` for an explicit same-operation resume; an expired window restores the client and fails |
| `interrupted` | Remain resumable within the window; resume rechecks current policy and lease gates. An expired window restores the client and fails |
| `adopting` | Under safe lease gates, use the matching adoption journal to finish or restore the manifest; an unproven adoption fails after client restoration |
| `client_restart_pending`, `verifying` | Retire completed journal remnants, restore recorded client state, and determine the outcome from manifest evidence |

Failed restoration or unreadable recovery evidence leaves work nonterminal for
a later retry. Partial content is retained; recovery does not delete it or
silently download repair content after authorization expires. Manifest rollback
does not roll back game files already changed by an in-place update. Existing
journal recovery and client restoration remain available when policy denies
new content work.

## Network, persistence, and Steam effects

The table below covers `steam-agent`. The separate `steam-agent-broker`
executable does change Steam, under the authorization rules above.

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
5. If `steam-agent-broker` was provisioned, quiesce it before deleting
   anything: run `steam-agent-broker status`, let anything mid-run finish, and
   run `steam-agent-broker reconcile` if a row was interrupted. Rows merely
   awaiting confirmation or deferred for a busy session hold no unrecovered
   state and can simply lapse. The ledger and adoption journal are what
   recovery reads, so deleting them under an interrupted operation can strand
   content or leave the Steam client stopped. Then remove its state directory
   (`~/.local/state/steam-broker`, or the `--state-dir` / `STEAM_BROKER_STATE`
   location). It holds the policy file, the operation ledger, the adoption
   journal, logs, and steamcmd's private HOME with its cached Steam
   credentials; none of that lives in the Steam Agent data directory.
6. Remove user-controlled copies, backups, or snapshots separately if desired.

This reset affects Steam Agent, and the broker's state if you removed it. It does not revoke provider keys, delete
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
