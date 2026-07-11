from __future__ import annotations

from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
import sqlite3

import pytest

import steam_agent.cli as cli
from steam_agent.storage import (
    CatalogObservation,
    CatalogPageInput,
    CatalogStreamInput,
    EvidenceInput,
    InstalledObservation,
    InvalidSyncTransition,
    Machine,
    OwnedObservation,
    Storage,
    steam_application_stable_id,
)


T0 = "2026-07-11T12:00:00Z"
T1 = "2026-07-11T12:01:00Z"
T2 = "2026-07-11T12:02:00Z"
T3 = "2026-07-11T12:03:00Z"
T_OLD = "2026-07-09T12:00:00Z"


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
        assert (
            storage._connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            == 12
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM catalog_current"
            ).fetchone()[0]
            == 0
        )


def _catalog_account(storage: Storage) -> int:
    account = storage.get_account("catalog-test")
    if account is None:
        account = storage.configure_steam_account(
            alias="catalog-test",
            steam_id64="76561198000000099",
            configured_at=T0,
        )
    return account.id


def _begin(
    storage: Storage,
    at: str = T0,
    *,
    account_id: int | None = None,
    machine_id: str = "local",
    demanded: list[int] | None = None,
) -> int:
    return storage.begin_catalog_sync(
        provider="steam_store_web_api",
        account_id=_catalog_account(storage) if account_id is None else account_id,
        machine_id=machine_id,
        demanded_appids=[10] if demanded is None else demanded,
        started_at=at,
    ).id


def _complete(
    storage: Storage,
    demanded: list[int],
    observations: list[CatalogObservation],
    *,
    started_at: str = T0,
    completed_at: str = T1,
    account_id: int | None = None,
    machine_id: str = "local",
) -> int:
    run_id = _begin(
        storage,
        started_at,
        account_id=account_id,
        machine_id=machine_id,
        demanded=demanded,
    )
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
        assert all(
            stream.pages[0].retrieved_at == T0 for stream in snapshot.sources[0].streams
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE account_id IS NULL"
            ).fetchone()[0]
            == 3
        )


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
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM catalog_observations"
            ).fetchone()[0]
            == 3
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE capability = 'catalog.application.read'"
            ).fetchone()[0]
            == 3
        )


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
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM catalog_sync_metadata WHERE sync_run_id = ?",
                (failed,),
            ).fetchone()[0]
            == 0
        )


def test_catalog_latest_attempt_is_scoped_by_subject_and_demand(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        primary = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=T0,
        )
        other = storage.configure_steam_account(
            alias="other",
            steam_id64="76561198000000001",
            configured_at=T0,
        )
        good = _complete(
            storage,
            [10],
            [CatalogObservation(10, "game")],
            account_id=primary.id,
        )
        failed = _begin(
            storage,
            T2,
            account_id=primary.id,
            demanded=[10],
        )
        storage.finish_catalog_sync(
            failed,
            status="failed",
            completed_at=T2,
            error_code="PROVIDER_UNAVAILABLE",
        )
        other_run = _complete(
            storage,
            [10],
            [CatalogObservation(10, "non_game")],
            started_at=T3,
            completed_at=T3,
            account_id=other.id,
        )
        _complete(
            storage,
            [20],
            [CatalogObservation(20, "game")],
            started_at=T3,
            completed_at=T3,
            account_id=primary.id,
            machine_id="deck",
        )

        snapshot = storage.read_catalog_snapshot(
            [10], account_id=primary.id, machine_id="local"
        )

        assert snapshot.latest is not None
        assert snapshot.latest.id == failed
        assert snapshot.latest.status == "failed"
        assert snapshot.facts[0].promoted_sync_run_id == good
        assert snapshot.facts[0].classification == "game"
        assert snapshot.facts[0].observed_at == T1

        other_snapshot = storage.read_catalog_snapshot(
            [10], account_id=other.id, machine_id="local"
        )
        assert other_snapshot.facts[0].promoted_sync_run_id == other_run
        assert other_snapshot.facts[0].classification == "non_game"
        assert other_snapshot.facts[0].observed_at == T3


