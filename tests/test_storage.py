from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from importlib import resources
import os
from pathlib import Path
import sqlite3
import stat
from threading import Barrier, Event
from time import sleep

import pytest

from steam_agent.storage import (
    EvidenceInput,
    InstalledObservation,
    InvalidSyncTransition,
    Machine,
    Storage,
)


T0 = "2026-07-10T12:00:00Z"
T1 = "2026-07-10T12:01:00Z"
T2 = "2026-07-10T12:02:00Z"
T3 = "2026-07-10T12:03:00Z"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    with Storage(tmp_path / "steam-agent.sqlite3") as database:
        database.upsert_machine(
            Machine("desktop", "Gaming PC", "linux", "x86_64"), observed_at=T0
        )
        yield database


def evidence(appid: int, at: str) -> EvidenceInput:
    return EvidenceInput(
        provider="local_steam",
        capability="installed",
        source_kind="local_file",
        source_locator=f"steamapps/appmanifest_{appid}.acf",
        retrieved_at=at,
        support_level="core",
        context={"machine_id": "desktop"},
        payload={"appid": appid, "installdir": f"game-{appid}"},
    )


def observation(appid: int, at: str, *, name: str | None = None) -> InstalledObservation:
    return InstalledObservation(
        appid=appid,
        name=name or f"Game {appid}",
        app_type="game",
        library_root="/games/steam",
        install_dir=f"game-{appid}",
        build_id="100",
        size_bytes=1024,
        manifest_path=f"/games/steam/steamapps/appmanifest_{appid}.acf",
        manifest_mtime=at,
        observed_at=at,
    )


def completed_scan(storage: Storage, appids: list[int], start: str, end: str) -> int:
    run = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id="desktop",
        started_at=start,
    )
    for appid in appids:
        storage.record_installed_observation(
            run.id, observation(appid, start), evidence(appid, start)
        )
    storage.finish_installed_sync(run.id, status="complete", completed_at=end)
    return run.id


def test_complete_scan_promotes_observations_atomically(storage: Storage) -> None:
    run_id = completed_scan(storage, [10, 20], T0, T1)

    installed = storage.list_installed("desktop")
    assert [game.appid for game in installed] == [10, 20]
    assert all(game.promoted_sync_run_id == run_id for game in installed)
    assert installed[0].name == "Game 10"
    assert storage.get_sync_run(run_id).records_seen == 2


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_incomplete_scan_preserves_last_good_projection(
    storage: Storage, status: str
) -> None:
    original_run = completed_scan(storage, [10, 20], T0, T1)
    run = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id="desktop",
        started_at=T2,
    )
    storage.record_installed_observation(run.id, observation(10, T2), evidence(10, T2))

    storage.finish_installed_sync(
        run.id,
        status=status,  # type: ignore[arg-type]
        completed_at=T3,
        error_code="SCAN_INCOMPLETE",
    )

    installed = storage.list_installed("desktop")
    assert [game.appid for game in installed] == [10, 20]
    assert all(game.promoted_sync_run_id == original_run for game in installed)
    assert storage.get_sync_run(run.id).status == status


def test_later_complete_scan_replaces_machine_projection(storage: Storage) -> None:
    completed_scan(storage, [10, 20], T0, T1)
    replacement_run = completed_scan(storage, [20, 30], T2, T3)

    installed = storage.list_installed("desktop")
    assert [game.appid for game in installed] == [20, 30]
    assert all(game.promoted_sync_run_id == replacement_run for game in installed)


def test_complete_empty_scan_clears_projection(storage: Storage) -> None:
    completed_scan(storage, [10], T0, T1)
    completed_scan(storage, [], T2, T3)
    assert storage.list_installed("desktop") == []


def test_recording_same_observation_is_idempotent(storage: Storage) -> None:
    run = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id="desktop",
        started_at=T0,
    )
    first = storage.record_installed_observation(
        run.id, observation(10, T0), evidence(10, T0)
    )
    second = storage.record_installed_observation(
        run.id, observation(10, T0), evidence(10, T0)
    )

    assert first == second
    assert storage.get_sync_run(run.id).records_seen == 1
    storage.finish_installed_sync(run.id, status="complete", completed_at=T1)
    assert [game.appid for game in storage.list_installed("desktop")] == [10]


