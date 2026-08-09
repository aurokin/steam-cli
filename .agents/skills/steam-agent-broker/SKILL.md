---
name: steam-agent-broker
description: Install, update, repair, or launch owned Steam titles on a machine this checkout provisions, using the `steam-agent-broker` execution CLI. Use when the user asks to actually install, update, re-acquire, verify/repair, or start a game, to check what an execution attempt did, or to recover an interrupted operation. Do not use for library questions, rankings, or plans — those are read-only and belong to the `steam-agent` skill.
---

# Execute with Steam Agent Broker

`steam-agent-broker` is the only surface in this project that changes Steam.
It runs on the machine it manages, as the desktop user who owns that Steam
installation. `steam-agent` (the planner) never executes anything; the two
CLIs are separate on purpose.

Authority for this skill: [CLI contract](../../../docs/design/cli-contract.md)
for exact syntax and outcomes, [ADR 0027](../../../docs/adr/0027-provisioned-execution.md)
as re-scoped by [ADR 0028](../../../docs/adr/0028-trusted-manager-execution.md),
plus [ADR 0030](../../../docs/adr/0030-verify-as-a-second-executable-class.md)
for verify, [ADR 0031](../../../docs/adr/0031-launch-allowlist-dispatched-terminal.md)
for launch, and [ADR 0029](../../../docs/adr/0029-move-as-inert-plan.md) for
move — for why the boundaries are where they are.

## Before acting

1. Run `steam-agent-broker policy` to see the effective grants and limits, and
   `steam-agent-broker status` to see whether an operation is already active.
   One operation per machine runs at a time.
2. If the broker is not initialized, say so and stop. "Not initialized" can
   also mean the state directory is elsewhere: it defaults to
   `~/.local/state/steam-broker` and is overridden by `--state-dir PATH` or
   `STEAM_BROKER_STATE`. Confirm which before reporting the broker as
   unprovisioned. Provisioning it
   (`init --library ... --steamcmd ...`) is an owner decision, not a step to
   take on the user's behalf.
3. Three classes are executable, each granted independently in the policy
   file — holding one never implies another. Check `policy` output rather
   than assuming:
   - `install` — installs or updates the same title.
   - `verify` — Valve's validate pass, the repair capability. It replaces
     locally modified official files, so it removes mods installed over game
     content. Say that before running it on a title the user may have
     modded. It repairs an existing install and refuses a title that is not
     installed.
   - `launch` — asks the client to start one game. It additionally requires
     the AppID on the owner's launch allowlist (`launch_allowlist` in `policy` output, `[launch] allowed_appids` in the file); a refusal there is the owner's
     recorded intent, not an error to work around.
4. Uninstall and move are planned by `steam-agent` and finished by a human in
   Steam — that is a decision, not a gap, so offer the plan rather than
   apologizing for a missing feature. Anything touching the store, market,
   wallet, credentials, or account settings does not exist. Do not attempt
   any of it by other means — no direct `steamcmd` calls, no editing
   `appmanifest` files, no deleting game directories, and no activating a
   `steam://` URI or calling `steam -applaunch` yourself. Naming a `steam://`
   URI is fine — they are supported Valve entry points — but activating one is
   execution, and execution goes through the broker so the grant and the
   allowlist apply.

## Run an operation

Submit an `operation-plan/0.1` document on stdin. Build it with the planner and
unwrap the envelope — the broker wants the plan object, not the response
wrapper:

```bash
steam-agent operations plan install APPID --account ALIAS --machine MACHINE \
  --format json | jq .data.plan > plan.json
```

Every plan carries an `idempotency_key` of 8–128 characters that may be
recorded only once. The planner derives it by hashing the plan's inputs, so
regenerating an identical plan produces an identical key and `request` refuses
it as already recorded. A genuine new attempt needs a changed input, such as a
different `--expires-minutes`.

```bash
steam-agent-broker request --account ALIAS < plan.json
```

Under an `allow` grant the response is `{"operation_id", "state":
"authorized"}` and you proceed directly to `run`. Under a `confirm` grant the
response carries a `nonce` and the operation is not authorized until a human
approves it — relay the plan and its cost to the user in your own words, get
their answer, then pass it through:

```bash
steam-agent-broker confirm NONCE --actor owner
steam-agent-broker run
```

`--actor` is provenance for the ledger, not authentication. Never invent an
approval. A nonce is good for 15 minutes; if the answer arrives later the
operation has already expired and the user needs a fresh `request`, not a
retried `confirm`.

A response containing `auto_confirm_denied` means an `allow` grant did not
apply (usually the free-disk floor). The nonce is still valid, so this becomes
an ordinary confirm — surface the reason to the user rather than retrying.

`run` holds the session for the whole download, which can be long. Prefer
backgrounding it and polling `steam-agent-broker status --limit 5`.

## Read the outcome honestly

- `confirmed` — content installed and adopted by the client. Report it as
  installed and waiting, never as ready to play: EULAs, install scripts, and
  anti-cheat setup run on a human-present first launch.
- `deferred` — no content work completed and the operation is still
  authorized. Retry later with `run`; do NOT submit a new plan or a new
  idempotency key, and do not try to clear the blocking condition by stopping
  the user's game. Read the detail rather than assuming nothing happened:
  most deferrals occur before any side effect (a game running, a download in
  flight, unknown client state), but one reports that state was left for
  reconcile, meaning a client the broker stopped may still be stopped. Run
  `reconcile` when the detail says so, and tell the user Steam may be down.
- `expired` — seen in `status` when the 15-minute confirmation nonce or the
  four-hour execution window lapsed before the operation ran. Nothing
  happened. Unlike the terminal outcomes, an expired row releases its
  idempotency key, so the same plan can be resubmitted with `request`.
- `dispatched` — launch only, and terminal. The client accepted the request;
  that is the entire claim. Do not report the game as running or playable,
  and do not go looking for a process to upgrade the claim — a process
  cannot be told apart from a hung launcher or a DRM prompt.
- `aborted`, `failed`, `unconfirmed`, `contradicted` — terminal. Report what
  the detail says; a new attempt needs a new plan with a fresh key.
- `auth_required` — steamcmd needs an interactive Steam Guard login from the
  owner. Never retry-loop it.

Exit 0 is success — `confirmed`, or `dispatched` for a launch. Exit 1 is a
completed run whose outcome was neither. Exit 2 is a refusal or error.

## Recovery and boundaries

If a previous run was interrupted, `steam-agent-broker reconcile` maps the
observed state to exactly one recovery action. Run it before diagnosing by
hand; it works even when the policy file is unreadable.

Never edit `policy.toml` to grant yourself a capability the owner has not
granted, and never work around a `deny` or a failed gate. Those are the
owner's recorded intent. When something is blocked, report the block and what
would unblock it, and let the owner decide.

Keep account identifiers and private filesystem paths out of your output; the
broker already redacts them from its own logs and messages.