def test_other_account_refresh_cannot_freshen_or_reclassify_subject_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        primary = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=T_OLD,
        )
        other = storage.configure_steam_account(
            alias="other",
            steam_id64="76561198000000001",
            configured_at=T0,
        )
        primary_run = _complete(
            storage,
            [10],
            [CatalogObservation(10, "game")],
            started_at=T_OLD,
            completed_at=T_OLD,
            account_id=primary.id,
        )
        other_run = _complete(
            storage,
            [10],
            [CatalogObservation(10, "non_game")],
            started_at=T0,
            completed_at=T0,
            account_id=other.id,
        )
        primary_snapshot = storage.read_catalog_snapshot(
            [10], account_id=primary.id, machine_id="local"
        )
        other_snapshot = storage.read_catalog_snapshot(
            [10], account_id=other.id, machine_id="local"
        )

    monkeypatch.setattr(
        cli, "_utc_now", lambda: datetime.fromisoformat(T0.replace("Z", "+00:00"))
    )
    primary_completeness, _ = cli._catalog_completeness(
        primary_snapshot, demanded_appids={10}
    )
    other_completeness, _ = cli._catalog_completeness(
        other_snapshot, demanded_appids={10}
    )

    assert primary_snapshot.facts[0].promoted_sync_run_id == primary_run
    assert primary_snapshot.facts[0].classification == "game"
    assert primary_snapshot.facts[0].observed_at == T_OLD
    assert primary_completeness["status"] == "partial"
    assert primary_completeness["stale_capabilities"] == [
        "catalog.application.read"
    ]
    assert other_snapshot.facts[0].promoted_sync_run_id == other_run
    assert other_snapshot.facts[0].classification == "non_game"
    assert other_snapshot.facts[0].observed_at == T0
    assert other_completeness["status"] == "complete"


