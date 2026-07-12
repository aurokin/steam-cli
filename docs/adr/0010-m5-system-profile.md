# ADR 0010: redacted observed system profiles for M5

Status: accepted with M5 on 2026-07-12

## Context

Compatibility needs target-machine evidence, but broad native inventory can
collect hostnames, user names, serial numbers, network identifiers, paths, and
other facts that do not improve an agent's answer. Hardware state also changes
independently from the stable local machine alias used by M1.

## Decision

Keep the existing explicit `machines.id` alias as the target identity. Never
derive or verify it with a hardware fingerprint. Persist a separate versioned
`system-profile/0.1` observation only after machine-scoped disclosure.

Collectors use fixed native APIs, files, or commands and allowlist normalized
OS, architecture, CPU, memory, graphics, coarse storage, and bounded input facts.
They discard raw native output at the adapter boundary. Normal output, evidence,
and source locators omit host/user names, serials, UUIDs, MAC/IP addresses,
volume labels, device nodes, display identifiers, command lines, and paths.

Every fact is known or carries a typed unknown/unavailable/error state. A
complete validated observation may contain unknown facts and becomes last-good;
partial or failed collection never replaces last-good. Static hardware and
volatile available-space facts retain separate freshness. Machine-scoped
system-profile deletion preserves the M1 installed projection and machine alias.

## Consequences

M5 can assess one explicit target without turning hardware inventory into a
tracking identifier. Reusing one alias for a different physical machine cannot
be detected reliably; an observed platform/architecture conflict is therefore
reported and requires explicit replacement rather than silently rebinding it.
Remote profile import/export and broader device enumeration remain deferred.
