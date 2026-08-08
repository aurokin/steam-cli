---
name: steam-agent-broker
description: Install or update owned Steam titles on a machine this checkout provisions, using the `steam-agent-broker` execution CLI. Use when the user asks to actually install, update, or re-acquire a game, to check what an execution attempt did, or to recover an interrupted operation. Do not use for library questions, rankings, or plans — those are read-only and belong to the `steam-agent` skill.
---

# Execute with Steam Agent Broker

`steam-agent-broker` is the only surface in this project that changes Steam.
It runs on the machine it manages, as the desktop user who owns that Steam
installation. `steam-agent` (the planner) never executes anything; the two
CLIs are separate on purpose.

Authority for this skill: [CLI contract](../../../docs/design/cli-contract.md)
for exact syntax and outcomes, [ADR 0027](../../../docs/adr/0027-provisioned-execution.md)
as re-scoped by [ADR 0028](../../../docs/adr/0028-trusted-manager-execution.md)
for why the boundaries are where they are.

## Before acting

1. Run `steam-agent-broker policy` to see the effective grants and limits, and
   `steam-agent-broker status` to see whether an operation is already active.
   One operation per machine runs at a time.
2. If the broker is not initialized, say so and stop. Provisioning it
   (`init --library ... --steamcmd ...`) is an owner decision, not a step to
   take on the user's behalf.
3. Only `install` is executable, and it covers updates of the same title.
   Uninstall, move, launch, repair, and anything touching the store, market,
   wallet, credentials, or account settings are not implemented. Do not
   attempt them by other means — no direct `steamcmd` calls, no editing
   `appmanifest` files, no deleting game directories.

## Run an operation

Submit an `operation-plan/0.1` document on stdin. Every plan needs an
`idempotency_key` of 8–128 characters that has never been used before.

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
approval: if the user has not answered, the operation waits.

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
- `aborted`, `failed`, `unconfirmed`, `contradicted` — terminal. Report what
  the detail says; a new attempt needs a new plan with a fresh key.
- `auth_required` — steamcmd needs an interactive Steam Guard login from the
  owner. Never retry-loop it.

Exit 0 is success, 1 is a completed run that did not end `confirmed`, 2 is a
refusal or error.

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