def test_failed_catalog_attempt_persists_subject_and_demand(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        run_id = _begin(
            storage,
            account_id=account_id,
            machine_id="deck",
            demanded=[10, 20],
        )
        storage.finish_catalog_sync(
            run_id,
            status="failed",
            completed_at=T1,
            error_code="PROVIDER_UNAVAILABLE",
        )

        subject = storage._connection.execute(
            "SELECT account_id, machine_id FROM catalog_sync_subjects "
            "WHERE sync_run_id = ?",
            (run_id,),
        ).fetchone()
        assert tuple(subject) == (account_id, "deck")
        assert [
            row[0]
            for row in storage._connection.execute(
                "SELECT appid FROM catalog_sync_demand "
                "WHERE sync_run_id = ? ORDER BY appid",
                (run_id,),
            )
        ] == [10, 20]


def test_unrelated_failure_does_not_stale_completed_demand(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        good = _complete(
            storage,
            [10],
            [CatalogObservation(10, "game")],
            account_id=account_id,
        )
        unrelated = _begin(
            storage,
            T2,
            account_id=account_id,
            demanded=[20],
        )
        storage.finish_catalog_sync(
            unrelated,
            status="failed",
            completed_at=T3,
            error_code="PROVIDER_UNAVAILABLE",
        )

        snapshot = storage.read_catalog_snapshot(
            [10], account_id=account_id, machine_id="local"
        )

        assert snapshot.latest is not None
        assert snapshot.latest.id == good
        assert snapshot.latest.status == "complete"


def test_running_catalog_refresh_keeps_fresh_last_good_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        _complete(
            storage,
            [10],
            [CatalogObservation(10, "game")],
            started_at=T0,
            completed_at=T0,
            account_id=account_id,
        )
        running = _begin(
            storage,
            T1,
            account_id=account_id,
            demanded=[10],
        )
        snapshot = storage.read_catalog_snapshot(
            [10], account_id=account_id, machine_id="local"
        )

    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime.fromisoformat(T1.replace("Z", "+00:00"))
        + timedelta(minutes=1),
    )
    value, metadata = cli._catalog_completeness(
        snapshot, demanded_appids={10}
    )

    assert snapshot.latest is not None and snapshot.latest.id == running
    assert value["status"] == "complete"
    assert value["stale_capabilities"] == []
    assert [warning["code"] for warning in value["warnings"]] == [
        "SYNC_IN_PROGRESS"
    ]
    assert metadata["last_attempt_status"] == "running"


def test_empty_catalog_demand_has_no_applicable_historical_attempt(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        _complete(
            storage,
            [10],
            [CatalogObservation(10, "game")],
            account_id=account_id,
        )
        failed = _begin(
            storage,
            T2,
            account_id=account_id,
            demanded=[10],
        )
        storage.finish_catalog_sync(
            failed,
            status="failed",
            completed_at=T3,
            error_code="PROVIDER_UNAVAILABLE",
        )
        snapshot = storage.read_catalog_snapshot(
            [], account_id=account_id, machine_id="local"
        )

    value, metadata = cli._catalog_completeness(snapshot, demanded_appids=set())

    assert snapshot.latest is None
    assert snapshot.facts == ()
    assert value["status"] == "complete"
    assert value["warnings"] == []
    assert value["missing_capabilities"] == []
    assert value["stale_capabilities"] == []
    assert metadata["last_attempt_status"] is None
    assert metadata["sources"] == []


def test_per_app_failed_attempt_degrades_multi_app_catalog_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        base = _complete(
            storage,
            [10, 20],
            [CatalogObservation(10, "game"), CatalogObservation(20, "game")],
            started_at=T0,
            completed_at=T0,
            account_id=account_id,
        )
        failed = _begin(
            storage,
            T1,
            account_id=account_id,
            demanded=[10],
        )
        storage.finish_catalog_sync(
            failed,
            status="failed",
            completed_at=T1,
            error_code="PROVIDER_UNAVAILABLE",
        )
        snapshot = storage.read_catalog_snapshot(
            [10, 20], account_id=account_id, machine_id="local"
        )

    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime.fromisoformat(T0.replace("Z", "+00:00"))
        + timedelta(hours=1),
    )
    value, metadata = cli._catalog_completeness(
        snapshot, demanded_appids={10, 20}
    )

    assert snapshot.latest is None
    assert [(fact.appid, fact.promoted_sync_run_id) for fact in snapshot.facts] == [
        (10, base),
        (20, base),
    ]
    assert value["status"] == "partial"
    assert value["stale_capabilities"] == ["catalog.application.read"]
    assert [
        (attempt["sync_run_id"], attempt["status"], attempt["appids"])
        for attempt in metadata["relevant_attempts"]
    ] == [
        (base, "complete", [20]),
        (failed, "failed", [10]),
    ]

    with Storage(tmp_path / "db.sqlite3") as storage:
        success_20 = _complete(
            storage,
            [20],
            [CatalogObservation(20, "non_game")],
            started_at=T2,
            completed_at=T2,
            account_id=account_id,
        )
        refreshed = storage.read_catalog_snapshot(
            [10, 20], account_id=account_id, machine_id="local"
        )
    refreshed_value, refreshed_metadata = cli._catalog_completeness(
        refreshed, demanded_appids={10, 20}
    )
    assert refreshed_value["status"] == "partial"
    assert [
        (attempt["sync_run_id"], attempt["status"], attempt["appids"])
        for attempt in refreshed_metadata["relevant_attempts"]
    ] == [
        (failed, "failed", [10]),
        (success_20, "complete", [20]),
    ]


def test_per_app_running_attempt_keeps_fresh_multi_app_slice_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        base = _complete(
            storage,
            [10, 20],
            [CatalogObservation(10, "game"), CatalogObservation(20, "game")],
            started_at=T0,
            completed_at=T0,
            account_id=account_id,
        )
        running = _begin(
            storage,
            T1,
            account_id=account_id,
            demanded=[10],
        )
        snapshot = storage.read_catalog_snapshot(
            [10, 20], account_id=account_id, machine_id="local"
        )

    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime.fromisoformat(T1.replace("Z", "+00:00"))
        + timedelta(minutes=1),
    )
    value, metadata = cli._catalog_completeness(
        snapshot, demanded_appids={10, 20}
    )

    assert snapshot.latest is None
    assert value["status"] == "complete"
    assert value["stale_capabilities"] == []
    assert [warning["code"] for warning in value["warnings"]] == [
        "SYNC_IN_PROGRESS"
    ]
    assert [
        (attempt["sync_run_id"], attempt["status"], attempt["appids"])
        for attempt in metadata["relevant_attempts"]
    ] == [
        (base, "complete", [20]),
        (running, "running", [10]),
    ]


