# ADR 0030: Verify (repair) is the second executable operation class

Status: accepted 2026-08-08 (Phase 2b of
[ADR 0027](0027-provisioned-execution.md)'s delivery plan, under
[ADR 0028](0028-trusted-manager-execution.md)'s trusted-manager model.
Supersedes the sentence in [ADR 0029](0029-move-as-inert-plan.md)'s
Consequences that fixed the broker's executable surface at exactly
`install`; the rest of 0029 stands.)

## Context

ADR 0027 planned repair as a distinct content-plane capability from the
start: `app_update <appid> validate` is the same Valve verb the client's
"verify integrity of game files" button runs, and the broker has used it
internally since Phase 1 as a recovery mechanism for an operation the owner
already authorized. What did not exist was a way for the owner's agent to
request it.

Repair is not a flag on install. It re-checks every file against the depot
manifest and replaces whatever differs, so it removes local modifications to
official content — a mod installed over game files is gone without further
warning. That consequence, not the mechanism, is why it needs its own grant.

## Decision

1. **`verify` is executable, granted separately.** The broker accepts plans
   whose operation is `verify` and runs `app_update <appid> validate`. The
   policy file gains a `verify` key taking the same `allow | confirm | deny`
   values as `install`. An `install` grant never carries `verify`, and an
   omitted key is `deny`.
2. **The plan schema does not change.** `verify` is already an
   `operation-plan/0.1` operation, so the planner emits these plans today and
   schema `0.1` stays intact. The delivery plan called this phase "repair";
   the executable class takes the plan's name.
3. **Verify repairs, it does not install.** Before any content work the
   broker requires an existing client manifest for the AppID, Valve's
   FullyInstalled bit in it, and a real file with bytes under the install
   directory. Failing any of those is a refusal, not a download.
4. **The bound is the manifest requirement, not the disk probe.** Content
   probes are evidence, not proof: residue in a hand-deleted directory can
   satisfy them, and validate would then re-download. What holds is that
   verify cannot run for a title with no client manifest, so its worst case
   re-acquires a title the owner already installed. It is not a route around
   `install = "deny"` for adding titles.
5. **Everything else is unchanged.** Uninstall stays human-in-Steam
   (ADR 0027 §10a), move ships as an inert plan (ADR 0029), and the
   hard-denied classes remain absent code rather than policy entries.

## Alternatives rejected

- Fold verify into the `install` grant: it shares install's mechanism but
  not its consequences. An owner who wants unattended updates has not
  thereby agreed to unattended replacement of their mods.
- A `repair` operation name distinct from the plan's `verify`: two names for
  one thing, and it would need an `operation-plan/0.1` change to carry.
- Requiring the measured directory size to approach the manifest's
  `SizeOnDisk`: a partially downloaded install is exactly the case validate
  exists to fix, so the threshold would refuse the operation most in need of
  it.

## Consequences

The unattended curation loop can now repair as well as install and update.
An owner granting `verify = "allow"` is authorizing unattended file
replacement within already-installed titles, which the policy template says
in the file itself. Phase 2c (launch allowlist) remains the only unbuilt
broker verb.
