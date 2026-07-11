"""Secret storage boundary for provider credentials.

Secrets enter this module as :class:`SecretValue` instances and leave it only
through the deliberately named ``reveal`` method.  Backend failures are mapped
to fixed messages so neither a secret nor a private path can accidentally be
included in agent-facing output.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import platform
import re
import secrets
import stat
from typing import Iterator, Literal, Protocol, runtime_checkable

import keyring
from keyring import errors as keyring_errors


CredentialErrorCode = Literal[
    "CREDENTIAL_STORE_UNAVAILABLE",
    "CREDENTIAL_STORE_LOCKED",
    "CREDENTIAL_NOT_FOUND",
    "CREDENTIAL_WRITE_FAILED",
    "CREDENTIAL_READ_FAILED",
    "CREDENTIAL_DELETE_FAILED",
    "CREDENTIAL_ROLLBACK_FAILED",
    "FILE_STORE_NOT_APPROVED",
]

_ERROR_MESSAGES: dict[CredentialErrorCode, str] = {
    "CREDENTIAL_STORE_UNAVAILABLE": "No approved credential store is available.",
    "CREDENTIAL_STORE_LOCKED": "The credential store is locked or access was denied.",
    "CREDENTIAL_NOT_FOUND": "The requested credential is not configured.",
    "CREDENTIAL_WRITE_FAILED": "The credential could not be stored.",
    "CREDENTIAL_READ_FAILED": "The credential could not be read.",
    "CREDENTIAL_DELETE_FAILED": "The credential could not be removed.",
    "CREDENTIAL_ROLLBACK_FAILED": "The credential operation could not be safely rolled back.",
    "FILE_STORE_NOT_APPROVED": "Protected-file credential storage requires explicit approval.",
}


class CredentialError(RuntimeError):
    """A credential failure whose text is fixed and safe to serialize."""

    def __init__(self, code: CredentialErrorCode) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class SecretValue:
    """A small guard against accidental secret formatting or representation.

    Python strings cannot be reliably zeroed.  This wrapper reduces accidental
    disclosure; callers must still minimize the lifetime of the revealed value.
    """

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("a secret must be a non-empty string")
        self.__value = value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def reveal(self) -> str:
        """Return the secret for an approved provider/backend boundary."""

        return self.__value

    def redact(self, text: str) -> str:
        """Remove this exact secret from defensive diagnostic text."""

        return text.replace(self.__value, "<redacted>")


_REF_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """Stable non-secret identity for a stored provider credential."""

    provider: str
    kind: str
    profile_id: str

    def __post_init__(self) -> None:
        for value in (self.provider, self.kind, self.profile_id):
            if not _REF_PART.fullmatch(value):
                raise ValueError("credential reference parts must be stable identifiers")

    @property
    def service(self) -> str:
        return f"steam-agent:{self.provider}:{self.kind}"

    @property
    def account(self) -> str:
        return self.profile_id

    def storage_id(self) -> str:
        canonical = f"{self.provider}\0{self.kind}\0{self.profile_id}".encode()
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class StoreProbe:
    available: bool
    backend: str
    protection: Literal["os", "file", "memory"]


@runtime_checkable
class CredentialStore(Protocol):
    """Replaceable secret-store interface used by provider adapters."""

    def probe(self) -> StoreProbe: ...

    def put(self, ref: CredentialRef, secret: SecretValue) -> None: ...

    def resolve(self, ref: CredentialRef) -> SecretValue | None: ...

    def delete(self, ref: CredentialRef) -> bool: ...

    def contains(self, ref: CredentialRef) -> bool: ...


_ALLOWED_BACKENDS: dict[str, frozenset[tuple[str, str]]] = {
    "Darwin": frozenset({("keyring.backends.macOS", "Keyring")}),
    "Windows": frozenset({("keyring.backends.Windows", "WinVaultKeyring")}),
    "Linux": frozenset(
        {
            ("keyring.backends.SecretService", "Keyring"),
            ("keyring.backends.libsecret", "Keyring"),
            ("keyring.backends.kwallet", "DBusKeyring"),
            ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
        }
    ),
}


def _approved_backend(backend: object) -> bool:
    identity = (type(backend).__module__, type(backend).__name__)
    return identity in _ALLOWED_BACKENDS.get(platform.system(), frozenset())


def _backend_id(backend: object) -> str:
    return f"{type(backend).__module__}.{type(backend).__name__}"


def _select_native_backend(expected_id: str | None = None) -> object:
    backend = keyring.get_keyring()
    if _approved_backend(backend) and (
        expected_id is None or _backend_id(backend) == expected_id
    ):
        return backend

    # keyring may select its built-in chainer when multiple native desktop
    # services are installed.  Select an approved member, never an arbitrary
    # third-party or plaintext member of that chain.
    if (
        type(backend).__module__ == "keyring.backends.chainer"
        and type(backend).__name__ == "ChainerBackend"
    ):
        for candidate in getattr(backend, "backends", ()):
            if _approved_backend(candidate) and (
                expected_id is None or _backend_id(candidate) == expected_id
            ):
                return candidate
    raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")


class NativeKeyringStore:
    """Approved OS keychain/credential-wallet storage via ``keyring``."""

    def __init__(
        self, backend: object | None = None, *, backend_id: str | None = None
    ) -> None:
        selected = (
            _select_native_backend(backend_id) if backend is None else backend
        )
        if not _approved_backend(selected):
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
        if backend_id is not None and _backend_id(selected) != backend_id:
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
        self._backend = selected

    def probe(self) -> StoreProbe:
        return StoreProbe(
            available=True,
            backend=_backend_id(self._backend),
            protection="os",
        )

    def put(self, ref: CredentialRef, secret: SecretValue) -> None:
        try:
            self._backend.set_password(ref.service, ref.account, secret.reveal())
        except keyring_errors.KeyringLocked:
            raise CredentialError("CREDENTIAL_STORE_LOCKED") from None
        except Exception:
            raise CredentialError("CREDENTIAL_WRITE_FAILED") from None

    def resolve(self, ref: CredentialRef) -> SecretValue | None:
        try:
            value = self._backend.get_password(ref.service, ref.account)
        except keyring_errors.KeyringLocked:
            raise CredentialError("CREDENTIAL_STORE_LOCKED") from None
        except Exception:
            raise CredentialError("CREDENTIAL_READ_FAILED") from None
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise CredentialError("CREDENTIAL_READ_FAILED")
        return SecretValue(value)

    def delete(self, ref: CredentialRef) -> bool:
        try:
            if not self.contains(ref):
                return False
        except CredentialError as exc:
            if exc.code != "CREDENTIAL_READ_FAILED":
                raise
            # A backend entry that cannot be decoded should still get a direct
            # deletion attempt; never retrieve or serialize its bad value.
        try:
            self._backend.delete_password(ref.service, ref.account)
        except keyring_errors.PasswordDeleteError:
            raise CredentialError("CREDENTIAL_DELETE_FAILED") from None
        except keyring_errors.KeyringLocked:
            raise CredentialError("CREDENTIAL_STORE_LOCKED") from None
        except Exception:
            raise CredentialError("CREDENTIAL_DELETE_FAILED") from None
        return True

    def contains(self, ref: CredentialRef) -> bool:
        return self.resolve(ref) is not None


class ProtectedFileStore:
    """Explicit POSIX fallback protected by ownership and file modes.

    This backend is not encrypted and must never be selected automatically.
    ``approved=True`` records the caller's explicit policy decision.
    """

    _MAX_SECRET_BYTES = 64 * 1024

    def __init__(self, root: str | Path, *, approved: bool = False) -> None:
        if not approved:
            raise CredentialError("FILE_STORE_NOT_APPROVED")
        if os.name != "posix" or not hasattr(os, "geteuid"):
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
        self.root = Path(root)

    def probe(self) -> StoreProbe:
        try:
            if self.root.exists() or self.root.is_symlink():
                self._validate_directory()
        except CredentialError:
            return StoreProbe(False, "protected-file", "file")
        return StoreProbe(True, "protected-file", "file")

    def put(self, ref: CredentialRef, secret: SecretValue) -> None:
        try:
            payload = secret.reveal().encode("utf-8")
            if len(payload) > self._MAX_SECRET_BYTES:
                raise CredentialError("CREDENTIAL_WRITE_FAILED")
            self._ensure_directory()
            with self._locked():
                target = self._path(ref)
                if target.exists() or target.is_symlink():
                    self._validate_file(target)
                temporary = self.root / f".{target.name}.{secrets.token_hex(12)}.tmp"
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(temporary, flags, 0o600)
                    try:
                        os.fchmod(descriptor, 0o600)
                        view = memoryview(payload)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("short credential write")
                            view = view[written:]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.replace(temporary, target)
                    self._validate_file(target)
                    self._fsync_directory()
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("CREDENTIAL_WRITE_FAILED") from None

    def resolve(self, ref: CredentialRef) -> SecretValue | None:
        try:
            if not self.root.exists() and not self.root.is_symlink():
                return None
            self._validate_directory()
            with self._locked():
                target = self._path(ref)
                if not target.exists() and not target.is_symlink():
                    return None
                descriptor = self._open_validated_file(target, os.O_RDONLY)
                try:
                    chunks: list[bytes] = []
                    remaining = self._MAX_SECRET_BYTES + 1
                    while remaining:
                        chunk = os.read(descriptor, min(remaining, 8192))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                finally:
                    os.close(descriptor)
                payload = b"".join(chunks)
                if not payload or len(payload) > self._MAX_SECRET_BYTES:
                    raise CredentialError("CREDENTIAL_READ_FAILED")
                return SecretValue(payload.decode("utf-8"))
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("CREDENTIAL_READ_FAILED") from None

    def delete(self, ref: CredentialRef) -> bool:
        try:
            if not self.root.exists() and not self.root.is_symlink():
                return False
            self._validate_directory()
            with self._locked():
                target = self._path(ref)
                if not target.exists() and not target.is_symlink():
                    return False
                # Removal is the recovery path for an entry whose mode, link
                # count, or type drifted unsafe. The secure root and fixed
                # hashed name bound the unlink; never open or follow the entry.
                info = target.lstat()
                if stat.S_ISDIR(info.st_mode):
                    raise CredentialError("CREDENTIAL_DELETE_FAILED")
                target.unlink()
                self._fsync_directory()
                return True
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("CREDENTIAL_DELETE_FAILED") from None

    def contains(self, ref: CredentialRef) -> bool:
        return self.resolve(ref) is not None

    def _path(self, ref: CredentialRef) -> Path:
        return self.root / f"{ref.storage_id()}.secret"

    def _ensure_directory(self) -> None:
        try:
            self.root.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
        except Exception:
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE") from None
        try:
            self._validate_directory()
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE") from None

    def _validate_directory(self) -> None:
        info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
        ):
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")

    def _validate_file(self, path: Path) -> os.stat_result:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise CredentialError("CREDENTIAL_READ_FAILED")
        return info

    def _open_validated_file(self, path: Path, flags: int) -> int:
        before = self._validate_file(path)
        descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
        try:
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise CredentialError("CREDENTIAL_READ_FAILED")
            if (
                not stat.S_ISREG(after.st_mode)
                or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_uid != os.geteuid()
                or after.st_nlink != 1
            ):
                raise CredentialError("CREDENTIAL_READ_FAILED")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        lock_path = self.root / ".lock"
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow, 0o600
            )
            os.fchmod(descriptor, 0o600)
        except FileExistsError:
            self._validate_lock_file(lock_path)
            descriptor = os.open(lock_path, os.O_RDWR | nofollow)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _validate_lock_file(self, path: Path) -> None:
        try:
            info = path.lstat()
        except Exception:
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class InMemoryCredentialStore:
    """Deterministic test store; never selected by production policy."""

    def __init__(self) -> None:
        self._values: dict[CredentialRef, str] = {}

    def probe(self) -> StoreProbe:
        return StoreProbe(True, "memory", "memory")

    def put(self, ref: CredentialRef, secret: SecretValue) -> None:
        self._values[ref] = secret.reveal()

    def resolve(self, ref: CredentialRef) -> SecretValue | None:
        value = self._values.get(ref)
        return None if value is None else SecretValue(value)

    def delete(self, ref: CredentialRef) -> bool:
        return self._values.pop(ref, None) is not None

    def contains(self, ref: CredentialRef) -> bool:
        return ref in self._values


__all__ = [
    "CredentialError",
    "CredentialRef",
    "CredentialStore",
    "InMemoryCredentialStore",
    "NativeKeyringStore",
    "ProtectedFileStore",
    "SecretValue",
    "StoreProbe",
]