@pytest.mark.parametrize(
    ("elapsed", "refresh_code"),
    [
        (timedelta(minutes=1), "SYNC_IN_PROGRESS"),
        (timedelta(minutes=16), "SYNC_ABANDONED"),
    ],
)
def test_first_running_catalog_attempt_reports_activity_without_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: timedelta,
    refresh_code: str,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        running = _begin(
            storage,
            T0,
            account_id=account_id,
            demanded=[10],
        )
        snapshot = storage.read_catalog_snapshot(
            [10], account_id=account_id, machine_id="local"
        )

    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime.fromisoformat(T0.replace("Z", "+00:00")) + elapsed,
    )
    value, metadata = cli._catalog_completeness(snapshot, demanded_appids={10})

    assert value["status"] == "partial"
    assert value["missing_capabilities"] == ["catalog.application.read"]
    assert value["stale_capabilities"] == []
    assert [warning["code"] for warning in value["warnings"]] == [
        "NOT_SYNCED",
        refresh_code,
    ]
    assert metadata["relevant_attempts"] == [
        {
            "sync_run_id": running,
            "status": "running",
            "error_code": None,
            "started_at": T0,
            "completed_at": None,
            "appids": [10],
        }
    ]


def test_first_failed_catalog_attempt_reports_failure_without_fact(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        failed = _begin(
            storage,
            T0,
            account_id=account_id,
            demanded=[10],
        )
        storage.finish_catalog_sync(
            failed,
            status="failed",
            completed_at=T1,
            error_code="PROVIDER_UNAVAILABLE",
        )
        snapshot = storage.read_catalog_snapshot(
            [10], account_id=account_id, machine_id="local"
        )

    value, metadata = cli._catalog_completeness(snapshot, demanded_appids={10})

    assert value["status"] == "partial"
    assert value["missing_capabilities"] == ["catalog.application.read"]
    assert [warning["code"] for warning in value["warnings"]] == [
        "NOT_SYNCED",
        "PROVIDER_UNAVAILABLE",
    ]
    assert metadata["last_attempt_status"] == "failed"
    assert metadata["last_error_code"] == "PROVIDER_UNAVAILABLE"


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
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM catalog_observations"
            ).fetchone()[0]
            == 1
        )


