from __future__ import annotations

from importlib import resources
from pathlib import Path
import sqlite3

import pytest

from steam_agent.storage import (
    CatalogObservation,
    CatalogPageInput,
    CatalogStreamInput,
    InvalidSyncTransition,
    OwnedObservation,
    Storage,
    steam_application_stable_id,
)


T0 = "2026-07-11T12:00:00Z"
T1 = "2026-07-11T12:01:00Z"
T2 = "2026-07-11T12:02:00Z"
T3 = "2026-07-11T12:03:00Z"


def _create_populated_v8(path: Path) -> None:
    migrations = resources.files("steam_agent").joinpath("migrations")
    with sqlite3.connect(path) as connection:
        connection.create_function(
            "steam_application_uuid_v5",
            1,
            steam_application_stable_id,
            deterministic=True,
        )
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 9):
            migration = next(
                item
                for item in migrations.iterdir()
                if item.name.startswith(f"{version:03d}_")
            )
            connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, T0),
            )
        connection.execute(
            "INSERT INTO steam_apps(appid, name, app_type, updated_at) "
            "VALUES (10, 'Existing App', 'unknown', ?)",
            (T0,),
        )
        connection.execute(
            "INSERT INTO game_entities(id, entity_kind, created_at, updated_at, stable_id) "
            "VALUES (10, 'application', ?, ?, ?)",
            (T0, T0, steam_application_stable_id(10)),
        )
        connection.execute(
            "INSERT INTO external_game_identities(provider, identity_kind, external_id, "
            "game_entity_id, created_at) VALUES "
            "('steam', 'application_appid', '10', 10, ?)",
            (T0,),
        )
        connection.commit()


def _stream(name: str, at: str = T0) -> CatalogStreamInput:
    games = name == "games"
    return CatalogStreamInput(
        stream=name,  # type: ignore[arg-type]
        termination="demand_boundary",
        scanned_through_appid=100,
        filter_context={
            "include_games": games,
            "include_dlc": not games,
            "include_software": not games,
            "include_videos": not games,
            "include_hardware": not games,
        },
        pages=(
            CatalogPageInput(
                page_number=1,
                requested_last_appid=0,
                first_appid=10,
                last_appid=100,
                item_count=3,
                have_more_results=True,
                retrieved_at=at,
            ),
        ),
    )


def test_populated_v8_upgrades_to_catalog_schema_without_rewriting_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v8.sqlite3"
    _create_populated_v8(path)

    with Storage(path) as storage:
        assert storage.get_app(10).name == "Existing App"  # type: ignore[union-attr]
        assert storage._connection.execute(
            "SELECT stable_id FROM game_entities WHERE id = 10"
        ).fetchone()[0] == steam_application_stable_id(10)
        assert storage._connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 9
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM catalog_current"
        ).fetchone()[0] == 0


def _begin(storage: Storage, at: str = T0) -> int:
    return storage.begin_sync(
        provider="steam_store_web_api",
        capability="catalog.application.read",
        started_at=at,
    ).id


def _complete(
    storage: Storage,
    demanded: list[int],
    observations: list[CatalogObservation],
    *,
    started_at: str = T0,
    completed_at: str = T1,
) -> int:
    run_id = _begin(storage, started_at)
    storage.complete_catalog_snapshot(
        run_id,
        demanded,
        observations,
        games=_stream("games", started_at),
        non_games=_stream("non_games", started_at),
        completed_at=completed_at,
    )
    return run_id


def test_complete_catalog_snapshot_persists_demanded_facts_and_provenance(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        run_id = _complete(
            storage,
            [10, 20, 30],
            [
                CatalogObservation(10, "game", 1000, 7),
                CatalogObservation(20, "non_game", None, 8),
                CatalogObservation(30, "not_observed"),
            ],
        )
        snapshot = storage.read_catalog_snapshot([10, 20, 30])

        assert [fact.classification for fact in snapshot.facts] == [
            "game",
            "non_game",
            "not_observed",
        ]
        assert snapshot.facts[0].stable_game_id == steam_application_stable_id(10)
        assert all(fact.promoted_sync_run_id == run_id for fact in snapshot.facts)
        assert len(snapshot.sources) == 1
        assert {stream.stream for stream in snapshot.sources[0].streams} == {
            "games",
            "non_games",
        }
        assert all(stream.pages[0].retrieved_at == T0 for stream in snapshot.sources[0].streams)
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE account_id IS NULL"
        ).fetchone()[0] == 3


def test_catalog_updates_only_demanded_ids_and_prunes_superseded_payload(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        first = _complete(
            storage,
            [10, 20],
            [CatalogObservation(10, "game"), CatalogObservation(20, "game")],
        )
        second = _complete(
            storage,
            [20, 30],
            [
                CatalogObservation(20, "non_game"),
                CatalogObservation(30, "not_observed"),
            ],
            started_at=T2,
            completed_at=T3,
        )
        snapshot = storage.read_catalog_snapshot([10, 20, 30])

        assert [fact.appid for fact in snapshot.facts] == [10, 20, 30]
        assert [fact.promoted_sync_run_id for fact in snapshot.facts] == [
            first,
            second,
            second,
        ]
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM catalog_observations"
        ).fetchone()[0] == 3
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE capability = 'catalog.application.read'"
        ).fetchone()[0] == 3


