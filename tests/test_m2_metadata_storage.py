from __future__ import annotations

from pathlib import Path

from steam_agent.storage import Storage


T0 = "2026-07-11T00:00:00Z"
T1 = "2026-07-11T00:01:00Z"


def test_credential_reference_tracks_backend_without_secret(tmp_path: Path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        record = storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id="default",
            backend="os",
            backend_locator="keyring.backends.macOS.Keyring",
            configured_at=T0,
        )

        assert record.backend == "os"
        assert record.backend_locator == "keyring.backends.macOS.Keyring"
        assert "secret" not in repr(record).lower()
        assert storage.get_credential_reference(
            provider="steam", kind="web-api-key", profile_id="default"
        ) == record
        assert storage.remove_credential_reference(
            provider="steam", kind="web-api-key", profile_id="default"
        )
        assert storage.get_credential_reference(
            provider="steam", kind="web-api-key", profile_id="default"
        ) is None


def test_provider_probe_is_scoped_to_account_and_cascades(tmp_path: Path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561197960265728",
            configured_at=T0,
        )
        record = storage.save_provider_probe(
            capability="owned.visible.read",
            account_alias="primary",
            probe_state="ready",
            checked_at=T1,
            retryable=False,
        )

        assert record.probe_state == "ready"
        assert storage.get_provider_probe(
            capability="owned.visible.read", account_alias="PRIMARY"
        ) == record

        assert storage.remove_account("primary")
        assert storage.get_provider_probe(
            capability="owned.visible.read", account_alias="primary"
        ) is None


def test_provider_request_interval_is_persisted_and_atomic(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with Storage(database) as storage:
        assert storage.reserve_provider_request(
            provider="steam-web-api",
            budget_scope="user-key",
            requested_at=T0,
            minimum_interval_seconds=1,
        )
    with Storage(database) as storage:
        assert not storage.reserve_provider_request(
            provider="steam-web-api",
            budget_scope="user-key",
            requested_at="2026-07-11T00:00:00.500000Z",
            minimum_interval_seconds=1,
        )
        assert storage.reserve_provider_request(
            provider="steam-web-api",
            budget_scope="user-key",
            requested_at="2026-07-11T00:00:01Z",
            minimum_interval_seconds=1,
        )


def test_provider_request_interval_recovers_from_clock_rollback(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with Storage(database) as storage:
        assert storage.reserve_provider_request(
            provider="steam-web-api",
            budget_scope="user-key",
            requested_at="2026-07-11T01:00:00Z",
            minimum_interval_seconds=1,
        )
        assert not storage.reserve_provider_request(
            provider="steam-web-api",
            budget_scope="user-key",
            requested_at="2026-07-11T00:00:00Z",
            minimum_interval_seconds=1,
        )
        assert storage.reserve_provider_request(
            provider="steam-web-api",
            budget_scope="user-key",
            requested_at="2026-07-11T00:00:01Z",
            minimum_interval_seconds=1,
        )
