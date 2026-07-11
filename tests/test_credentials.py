from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from steam_agent.credentials import (
    CredentialError,
    CredentialRef,
    CredentialStore,
    InMemoryCredentialStore,
    NativeKeyringStore,
    ProtectedFileStore,
    SecretValue,
)


@pytest.fixture
def ref() -> CredentialRef:
    return CredentialRef("steam", "web-api-key", "primary")


def test_secret_value_never_formats_its_value() -> None:
    value = SecretValue("canary-secret-value")

    assert str(value) == "<redacted>"
    assert repr(value) == "SecretValue(<redacted>)"
    assert "canary-secret-value" not in f"{value!r} {value}"
    assert value.redact("before canary-secret-value after") == "before <redacted> after"


@pytest.mark.parametrize("value", ["", None, b"bytes"])
def test_secret_value_rejects_empty_or_non_string_values(value: object) -> None:
    with pytest.raises(ValueError):
        SecretValue(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "part",
    ["", "../escape", "has space", "/absolute", "line\nbreak", "x" * 129],
)
def test_credential_ref_rejects_unsafe_parts(part: str) -> None:
    with pytest.raises(ValueError):
        CredentialRef(part, "web-api-key", "primary")


def test_credential_ref_has_stable_non_secret_backend_keys(ref: CredentialRef) -> None:
    assert ref.service == "steam-agent:steam:web-api-key"
    assert ref.account == "primary"
    assert len(ref.storage_id()) == 64
    assert ref.storage_id() == CredentialRef("steam", "web-api-key", "primary").storage_id()


def test_in_memory_store_satisfies_protocol_and_crud(ref: CredentialRef) -> None:
    store = InMemoryCredentialStore()
    assert isinstance(store, CredentialStore)
    assert store.probe().protection == "memory"
    assert not store.contains(ref)

    store.put(ref, SecretValue("secret-one"))
    assert store.contains(ref)
    assert store.resolve(ref).reveal() == "secret-one"  # type: ignore[union-attr]
    assert store.delete(ref)
    assert not store.delete(ref)
    assert store.resolve(ref) is None


