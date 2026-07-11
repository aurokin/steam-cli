from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from steam_agent.storage import (
    EvidenceInput,
    InstalledObservation,
    InvalidSyncTransition,
    Machine,
    OwnedObservation,
    Storage,
)


T0 = "2026-07-11T12:00:00Z"
T1 = "2026-07-11T12:01:00Z"
T2 = "2026-07-11T12:02:00Z"
T3 = "2026-07-11T12:03:00Z"


def _account(storage: Storage, alias: str = "primary", suffix: int = 0) -> int:
    return storage.configure_steam_account(
        alias=alias,
        steam_id64=str(76561198000000000 + suffix),
        configured_at=T0,
    ).id


def _consent(storage: Storage, account_id: int) -> None:
    storage.record_owned_data_consent(
        account_id=account_id,
        disclosure_version="owned-v1",
        accepted_at=T0,
        backups_acknowledged=True,
    )


def _owned(
    appid: int,
    at: str,
    *,
    basis: str = "visible_owned",
    name: str | None = None,
) -> OwnedObservation:
    return OwnedObservation(
        appid=appid,
        name=name or f"Account Game {appid}",
        playtime_forever_minutes=appid,
        inclusion_basis=basis,  # type: ignore[arg-type]
        observed_at=at,
    )


def _complete_owned(
    storage: Storage,
    account_id: int,
    games: list[OwnedObservation],
    start: str,
    end: str,
) -> int:
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="owned.visible.read",
        account_id=account_id,
        started_at=start,
    )
    storage.record_owned_snapshot(
        run.id,
        games,
        retrieved_at=start,
        include_appinfo=True,
        include_played_free_games=True,
    )
    storage.finish_owned_sync(run.id, status="complete", completed_at=end)
    return run.id


def test_owned_migration_and_secure_delete_are_enabled(tmp_path: Path) -> None:
    path = tmp_path / "owned.sqlite3"
    with Storage(path) as storage:
        assert storage._connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
        columns = {
            row[1]
            for row in storage._connection.execute("PRAGMA table_info(owned_current)")
        }
        assert columns == {
            "account_id",
            "appid",
            "evidence_id",
            "promoted_sync_run_id",
            "name",
            "playtime_forever_minutes",
            "inclusion_basis",
            "observed_at",
        }
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (6,)


def test_owned_snapshot_requires_reviewed_consent(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account_id,
            started_at=T0,
        )
        with pytest.raises(InvalidSyncTransition, match="consent"):
            storage.record_owned_snapshot(
                run.id,
                [_owned(10, T0)],
                retrieved_at=T0,
                include_appinfo=False,
                include_played_free_games=True,
            )
        assert storage.get_sync_run(run.id).records_seen == 0
        assert (
            storage._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            == 0
        )


