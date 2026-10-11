---
name: steam-agent-broker
description: Install, update, repair, launch, or stop Steam games through the policy-gated host-local broker. Use for actual game actions, runtime observations, execution receipts, or recovery. Library questions and inert plans belong to steam-agent.
---

# Execute with Steam Agent Broker

Run `steam-agent-broker` on the gaming host as its desktop user. The planner
never executes Steam actions. Exact syntax, outcomes and support are canonical
in the [CLI contract](../../../docs/design/cli-contract.md#implemented-broker-execution-commands)
and [host support record](../../../docs/design/agent-host-support.md).
[ADR 0032](../../../docs/adr/0032-agent-requests-and-attributed-game-stop.md)
adds request receipts and attributed stop to the earlier broker decisions.

Before acting, inspect `capabilities`, `policy`, and `status`. A cached machine
alias is not remote routing. Use an existing authorized host connection; Windows
requires an interactive same-user desktop session, which ordinary service-session
SSH may not provide. If unprovisioned, check the intended state directory
(`--state-dir` or `STEAM_BROKER_STATE`) before reporting that prerequisite.
Provisioning and policy changes require the owner's authorization; use existing
authorization when it already covers those changes.

Prefer global `--format json` for the versioned result: receipt is durable
state, attempt describes this invocation, and retry advice grants no authority.
Use `--expected-machine ALIAS` to catch routing mistakes. `preflight` is
advisory; unknown prerequisites remain unknown and dispatch rechecks them.

Each action has its own grant. Launch and stop additionally have independent
AppID allowlists. Stop force permission is separate and defaults to false.
Never widen policy to make a refused operation pass. Install/update and verify
are Linux content operations; Windows content execution remains gated. Verify
replaces official files and can overwrite mods; explain that consequence when
relevant. Steam Guard and first-run setup are human interactions.

Prefer direct verbs with a request ID chosen once for the intended attempt:

```text
steam-agent-broker launch APPID --request-id ID
steam-agent-broker stop APPID --request-id ID
steam-agent-broker install APPID --request-id ID
steam-agent-broker verify APPID --request-id ID
```

Use `--account ALIAS` when overriding the configured alias. Under `allow`, these
request and execute through the same policy/ledger gates as `request` plus
`run`. Under `confirm`, relay the action and its consequences, obtain approval,
then consume the returned nonce:

```text
steam-agent-broker confirm NONCE --actor owner
steam-agent-broker run --operation-id OPERATION_ID
```

`--actor` is provenance, not authentication. Never invent approval. A pending
nonce lasts 15 minutes; authorization lasts four hours. Retry the same request
ID after a lost response; it returns the existing nonce or receipt. Reusing an
ID for different intent is refused. Terminal results never replay. A deliberate
new attempt uses a new ID, including after expiry. Expired direct requests retain
their original ID and receipt.
`--request-only` prepares without running. Legacy bare `operation-plan/0.1`
stdin intake remains supported; the planner puts it at `data.plan`. Stop uses
broker intent, not a planner operation-plan.

Interpret results according to their evidence:

- `confirmed`: content present/adopted; human first-run setup may remain.
- `dispatched`: launch accepted by the client; stop attributed processes exited
  or were absent, as detailed. Never claim playability or completed saves.
- `deferred`: still retryable in place. Use `run` later, not a fresh request ID.
  Read details: content recovery may still need to restore the client.
- `unconfirmed`: terminal uncertainty, including a crash or stop timeout after
  durable dispatch intent. Inspect/reconcile; never replay that request.
- `expired`, `aborted`, `failed`, `contradicted`, `auth_required`: report the
  specific reason. Steam Guard needs the owner; do not retry-loop it.

`runtime [--appid APPID]` independently reports live OS process evidence. It can
observe manual Steam launches with readable AppID environment attribution, but
cannot recognize every game. Unknown is not absence. The broker rechecks user,
Windows session, PID and creation time before stop. Do not replace attribution
with process-name guesses or kill shared Steam/Wine infrastructure. Graceful
Linux SIGTERM and Windows WM_CLOSE cannot prove saves completed. Request force
only when the user's authorization covers it, with `--force`, an independent
force-enabled policy, and a distinct intent. Timeout never implies force.

Use `inspect --request-id ID` after a lost response before considering another
action. It is read-only and does not resume or expire work. For a desktop timeout,
use `desktop-jobs` and `desktop-collect JOB_ID`; completed-only `--cleanup`
rechecks the original owned task. Collection never submits work, and unknown
or running jobs remain retained.

Use `inspect OPERATION_ID` for a receipt, `status --limit 5` for recent outcomes,
and `reconcile` for interrupted work. Never remove an active ledger or adoption
journal. Client-plane `dispatch_pending` recovers terminal `unconfirmed` without
replay. Content-plane recovery restores the prior client state when possible.

Do not bypass the broker with Steam commands, URI activation, manifest edits or
process kills. Uninstall/move remain human-in-Steam; store, market, wallet,
credentials and account mutation remain outside execution. Keep login names,
private paths, raw process environment, credentials and auth output out of
reports. No game process observation upgrades a launch receipt.