def test_file_store_requires_explicit_approval(tmp_path: Path) -> None:
    with pytest.raises(CredentialError) as error:
        ProtectedFileStore(tmp_path / "credentials")

    assert error.value.code == "FILE_STORE_NOT_APPROVED"
    assert str(tmp_path) not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_reads_and_deletes_are_non_mutating_when_absent(
    tmp_path: Path, ref: CredentialRef
) -> None:
    root = tmp_path / "credentials"
    store = ProtectedFileStore(root, approved=True)

    assert store.resolve(ref) is None
    assert not store.delete(ref)
    assert not root.exists()


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_crud_uses_private_modes(tmp_path: Path, ref: CredentialRef) -> None:
    root = tmp_path / "credentials"
    store = ProtectedFileStore(root, approved=True)
    secret = "file-canary-secret"

    store.put(ref, SecretValue(secret))

    secret_path = root / f"{ref.storage_id()}.secret"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / ".lock").stat().st_mode) == 0o600
    assert store.resolve(ref).reveal() == secret  # type: ignore[union-attr]
    assert store.contains(ref)
    assert store.delete(ref)
    assert not secret_path.exists()
    assert not store.contains(ref)


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_overwrites_atomically_without_temp_files(
    tmp_path: Path, ref: CredentialRef
) -> None:
    root = tmp_path / "credentials"
    store = ProtectedFileStore(root, approved=True)
    store.put(ref, SecretValue("first"))
    store.put(ref, SecretValue("second"))

    assert store.resolve(ref).reveal() == "second"  # type: ignore[union-attr]
    assert sorted(path.name for path in root.iterdir()) == [
        ".lock",
        f"{ref.storage_id()}.secret",
    ]


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_cleans_secret_temp_file_after_write_failure(
    tmp_path: Path, ref: CredentialRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "credentials"
    store = ProtectedFileStore(root, approved=True)

    def fail_write(descriptor: int, payload: object) -> int:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("steam_agent.credentials.os.write", fail_write)
    with pytest.raises(CredentialError) as error:
        store.put(ref, SecretValue("temp-file-canary"))

    assert error.value.code == "CREDENTIAL_WRITE_FAILED"
    assert not list(root.glob("*.tmp"))
    assert "temp-file-canary" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_rejects_insecure_directory_mode(
    tmp_path: Path, ref: CredentialRef
) -> None:
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    root.chmod(0o755)
    store = ProtectedFileStore(root, approved=True)

    assert not store.probe().available
    with pytest.raises(CredentialError) as error:
        store.put(ref, SecretValue("never-written"))

    assert error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "never-written" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_rejects_symlinked_root(tmp_path: Path, ref: CredentialRef) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    root = tmp_path / "credentials"
    root.symlink_to(real, target_is_directory=True)
    store = ProtectedFileStore(root, approved=True)

    with pytest.raises(CredentialError) as error:
        store.put(ref, SecretValue("never-written"))

    assert error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_rejects_symlinked_secret(tmp_path: Path, ref: CredentialRef) -> None:
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("outside-secret", encoding="utf-8")
    outside.chmod(0o600)
    (root / f"{ref.storage_id()}.secret").symlink_to(outside)
    store = ProtectedFileStore(root, approved=True)

    with pytest.raises(CredentialError) as error:
        store.resolve(ref)

    assert error.value.code == "CREDENTIAL_READ_FAILED"
    assert "outside-secret" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_rejects_hardlinked_secret(tmp_path: Path, ref: CredentialRef) -> None:
    root = tmp_path / "credentials"
    store = ProtectedFileStore(root, approved=True)
    store.put(ref, SecretValue("hardlink-canary"))
    secret_path = root / f"{ref.storage_id()}.secret"
    os.link(secret_path, tmp_path / "second-link")

    with pytest.raises(CredentialError) as error:
        store.resolve(ref)

    assert error.value.code == "CREDENTIAL_READ_FAILED"
    assert "hardlink-canary" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_rejects_wrong_secret_mode(tmp_path: Path, ref: CredentialRef) -> None:
    root = tmp_path / "credentials"
    store = ProtectedFileStore(root, approved=True)
    store.put(ref, SecretValue("mode-canary"))
    (root / f"{ref.storage_id()}.secret").chmod(0o644)

    with pytest.raises(CredentialError) as error:
        store.resolve(ref)

    assert error.value.code == "CREDENTIAL_READ_FAILED"
    assert "mode-canary" not in str(error.value)
    assert store.delete(ref)
    assert not (root / f"{ref.storage_id()}.secret").exists()


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_deletes_unsafe_symlink_without_following_it(
    tmp_path: Path, ref: CredentialRef
) -> None:
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside-secret"
    outside.write_text("outside-canary", encoding="utf-8")
    target = root / f"{ref.storage_id()}.secret"
    target.symlink_to(outside)
    store = ProtectedFileStore(root, approved=True)

    assert store.delete(ref)
    assert not target.exists()
    assert outside.read_text(encoding="utf-8") == "outside-canary"


@pytest.mark.skipif(os.name != "posix", reason="protected fallback is POSIX-only")
def test_file_store_rejects_unsafe_existing_lock_without_mutating_it(
    tmp_path: Path, ref: CredentialRef
) -> None:
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    lock = root / ".lock"
    lock.write_text("not-a-secret", encoding="utf-8")
    lock.chmod(0o644)
    store = ProtectedFileStore(root, approved=True)

    with pytest.raises(CredentialError) as error:
        store.put(ref, SecretValue("never-written"))

    assert error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644


class _FakeBackendBase:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.failure: Exception | None = None

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.failure:
            raise self.failure
        self.values[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        if self.failure:
            raise self.failure
        return self.values.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        if self.failure:
            raise self.failure
        del self.values[(service, account)]


def _fake_approved_backend() -> _FakeBackendBase:
    backend_type = type("Keyring", (_FakeBackendBase,), {})
    backend_type.__module__ = "keyring.backends.macOS"
    return backend_type()


def test_native_store_rejects_unapproved_backend() -> None:
    with pytest.raises(CredentialError) as error:
        NativeKeyringStore(_FakeBackendBase())

    assert error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"


def test_native_store_rejects_backend_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("steam_agent.credentials.platform.system", lambda: "Darwin")
    with pytest.raises(CredentialError) as error:
        NativeKeyringStore(
            _fake_approved_backend(),
            backend_id="keyring.backends.SecretService.Keyring",
        )

    assert error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"


def test_native_store_crud_with_approved_backend(
    monkeypatch: pytest.MonkeyPatch, ref: CredentialRef
) -> None:
    monkeypatch.setattr("steam_agent.credentials.platform.system", lambda: "Darwin")
    store = NativeKeyringStore(_fake_approved_backend())

    store.put(ref, SecretValue("native-canary"))
    assert store.resolve(ref).reveal() == "native-canary"  # type: ignore[union-attr]
    assert store.contains(ref)
    assert store.delete(ref)
    assert store.resolve(ref) is None
    assert not store.delete(ref)


def test_native_store_can_delete_an_undecodable_existing_entry(
    monkeypatch: pytest.MonkeyPatch, ref: CredentialRef
) -> None:
    monkeypatch.setattr("steam_agent.credentials.platform.system", lambda: "Darwin")
    backend = _fake_approved_backend()
    backend.values[(ref.service, ref.account)] = ""
    store = NativeKeyringStore(backend)

    with pytest.raises(CredentialError) as error:
        store.resolve(ref)
    assert error.value.code == "CREDENTIAL_READ_FAILED"
    assert store.delete(ref)
    assert (ref.service, ref.account) not in backend.values


def test_native_backend_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, ref: CredentialRef
) -> None:
    monkeypatch.setattr("steam_agent.credentials.platform.system", lambda: "Darwin")
    backend = _fake_approved_backend()
    backend.failure = RuntimeError("backend leaked canary-secret and /private/path")
    store = NativeKeyringStore(backend)

    with pytest.raises(CredentialError) as error:
        store.put(ref, SecretValue("canary-secret"))

    assert error.value.code == "CREDENTIAL_WRITE_FAILED"
    assert "canary-secret" not in str(error.value)
    assert "/private/path" not in str(error.value)


def test_native_default_fails_closed_for_unapproved_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("steam_agent.credentials.keyring.get_keyring", _FakeBackendBase)

    with pytest.raises(CredentialError) as error:
        NativeKeyringStore()

    assert error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