def test_complete_owned_snapshot_preserves_basis_provenance_and_account_name(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        run_id = _complete_owned(
            storage,
            account_id,
            [_owned(10, T0, name="Private Name"), _owned(20, T0, basis="played_free")],
            T0,
            T1,
        )

        snapshot = storage.read_owned_snapshot(account_id)
        assert [game.appid for game in snapshot.games] == [10, 20]
        assert [game.inclusion_basis for game in snapshot.games] == [
            "visible_owned",
            "played_free",
        ]
        assert all(game.promoted_sync_run_id == run_id for game in snapshot.games)
        assert snapshot.latest_complete is not None
        assert snapshot.latest_complete.id == run_id


def test_complete_owned_sync_requires_an_explicit_recorded_snapshot(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account_id,
            started_at=T0,
        )
        with pytest.raises(InvalidSyncTransition, match="recorded snapshot"):
            storage.finish_owned_sync(run.id, status="complete", completed_at=T1)
        assert storage.get_sync_run(run.id).status == "running"


def test_nullable_playtime_remains_unknown(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        unknown = OwnedObservation(
            appid=10,
            name=None,
            playtime_forever_minutes=None,
            inclusion_basis="visible_owned",
            observed_at=T0,
        )
        _complete_owned(storage, account_id, [unknown], T0, T1)
        assert storage.list_owned(account_id)[0].playtime_forever_minutes is None


def test_older_complete_owned_sync_cannot_replace_newer_completion(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        older = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account_id,
            started_at=T0,
        )
        newer = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account_id,
            started_at=T1,
        )
        for run, appid, at in ((older, 10, T0), (newer, 20, T1)):
            storage.record_owned_snapshot(
                run.id,
                [_owned(appid, at)],
                retrieved_at=at,
                include_appinfo=True,
                include_played_free_games=True,
            )
        storage.finish_owned_sync(newer.id, status="complete", completed_at=T2)
        finished_older = storage.finish_owned_sync(
            older.id, status="complete", completed_at=T3
        )
        assert not finished_older.promoted
        assert [game.appid for game in storage.list_owned(account_id)] == [20]
        evidence = storage._connection.execute(
            "SELECT context_json, payload_json FROM evidence ORDER BY id LIMIT 1"
        ).fetchone()
        assert json.loads(evidence["context_json"]) == {
            "account_id": account_id,
            "include_appinfo": True,
            "include_played_free_games": True,
        }
        assert set(json.loads(evidence["payload_json"])) == {
            "appid",
            "inclusion_basis",
            "name",
            "playtime_forever_minutes",
        }
        assert storage.get_app(10).name is None  # type: ignore[union-attr]


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_incomplete_owned_sync_preserves_last_good(tmp_path: Path, status: str) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        original = _complete_owned(storage, account_id, [_owned(10, T0)], T0, T1)
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account_id,
            started_at=T2,
        )
        storage.record_owned_snapshot(
            run.id,
            [_owned(20, T2)],
            retrieved_at=T2,
            include_appinfo=False,
            include_played_free_games=True,
        )
        storage.finish_owned_sync(
            run.id,
            status=status,  # type: ignore[arg-type]
            completed_at=T3,
            error_code="SYNC_INCOMPLETE",
        )
        games = storage.list_owned(account_id)
        assert [game.appid for game in games] == [10]
        assert games[0].promoted_sync_run_id == original


