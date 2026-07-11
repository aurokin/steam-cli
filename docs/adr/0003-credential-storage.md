# ADR 0003: Native credential storage with an explicit protected-file fallback

Status: accepted for M2

Date: 2026-07-10

## Context

M2 needs a user-supplied Steam Web API key, and later provider checkpoints need
user-supplied ITAD, SteamGridDB, and GG.deals API keys. Secrets must remain outside command
arguments, logs, fixtures, normal output, SQLite evidence, and committed files.
The design must support a macOS-first CLI without making the provider layer
depend directly on one platform API.

Invoking Apple's `security` command would avoid a Python dependency, but passing
the password with `-p` or `-w` exposes it in process arguments. The command can
prompt when `-w` is its final argument, but delegating interactive input and
reading the secret back through subprocess output creates a brittle,
macOS-specific boundary. Apple's Keychain Services API is the appropriate
storage mechanism instead.

The maintained Python `keyring` package implements macOS Keychain through the
native Security framework. It also supports Windows Credential Manager and
Linux Secret Service or KWallet. Its backend discovery can include third-party,
null, or plaintext implementations, so accepting every discovered backend would
silently weaken the storage promise.

A local permission-protected file remains useful on POSIX systems without a
working desktop keychain. It is not equivalent to encrypted OS storage: it may
be readable by any process running as the user and may be included in backups.

## Decision

- Use `keyring` behind an internal `CredentialStore` protocol.
- Allow only known native macOS, Windows, Secret Service, libsecret, and KWallet
  backend types. Reject all other discovered backends. A native backend that is
  unavailable or locked is a typed failure; it never triggers an automatic
  fallback.
- Model secrets as `SecretValue`. Its string and representation are redacted,
  and revealing the value requires an explicit method call at a storage or
  provider-request boundary.
- Identify credentials by provider, kind, and stable internal profile ID.
  Derive the profile ID from a one-way hash of the selected local data-store
  path so separate `--data-dir` profiles cannot overwrite or delete each
  other's key. Keychain labels and lookup attributes contain no secret or path
  material.
- Keep every provider in a distinct credential reference. Third-party key
  changes do not invalidate Steam account capability evidence. ITAD OAuth
  client secrets remain out of scope because pricing endpoints use its API key.
- Persist the exact approved native backend identity used to create a key and
  require that same backend for later resolution/removal. Backend drift fails
  closed instead of orphaning a secret in another wallet.
- Scope a cross-process operation lock to the same local data profile and hold
  it across credential reads/writes, metadata commits, compensation, removal,
  and provider use so concurrent commands cannot invalidate one another's
  rollback snapshot.
- Provide a protected-file backend only on POSIX and only after explicit caller
  approval. Use a dedicated `0700` directory, hashed filenames, `0600` regular
  files, owner and link-count validation, advisory locking, bounded reads, and
  atomic write-and-replace. Reject symlinks, hard links, and unexpected modes
  for reads/writes; permit a fixed-name, no-follow unlink so an entry that has
  drifted unsafe can still be removed without reading it.
  Its location is fixed below the user's home configuration directory and does
  not honor data-directory or XDG overrides that may target a repository.
- Provide an in-memory implementation for deterministic tests. Production
  selection policy must never choose it.
- Translate backend and filesystem failures to fixed error codes and messages.
  Do not serialize raw backend exceptions or private paths.
- The CLI ingress added by M2 will use hidden interactive input. Its only
  non-interactive ingress may read a fixed, documented provider-specific
  environment variable. Secrets remain forbidden on argv, and no command will
  display or retrieve a stored value.

## Consequences

- `keyring` and its platform dependencies become runtime dependencies.
- Headless Linux environments may have no approved native backend; users must
  explicitly select the documented POSIX file fallback or use ephemeral
  environment-based credential ingress when that capability is implemented.
- Permission protection is not encryption. The file fallback must be labeled as
  such in capability and doctor output and excluded from ordinary export paths.
- Python strings cannot be reliably zeroed. The implementation minimizes
  deliberate copies and secret lifetime but does not claim memory erasure.
- The macOS backend associates access with the Python executable. Other Python
  code running through the same executable may have broader access than a
  signed standalone application would. Signed packaging and stronger Keychain
  access-control identity remain a future hardening option.
- Backend allowlists require maintenance when `keyring` changes native class
  names or adds a platform backend. Such changes fail closed until reviewed.

## Evidence

- [Apple: Adding a password to the keychain](https://developer.apple.com/documentation/security/adding-a-password-to-the-keychain)
- [Python `getpass` documentation](https://docs.python.org/3/library/getpass.html)
- [`keyring` documentation and security considerations](https://keyring.readthedocs.io/en/latest/)
- [`keyring` macOS native Security framework implementation](https://github.com/jaraco/keyring/blob/main/keyring/backends/macOS/api.py)
- [Microsoft `CredWriteW`](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)
- [freedesktop.org Secret Service API](https://specifications.freedesktop.org/secret-service/latest-single/)