def test_subject_projection_merges_narrower_complete_demand(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        first = _complete(
            storage,
            [10, 20],
            [CatalogObservation(10, "game"), CatalogObservation(20, "game")],
            account_id=account_id,
        )
        second = _complete(
            storage,
            [20],
            [CatalogObservation(20, "non_game")],
            started_at=T2,
            completed_at=T3,
            account_id=account_id,
        )

        ten = storage.read_catalog_snapshot(
            [10], account_id=account_id, machine_id="local"
        )
        twenty = storage.read_catalog_snapshot(
            [20], account_id=account_id, machine_id="local"
        )

        assert ten.latest is not None and ten.latest.id == first
        assert ten.facts[0].promoted_sync_run_id == first
        assert ten.facts[0].classification == "game"
        assert twenty.latest is not None and twenty.latest.id == second
        assert twenty.facts[0].promoted_sync_run_id == second
        assert twenty.facts[0].classification == "non_game"


def test_out_of_order_disjoint_subject_completions_both_survive(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _catalog_account(storage)
        run_10 = _begin(storage, T0, account_id=account_id, demanded=[10])
        run_20 = _begin(storage, T1, account_id=account_id, demanded=[20])
        storage.complete_catalog_snapshot(
            run_20,
            [20],
            [CatalogObservation(20, "non_game")],
            games=_stream("games", T1),
            non_games=_stream("non_games", T1),
            completed_at=T2,
        )
        storage.complete_catalog_snapshot(
            run_10,
            [10],
            [CatalogObservation(10, "game")],
            games=_stream("games", T0),
            non_games=_stream("non_games", T0),
            completed_at=T3,
        )

        snapshot = storage.read_catalog_snapshot(
            [10, 20], account_id=account_id, machine_id="local"
        )

        assert [(fact.appid, fact.promoted_sync_run_id) for fact in snapshot.facts] == [
            (10, run_10),
            (20, run_20),
        ]
        assert [fact.classification for fact in snapshot.facts] == [
            "game",
            "non_game",
        ]


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
            assert (
                storage._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                    0
                ]
                == 0
            )


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
            account_id=account.id,
        )

        library = storage.read_library_snapshot(account.id, "local")

        assert [fact.appid for fact in library.catalog.facts] == [10]
        assert library.catalog.facts[0].classification == "game"
        assert library.catalog.sources[0].provider == "steam_store_web_api"

        storage.delete_steam_account_data(account.id)
        retained_catalog = storage.read_catalog_snapshot([10])
        assert retained_catalog.facts == ()
        assert storage.get_app(10) is None
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM catalog_sync_subjects"
            ).fetchone()[0]
            == 0
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM sync_runs "
                "WHERE capability = 'catalog.application.read'"
            ).fetchone()[0]
            == 0
        )


def test_account_delete_preserves_catalog_needed_by_other_demand_and_installed(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        primary = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=T0,
        )
        other = storage.configure_steam_account(
            alias="other",
            steam_id64="76561198000000001",
            configured_at=T0,
        )
        storage.upsert_machine(Machine("local", "PC", "linux"), observed_at=T0)
        installed_run = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=T0,
        )
        storage.record_installed_observation(
            installed_run.id,
            InstalledObservation(
                appid=20,
                library_root="/games",
                install_dir="game-20",
                observed_at=T0,
            ),
            EvidenceInput(
                provider="local_steam",
                capability="installed",
                source_kind="local_file",
                source_locator="appmanifest_20.acf",
                retrieved_at=T0,
                support_level="local_heuristic",
                payload={"appid": 20},
            ),
        )
        storage.finish_installed_sync(
            installed_run.id, status="complete", completed_at=T1
        )
        primary_run = _complete(
            storage,
            [10, 20, 30],
            [
                CatalogObservation(10, "game"),
                CatalogObservation(20, "game"),
                CatalogObservation(30, "not_observed"),
            ],
            account_id=primary.id,
        )
        other_attempt = _begin(
            storage,
            T2,
            account_id=other.id,
            demanded=[10],
        )
        storage.finish_catalog_sync(
            other_attempt,
            status="failed",
            completed_at=T3,
            error_code="PROVIDER_UNAVAILABLE",
        )

        result = storage.delete_steam_account_data(primary.id)
        snapshot = storage.read_catalog_snapshot([10, 20, 30])

        assert result.sync_runs_removed == 1
        assert result.evidence_removed == 1
        assert result.orphan_apps_removed == 1
        assert [(fact.appid, fact.classification) for fact in snapshot.facts] == [
            (10, "game"),
            (20, "game"),
        ]
        assert all(
            fact.promoted_sync_run_id not in (primary_run, other_attempt)
            for fact in snapshot.facts
        )
        assert all(
            storage._connection.execute(
                "SELECT 1 FROM catalog_sync_subjects WHERE sync_run_id = ?",
                (fact.promoted_sync_run_id,),
            ).fetchone()
            is None
            for fact in snapshot.facts
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM sync_runs WHERE id = ?", (primary_run,)
            ).fetchone()[0]
            == 0
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM catalog_sync_subjects WHERE account_id = ?",
                (primary.id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM catalog_sync_subjects WHERE account_id = ?",
                (other.id,),
            ).fetchone()[0]
            == 1
        )
        assert storage.get_app(10) is not None
        assert storage.get_app(20) is not None
        assert storage.get_app(30) is None