def test_explicit_complete_empty_owned_snapshot_clears_projection(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        _complete_owned(storage, account_id, [_owned(10, T0)], T0, T1)
        run_id = _complete_owned(storage, account_id, [], T2, T3)
        snapshot = storage.read_owned_snapshot(account_id)
        assert snapshot.games == ()
        assert snapshot.latest_complete is not None
        assert snapshot.latest_complete.id == run_id


def test_owned_bulk_validation_is_atomic(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account_id,
            started_at=T0,
        )
        with pytest.raises(ValueError, match="unique"):
            storage.record_owned_snapshot(
                run.id,
                [_owned(10, T0), _owned(10, T0)],
                retrieved_at=T0,
                include_appinfo=False,
                include_played_free_games=True,
            )
        assert (
            storage._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            == 0
        )
        assert (
            storage._connection.execute("SELECT COUNT(*) FROM steam_apps").fetchone()[0]
            == 0
        )


def test_owned_bulk_write_rolls_back_after_late_failure(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account_id,
            started_at=T0,
        )
        storage._connection.execute(
            """
            CREATE TRIGGER reject_second_owned
            BEFORE INSERT ON owned_observations WHEN NEW.appid = 20
            BEGIN
                SELECT RAISE(ABORT, 'late owned failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="late owned failure"):
            storage.record_owned_snapshot(
                run.id,
                [_owned(10, T0), _owned(20, T0)],
                retrieved_at=T0,
                include_appinfo=True,
                include_played_free_games=True,
            )
        assert (
            storage._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            == 0
        )
        assert (
            storage._connection.execute("SELECT COUNT(*) FROM steam_apps").fetchone()[0]
            == 0
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM owned_sync_metadata"
            ).fetchone()[0]
            == 0
        )


def test_joined_library_snapshot_reads_both_projections(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        _complete_owned(storage, account_id, [_owned(10, T0)], T0, T1)
        storage.upsert_machine(Machine("local", "PC", "linux"), observed_at=T0)
        run = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=T0,
        )
        storage.record_installed_observation(
            run.id,
            InstalledObservation(
                appid=10,
                library_root="/games",
                install_dir="game-10",
                observed_at=T0,
            ),
            EvidenceInput(
                provider="local_steam",
                capability="installed",
                source_kind="local_file",
                source_locator="appmanifest_10.acf",
                retrieved_at=T0,
                support_level="local_heuristic",
                payload={"appid": 10},
            ),
        )
        storage.finish_installed_sync(run.id, status="complete", completed_at=T1)

        joined = storage.read_library_snapshot(account_id, "local")
        assert [game.appid for game in joined.owned.games] == [10]
        assert [game.appid for game in joined.installed.games] == [10]
        assert joined.owned.latest_complete is not None
        assert joined.installed.latest_complete is not None


def test_account_deletion_removes_account_data_but_preserves_m1_and_shared_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite3"
    with Storage(path) as storage:
        primary = _account(storage)
        other = _account(storage, "other", 1)
        _consent(storage, primary)
        _consent(storage, other)
        _complete_owned(
            storage, primary, [_owned(10, T0, name="DELETE-CANARY")], T0, T1
        )
        _complete_owned(storage, other, [_owned(20, T0)], T0, T1)
        storage.save_provider_probe(
            capability="owned.visible.read",
            account_alias="primary",
            probe_state="ready",
            checked_at=T1,
            retryable=False,
        )
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id="data-shared",
            backend="os",
            configured_at=T0,
        )
        storage.upsert_machine(Machine("local", "PC", "linux"), observed_at=T0)

        result = storage.delete_steam_account_data(primary)
        assert result.account_removed
        assert result.owned_observations_removed == 1
        assert result.owned_current_removed == 1
        assert result.probes_removed == 1
        assert result.consents_removed == 1
        assert result.shared_credential_preserved
        assert result.orphan_apps_removed == 1
        assert storage.get_account("primary") is None
        assert storage.get_account("other") is not None
        assert storage.get_app(10) is None
        assert [game.appid for game in storage.list_owned(other)] == [20]
        assert (
            storage.get_credential_reference(
                provider="steam", kind="web-api-key", profile_id="data-shared"
            )
            is not None
        )
        assert (
            storage._connection.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
            == 1
        )
        assert not storage.delete_steam_account_data(primary).account_removed

    assert b"DELETE-CANARY" not in path.read_bytes()


def test_all_account_deletion_can_remove_shared_credential_metadata_atomically(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        primary = _account(storage)
        other = _account(storage, "other", 1)
        _consent(storage, primary)
        _consent(storage, other)
        _complete_owned(storage, primary, [_owned(10, T0)], T0, T1)
        _complete_owned(storage, other, [_owned(20, T0)], T0, T1)
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id="data-shared",
            backend="os",
            configured_at=T0,
        )

        result = storage.delete_all_steam_account_data(
            credential_provider="steam",
            credential_kind="web-api-key",
            credential_profile_id="data-shared",
        )
        assert result.accounts_removed == 2
        assert result.owned_observations_removed == 2
        assert result.owned_current_removed == 2
        assert result.consents_removed == 2
        assert result.evidence_removed == 2
        assert result.orphan_apps_removed == 2
        assert result.credential_refs_removed == 1
        assert not result.shared_credential_preserved
        assert storage.list_accounts() == []
        assert (
            storage.get_credential_reference(
                provider="steam", kind="web-api-key", profile_id="data-shared"
            )
            is None
        )
        assert storage.delete_all_steam_account_data().accounts_removed == 0


def test_all_account_deletion_rolls_back_as_one_unit(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        primary = _account(storage)
        other = _account(storage, "other", 1)
        _consent(storage, primary)
        _consent(storage, other)
        _complete_owned(storage, primary, [_owned(10, T0)], T0, T1)
        _complete_owned(storage, other, [_owned(20, T0)], T0, T1)
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id="data-shared",
            backend="os",
            configured_at=T0,
        )
        storage._connection.execute(
            """
            CREATE TRIGGER interrupt_all_account_delete
            BEFORE DELETE ON accounts WHEN OLD.alias = 'other'
            BEGIN
                SELECT RAISE(ABORT, 'interrupted account deletion');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
            storage.delete_all_steam_account_data(
                credential_provider="steam",
                credential_kind="web-api-key",
                credential_profile_id="data-shared",
            )
        assert {account.alias for account in storage.list_accounts()} == {
            "primary",
            "other",
        }
        assert [game.appid for game in storage.list_owned(primary)] == [10]
        assert [game.appid for game in storage.list_owned(other)] == [20]
        assert (
            storage.get_credential_reference(
                provider="steam", kind="web-api-key", profile_id="data-shared"
            )
            is not None
        )
