# ADR 0035: explicit account configuration without a local Steam client

Status: **Accepted, 2026-10-11.** Owner-requested removal of the local-file
prerequisite for explicit account setup; no execution or credential authority
changes.

## Decision

`accounts configure --steam-id64 ID` validates a positive ASCII decimal uint64
using the existing validator and stores its canonical form directly. It never
discovers a Steam installation or reads the login registry, resolves a provider
key, or contacts Valve. The existing `source_kind` field records
`explicit_steam_id`; this is a user assertion, not verified account access.

`--from-local-most-recent` remains an independent local-discovery mode with its
existing refusal rules and `local_steam_login_registry` source kind. Existing
callers may still pass `--steam-root` with an explicit ID; it is unused there,
as stated in help. It continues to select the root for local discovery.

Keep schema `0.1`, identifier opt-in, credential isolation, alias/identity
conflicts, internal account bindings and retained evidence unchanged. Existing
storage supports the source kind, so no migration is needed. Provider refreshes
still acquire evidence explicitly and preserve inaccessible versus empty data.

## Evidence and alternatives

Focused CLI tests forbid local-root discovery, account-file reads, credential
resolution and provider construction during explicit setup. They cover invalid
and overflowing IDs, canonicalization, redacted status, identifier opt-in and
conflicting aliases. A composed explicit-setup → injected Web API refresh →
cached-query test runs without Steam. Existing local-discovery and storage
tests remain in the gate. Installed-wheel smoke configures and reopens a
redacted account with an absent Steam root.

Keeping the local candidate requirement prevents fresh setup on an assistant
host even though provider reads need only an account ID and key. Adding another
explicit-ID flag would duplicate an existing option. Implicit provider probing
would add network and authentication effects to a local configuration command.
Those alternatives are rejected.

## Consequences and reversal

An explicit ID may name a nonexistent or inaccessible account; subsequent
provider evidence remains authoritative about access. Setup does not authenticate
or change Steam. New configurations record their actual source rather than
claiming local discovery. Reconfiguration of the same pair updates that source
without replacing its account row or cache. Older readers already support the
storage value; reverting CLI semantics would reintroduce the local-file
prerequisite for future configurations.

The [CLI contract](../design/cli-contract.md#implemented-m2-capability-gate-commands)
is canonical; the [user guide](../user-guide.md#add-an-account) provides setup.