def test_account_delete_ignores_unpromoted_installed_observations_for_catalog(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=T0,
        )
        storage.upsert_machine(Machine("local", "PC", "linux"), observed_at=T0)

        def observe_installed(
            appid: int, *, status: str, started_at: str, completed_at: str
        ) -> None:
            run = storage.begin_sync(
                provider="local_steam",
                capability="installed",
                machine_id="local",
                started_at=started_at,
            )
            storage.record_installed_observation(
                run.id,
                InstalledObservation(
                    appid=appid,
                    library_root="/games",
                    install_dir=f"game-{appid}",
                    observed_at=started_at,
                ),
                EvidenceInput(
                    provider="local_steam",
                    capability="installed",
                    source_kind="local_file",
                    source_locator=f"appmanifest_{appid}.acf",
                    retrieved_at=started_at,
                    support_level="local_heuristic",
                    payload={"appid": appid},
                ),
            )
            storage.finish_installed_sync(
                run.id,
                status=status,  # type: ignore[arg-type]
                completed_at=completed_at,
            )

        observe_installed(10, status="complete", started_at=T0, completed_at=T1)
        observe_installed(20, status="complete", started_at=T1, completed_at=T2)
        observe_installed(30, status="partial", started_at=T2, completed_at=T3)
        assert [game.appid for game in storage.list_installed("local")] == [20]
        assert [
            int(row[0])
            for row in storage._connection.execute(
                "SELECT DISTINCT appid FROM installed_observations ORDER BY appid"
            )
        ] == [10, 20, 30]

        _complete(
            storage,
            [10, 20, 30],
            [
                CatalogObservation(10, "game"),
                CatalogObservation(20, "game"),
                CatalogObservation(30, "game"),
            ],
            started_at=T2,
            completed_at=T3,
            account_id=account.id,
        )

        storage.delete_steam_account_data(account.id)

        snapshot = storage.read_catalog_snapshot([10, 20, 30])
        assert [fact.appid for fact in snapshot.facts] == [20]


def test_all_provider_deletion_removes_catalog_but_preserves_installed(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        storage.upsert_machine(Machine("local", "PC", "linux"), observed_at=T0)
        installed_run = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=T0,
        )
        storage.record_installed_observation(
            installed_run.id,
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
        storage.finish_installed_sync(
            installed_run.id, status="complete", completed_at=T1
        )
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
                    appid=20,
                    name="Owned Only",
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
        catalog_run = storage.begin_catalog_sync(
            provider="steam_web_api",
            account_id=account.id,
            machine_id="local",
            demanded_appids=[10, 20, 30],
            started_at=T2,
        )
        storage.complete_catalog_snapshot(
            catalog_run.id,
            [10, 20, 30],
            [
                CatalogObservation(10, "game"),
                CatalogObservation(20, "game"),
                CatalogObservation(30, "not_observed"),
            ],
            games=_stream("games", T2),
            non_games=_stream("non_games", T2),
            completed_at=T3,
        )
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id="shared",
            backend="os",
            configured_at=T0,
        )

        result = storage.delete_all_steam_account_data(
            credential_provider="steam",
            credential_kind="web-api-key",
            credential_profile_id="shared",
        )

        assert result.accounts_removed == 1
        assert result.catalog_observations_removed == 3
        assert result.catalog_current_removed == 3
        assert result.catalog_sync_runs_removed == 1
        assert result.catalog_metadata_removed == 1
        assert result.catalog_streams_removed == 2
        assert result.catalog_pages_removed == 2
        assert result.catalog_evidence_removed == 3
        assert result.evidence_removed == 4
        assert result.sync_runs_removed == 2
        assert result.credential_refs_removed == 1
        assert result.orphan_apps_removed == 2
        assert [game.appid for game in storage.list_installed("local")] == [10]
        assert storage.get_app(10) is not None
        assert storage.get_app(20) is None
        assert storage.get_app(30) is None
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM sync_runs WHERE capability = 'installed'"
            ).fetchone()[0]
            == 1
        )
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE capability = 'installed'"
            ).fetchone()[0]
            == 1
        )