def test_observation_write_rolls_back_on_late_failure(storage: Storage) -> None:
    run = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id="desktop",
        started_at=T0,
    )
    storage._connection.execute(  # noqa: SLF001 - inject a late database failure
        """
        CREATE TRIGGER fail_records_seen
        BEFORE UPDATE OF records_seen ON sync_runs
        BEGIN
            SELECT RAISE(ABORT, 'injected late failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected late failure"):
        storage.record_installed_observation(
            run.id, observation(10, T0), evidence(10, T0)
        )

    assert storage.get_app(10) is None
    assert storage.get_sync_run(run.id).records_seen == 0
    assert storage._connection.execute(  # noqa: SLF001 - transaction assertion
        "SELECT COUNT(*) FROM evidence"
    ).fetchone()[0] == 0
    assert storage._connection.execute(  # noqa: SLF001 - transaction assertion
        "SELECT COUNT(*) FROM installed_observations WHERE sync_run_id = ?",
        (run.id,),
    ).fetchone()[0] == 0


def test_finishing_with_identical_arguments_is_idempotent(storage: Storage) -> None:
    run_id = completed_scan(storage, [10], T0, T1)
    first = storage.get_sync_run(run_id)
    second = storage.finish_installed_sync(
        run_id, status="complete", completed_at=T1
    )
    assert second == first


def test_terminal_run_rejects_new_observations(storage: Storage) -> None:
    run_id = completed_scan(storage, [10], T0, T1)
    with pytest.raises(InvalidSyncTransition):
        storage.record_installed_observation(
            run_id, observation(20, T2), evidence(20, T2)
        )


def test_database_reopens_with_projection_intact(tmp_path: Path) -> None:
    path = tmp_path / "steam-agent.sqlite3"
    with Storage(path) as first:
        first.upsert_machine(
            Machine("desktop", "Gaming PC", "linux", "x86_64"), observed_at=T0
        )
        completed_scan(first, [10], T0, T1)

    with Storage(path) as reopened:
        assert [game.appid for game in reopened.list_installed("desktop")] == [10]


def test_naive_timestamps_are_rejected(tmp_path: Path) -> None:
    with Storage(tmp_path / "test.sqlite3") as database:
        with pytest.raises(ValueError, match="timezone"):
            database.upsert_machine(
                Machine("desktop", "Gaming PC", "linux"),
                observed_at="2026-07-10T12:00:00",
            )


def test_migration_resource_is_packaged_and_applied_once(tmp_path: Path) -> None:
    migration = resources.files("steam_agent").joinpath(
        "migrations", "001_initial.sql"
    )
    assert migration.is_file()
    assert "CREATE TABLE installed_current" in migration.read_text(encoding="utf-8")

    path = tmp_path / "migrated.sqlite3"
    with Storage(path):
        pass
    with Storage(path):
        pass

    with sqlite3.connect(path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert versions == [
        (1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,),
        (12,), (13,), (14,)
    ]
    assert {"machines", "steam_apps", "sync_runs", "evidence"} <= tables
    assert {"installed_observations", "installed_current", "accounts"} <= tables


def test_machine_projections_are_isolated_and_ordered(tmp_path: Path) -> None:
    with Storage(tmp_path / "multi-machine.sqlite3") as database:
        database.upsert_machine(
            Machine("desktop", "Desktop", "linux", "x86_64"), observed_at=T0
        )
        database.upsert_machine(
            Machine("deck", "Steam Deck", "linux", "x86_64"), observed_at=T0
        )

        desktop = database.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="desktop",
            started_at=T0,
        )
        for appid in (30, 10, 20):
            database.record_installed_observation(
                desktop.id,
                observation(appid, T0),
                evidence(appid, T0),
            )
        database.finish_installed_sync(
            desktop.id, status="complete", completed_at=T1
        )

        deck = database.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="deck",
            started_at=T2,
        )
        database.record_installed_observation(
            deck.id,
            observation(40, T2),
            EvidenceInput(
                provider="local_steam",
                capability="installed",
                source_kind="local_file",
                source_locator="deck/appmanifest_40.acf",
                retrieved_at=T2,
                support_level="core",
                context={"machine_id": "deck"},
                payload={"appid": 40},
            ),
        )
        database.finish_installed_sync(deck.id, status="complete", completed_at=T3)

        assert [game.appid for game in database.list_installed("desktop")] == [
            10,
            20,
            30,
        ]
        assert [game.appid for game in database.list_installed("deck")] == [40]

        empty_deck = database.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="deck",
            started_at="2026-07-10T12:04:00Z",
        )
        database.finish_installed_sync(
            empty_deck.id,
            status="complete",
            completed_at="2026-07-10T12:05:00Z",
        )

        assert database.list_installed("deck") == []
        assert [game.appid for game in database.list_installed("desktop")] == [
            10,
            20,
            30,
        ]


def test_promoted_metadata_is_isolated_per_machine(tmp_path: Path) -> None:
    path = tmp_path / "metadata.sqlite3"
    with Storage(path) as database:
        database.upsert_machine(
            Machine("desktop", "Desktop", "linux"), observed_at=T0
        )
        database.upsert_machine(Machine("deck", "Deck", "linux"), observed_at=T0)

        desktop = database.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="desktop",
            started_at=T0,
        )
        database.record_installed_observation(
            desktop.id,
            observation(10, T0, name="Desktop Name"),
            evidence(10, T0),
        )
        database.finish_installed_sync(
            desktop.id, status="complete", completed_at=T1
        )

        deck = database.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="deck",
            started_at=T2,
        )
        database.record_installed_observation(
            deck.id,
            observation(10, T2, name="Deck Name"),
            EvidenceInput(
                provider="local_steam",
                capability="installed",
                source_kind="local_file",
                source_locator="deck/appmanifest_10.acf",
                retrieved_at=T2,
                support_level="core",
                context={"machine_id": "deck"},
                payload={"appid": 10},
            ),
        )
        database.finish_installed_sync(deck.id, status="complete", completed_at=T3)

        assert database.list_installed("desktop")[0].name == "Desktop Name"
        assert database.list_installed("deck")[0].name == "Deck Name"


def test_installed_snapshot_does_not_mix_projection_and_sync_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "snapshot.sqlite3"
    with Storage(path) as setup:
        setup.upsert_machine(
            Machine("desktop", "Desktop", "linux"), observed_at=T0
        )
        original_run = completed_scan(setup, [10], T0, T1)

    writer_ready = Event()
    writer_go = Event()
    writer_attempting = Event()

    def replace_projection() -> int:
        with Storage(path) as writer:
            writer_ready.set()
            assert writer_go.wait(timeout=5)
            writer_attempting.set()
            return completed_scan(writer, [20], T2, T3)

    with ThreadPoolExecutor(max_workers=1) as executor, Storage(path) as reader:
        future = executor.submit(replace_projection)
        assert writer_ready.wait(timeout=5)
        original_latest_sync = reader.latest_sync
        released_writer = False

        def latest_sync_during_write(**kwargs: object):
            nonlocal released_writer
            if not released_writer:
                released_writer = True
                writer_go.set()
                assert writer_attempting.wait(timeout=5)
                sleep(0.05)
            return original_latest_sync(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(reader, "latest_sync", latest_sync_during_write)
        snapshot = reader.read_installed_snapshot("desktop")
        replacement_run = future.result(timeout=5)

        assert [game.appid for game in snapshot.games] == [10]
        assert snapshot.latest is not None
        assert snapshot.latest.id == original_run
        assert snapshot.latest_complete is not None
        assert snapshot.latest_complete.id == original_run

        current = reader.read_installed_snapshot("desktop")
        assert [game.appid for game in current.games] == [20]
        assert current.latest is not None
        assert current.latest.id == replacement_run


def test_newer_scan_wins_when_scans_complete_out_of_order(storage: Storage) -> None:
    older = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id="desktop",
        started_at=T0,
    )
    storage.record_installed_observation(
        older.id, observation(10, T0), evidence(10, T0)
    )
    newer = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id="desktop",
        started_at=T1,
    )
    storage.record_installed_observation(
        newer.id, observation(20, T1), evidence(20, T1)
    )

    newer_result = storage.finish_installed_sync(
        newer.id, status="complete", completed_at=T2
    )
    older_result = storage.finish_installed_sync(
        older.id, status="complete", completed_at=T3
    )

    assert [game.appid for game in storage.list_installed("desktop")] == [20]
    assert newer_result.promoted is True
    assert older_result.promoted is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_storage_restricts_data_directory_and_database_modes(tmp_path: Path) -> None:
    data_dir = tmp_path / "private-data"
    database_path = data_dir / "steam-agent.sqlite3"

    with Storage(database_path):
        pass

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_storage_preserves_existing_data_directory_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "caller-owned"
    data_dir.mkdir(mode=0o750)
    data_dir.chmod(0o750)

    with Storage(data_dir / "steam-agent.sqlite3"):
        pass

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o750


def test_concurrent_first_open_applies_migration_once(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrent" / "steam-agent.sqlite3"
    workers = 6
    barrier = Barrier(workers)

    def initialize(_: int) -> tuple[int, ...]:
        barrier.wait(timeout=5)
        with Storage(database_path) as database:
            return tuple(
                row[0]
                for row in database._connection.execute(  # noqa: SLF001 - migration assertion
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(initialize, range(workers)))

    assert results == [(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)] * workers
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,),
            (11,), (12,), (13,), (14,)
        ]