def test_failed_catalog_sync_preserves_last_good_and_has_no_payload(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        good = _complete(storage, [10], [CatalogObservation(10, "game")])
        failed = _begin(storage, T2)
        storage.finish_catalog_sync(
            failed,
            status="failed",
            completed_at=T3,
            error_code="PROVIDER_UNAVAILABLE",
        )
        snapshot = storage.read_catalog_snapshot([10])

        assert snapshot.facts[0].promoted_sync_run_id == good
        assert snapshot.latest is not None and snapshot.latest.id == failed
        assert snapshot.latest.status == "failed"
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM catalog_sync_metadata WHERE sync_run_id = ?",
            (failed,),
        ).fetchone()[0] == 0


def test_older_catalog_completion_cannot_replace_newer_overlapping_fact(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        older = _begin(storage, T0)
        newer = _begin(storage, T1)
        newer_result = storage.complete_catalog_snapshot(
            newer,
            [10],
            [CatalogObservation(10, "non_game")],
            games=_stream("games", T1),
            non_games=_stream("non_games", T1),
            completed_at=T2,
        )
        older_result = storage.complete_catalog_snapshot(
            older,
            [10],
            [CatalogObservation(10, "game")],
            games=_stream("games", T0),
            non_games=_stream("non_games", T0),
            completed_at=T3,
        )

        snapshot = storage.read_catalog_snapshot([10])
        assert snapshot.facts[0].classification == "non_game"
        assert snapshot.facts[0].promoted_sync_run_id == newer
        assert newer_result.promoted is True
        assert older_result.promoted is False
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM catalog_observations"
        ).fetchone()[0] == 1


def test_catalog_completion_is_atomic_on_late_promotion_failure(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        run_id = _begin(storage)
        storage._connection.execute(
            """
            CREATE TRIGGER reject_catalog_promotion
            BEFORE INSERT ON catalog_current
            BEGIN
                SELECT RAISE(ABORT, 'catalog promotion failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="catalog promotion failure"):
            storage.complete_catalog_snapshot(
                run_id,
                [10],
                [CatalogObservation(10, "game")],
                games=_stream("games"),
                non_games=_stream("non_games"),
                completed_at=T1,
            )
        assert storage.get_sync_run(run_id).status == "running"
        for table in (
            "catalog_current",
            "catalog_observations",
            "catalog_sync_metadata",
            "catalog_stream_provenance",
            "catalog_page_provenance",
            "evidence",
        ):
            assert storage._connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0


def test_catalog_rejects_non_application_or_incomplete_demand() -> None:
    with Storage(":memory:") as storage:
        run_id = _begin(storage)
        with pytest.raises(ValueError, match="classification"):
            storage.complete_catalog_snapshot(
                run_id,
                [10],
                [CatalogObservation(10, "package")],  # type: ignore[arg-type]
                games=_stream("games"),
                non_games=_stream("non_games"),
                completed_at=T1,
            )
        with pytest.raises(ValueError, match="exactly cover"):
            storage.complete_catalog_snapshot(
                run_id,
                [10, 20],
                [CatalogObservation(10, "game")],
                games=_stream("games"),
                non_games=_stream("non_games"),
                completed_at=T1,
            )
        assert storage.get_sync_run(run_id).status == "running"


def test_catalog_sync_requires_unscoped_catalog_run() -> None:
    with Storage(":memory:") as storage:
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=T0,
        )
        run = storage.begin_sync(
            provider="steam_store_web_api",
            capability="catalog.application.read",
            account_id=account.id,
            started_at=T0,
        )
        with pytest.raises(InvalidSyncTransition, match="cannot target"):
            storage.complete_catalog_snapshot(
                run.id,
                [10],
                [CatalogObservation(10, "game")],
                games=_stream("games"),
                non_games=_stream("non_games"),
                completed_at=T1,
            )


def test_library_snapshot_limits_catalog_facts_to_owned_installed_union(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=T0,
        )
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="test-v1",
            accepted_at=T0,
            backups_acknowledged=True,
        )
        owned_run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at=T0,
        )
        storage.complete_owned_snapshot(
            owned_run.id,
            [
                OwnedObservation(
                    appid=10,
                    name="Owned",
                    playtime_forever_minutes=0,
                    inclusion_basis="visible_owned",
                    observed_at=T0,
                )
            ],
            base_retrieved_at=T0,
            expanded_retrieved_at=T0,
            base_reported_count=1,
            expanded_reported_count=1,
            completed_at=T1,
        )
        _complete(
            storage,
            [10, 20],
            [CatalogObservation(10, "game"), CatalogObservation(20, "non_game")],
            started_at=T2,
            completed_at=T3,
        )

        library = storage.read_library_snapshot(account.id, "local")

        assert [fact.appid for fact in library.catalog.facts] == [10]
        assert library.catalog.facts[0].classification == "game"
        assert library.catalog.sources[0].provider == "steam_store_web_api"

        storage.delete_steam_account_data(account.id)
        retained_catalog = storage.read_catalog_snapshot([10])
        assert retained_catalog.facts[0].classification == "game"
        assert storage.get_app(10) is not None
