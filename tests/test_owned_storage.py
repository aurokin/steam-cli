from __future__ import annotations

from importlib import resources
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
    steam_application_stable_id,
)


T0 = "2026-07-11T12:00:00Z"
T1 = "2026-07-11T12:01:00Z"
T2 = "2026-07-11T12:02:00Z"
T3 = "2026-07-11T12:03:00Z"


def _apply_migrations_through(
    connection: sqlite3.Connection, latest_version: int
) -> None:
    connection.create_function(
        "steam_application_uuid_v5",
        1,
        steam_application_stable_id,
        deterministic=True,
    )
    migrations = resources.files("steam_agent").joinpath("migrations")
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in range(1, latest_version + 1):
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


def test_v18_upgrade_backfills_empty_ready_achievement_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v18-empty-achievement.sqlite3"
    with sqlite3.connect(path) as connection:
        _apply_migrations_through(connection, 18)
        account_id = int(
            connection.execute(
                """INSERT INTO accounts(
                       alias, provider, provider_account_id, source_kind,
                       created_at, updated_at
                   ) VALUES ('primary', 'steam', '76561198000000000',
                             'upgrade-test', ?, ?)""",
                (T0, T0),
            ).lastrowid
        )
        connection.execute(
            """INSERT INTO steam_apps(appid, name, app_type, updated_at)
               VALUES (10, 'Empty Achievement Game', 'game', ?)""",
            (T0,),
        )
        run_id = int(
            connection.execute(
                """INSERT INTO sync_runs(
                       provider, capability, account_id, started_at, completed_at,
                       status, promoted, records_seen
                   ) VALUES ('steam_web_api', 'achievements.read', ?, ?, ?,
                             'complete', 1, 1)""",
                (account_id, T0, T1),
            ).lastrowid
        )
        connection.execute(
            """INSERT INTO achievement_sync_demand(
                   sync_run_id, account_id, appid, ordinal, targeted, evaluated,
                   state, observed_at
               ) VALUES (?, ?, 10, 0, 1, 1, 'ready', ?)""",
            (run_id, account_id, T0),
        )
        unpromoted_run_id = int(
            connection.execute(
                """INSERT INTO sync_runs(
                       provider, capability, account_id, started_at, completed_at,
                       status, promoted, records_seen, error_code
                   ) VALUES ('steam_web_api', 'achievements.read', ?, ?, ?,
                             'failed', 0, 1, 'SYNC_INTERRUPTED')""",
                (account_id, T2, T3),
            ).lastrowid
        )
        connection.execute(
            """INSERT INTO achievement_sync_demand(
                   sync_run_id, account_id, appid, ordinal, targeted, evaluated,
                   state, observed_at
               ) VALUES (?, ?, 10, 0, 1, 1, 'ready', ?)""",
            (unpromoted_run_id, account_id, T2),
        )
        connection.commit()

    with Storage(path) as storage:
        projection = storage._connection.execute(
            """SELECT observed_at, promoted_sync_run_id
               FROM achievement_player_projection
               WHERE account_id=? AND appid=10""",
            (account_id,),
        ).fetchone()
        assert tuple(projection) == (T0, run_id)
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM achievement_player_current"
        ).fetchone()[0] == 0


def test_v19_upgrade_repairs_unpromoted_empty_achievement_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v19-invalid-achievement-projection.sqlite3"
    with sqlite3.connect(path) as connection:
        _apply_migrations_through(connection, 19)
        account_id = int(
            connection.execute(
                """INSERT INTO accounts(
                       alias, provider, provider_account_id, source_kind,
                       created_at, updated_at
                   ) VALUES ('primary', 'steam', '76561198000000000',
                             'upgrade-test', ?, ?)""",
                (T0, T0),
            ).lastrowid
        )
        connection.executemany(
            """INSERT INTO steam_apps(appid, name, app_type, updated_at)
               VALUES (?, ?, 'game', ?)""",
            ((10, "Empty Game", T0), (20, "Nonempty Game", T0)),
        )
        valid_empty_run = int(
            connection.execute(
                """INSERT INTO sync_runs(
                       provider, capability, account_id, started_at, completed_at,
                       status, promoted, records_seen
                   ) VALUES ('steam_web_api', 'achievements.read', ?, ?, ?,
                             'complete', 1, 1)""",
                (account_id, T0, T1),
            ).lastrowid
        )
        invalid_newer_run = int(
            connection.execute(
                """INSERT INTO sync_runs(
                       provider, capability, account_id, started_at, completed_at,
                       status, promoted, records_seen, error_code
                   ) VALUES ('steam_web_api', 'achievements.read', ?, ?, ?,
                             'failed', 0, 1, 'SYNC_INTERRUPTED')""",
                (account_id, T2, T3),
            ).lastrowid
        )
        valid_nonempty_run = int(
            connection.execute(
                """INSERT INTO sync_runs(
                       provider, capability, account_id, started_at, completed_at,
                       status, promoted, records_seen
                   ) VALUES ('steam_web_api', 'achievements.read', ?, ?, ?,
                             'complete', 1, 1)""",
                (account_id, T0, T1),
            ).lastrowid
        )
        connection.executemany(
            """INSERT INTO achievement_sync_demand(
                   sync_run_id, account_id, appid, ordinal, targeted, evaluated,
                   state, observed_at
               ) VALUES (?, ?, ?, 0, 1, 1, 'ready', ?)""",
            (
                (valid_empty_run, account_id, 10, T0),
                (invalid_newer_run, account_id, 10, T2),
                (valid_nonempty_run, account_id, 20, T0),
            ),
        )
        connection.execute(
            """INSERT INTO achievement_player_current(
                   account_id, appid, api_name, achieved, unlock_time_unix,
                   observed_at, promoted_sync_run_id
               ) VALUES (?, 20, 'KEEP', 1, 1, ?, ?)""",
            (account_id, T0, valid_nonempty_run),
        )
        # Reproduce the marker written by the original version 019.
        connection.executemany(
            """INSERT INTO achievement_player_projection(
                   account_id, appid, observed_at, promoted_sync_run_id
               ) VALUES (?, ?, ?, ?)""",
            (
                (account_id, 10, T2, invalid_newer_run),
                (account_id, 20, T0, valid_nonempty_run),
            ),
        )
        connection.commit()

    with Storage(path) as storage:
        projections = storage._connection.execute(
            """SELECT appid, observed_at, promoted_sync_run_id
               FROM achievement_player_projection
               WHERE account_id=? ORDER BY appid""",
            (account_id,),
        ).fetchall()
        assert [tuple(row) for row in projections] == [
            (10, T0, valid_empty_run),
            (20, T0, valid_nonempty_run),
        ]
        player = storage._connection.execute(
            """SELECT api_name, promoted_sync_run_id
               FROM achievement_player_current
               WHERE account_id=? AND appid=20""",
            (account_id,),
        ).fetchone()
        assert tuple(player) == ("KEEP", valid_nonempty_run)


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
    storage.complete_owned_snapshot(
        run.id,
        games,
        base_retrieved_at=start,
        expanded_retrieved_at=start,
        base_reported_count=sum(
            game.inclusion_basis == "visible_owned" for game in games
        ),
        expanded_reported_count=len(games),
        completed_at=end,
    )
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
        ).fetchone() == (24,)


def test_populated_v5_upgrade_backfills_steam_application_identities(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade.sqlite3"
    with sqlite3.connect(path) as connection:
        _apply_migrations_through(connection, 5)
        connection.executemany(
            "INSERT INTO steam_apps(appid, name, app_type, updated_at) "
            "VALUES (?, ?, 'unknown', ?)",
            ((10, "Legacy Ten", T0), (20, None, T1)),
        )
        connection.execute(
            "INSERT INTO accounts(alias, provider, provider_account_id, source_kind, "
            "created_at, updated_at) VALUES "
            "('primary', 'steam', '76561198000000000', 'upgrade-test', ?, ?)",
            (T0, T0),
        )
        connection.commit()

    with Storage(path) as storage:
        assert storage.get_app(10).name == "Legacy Ten"  # type: ignore[union-attr]
        mappings = storage._connection.execute(
            """
            SELECT external_id, game_entity_id, entity_kind
            FROM external_game_identities
            JOIN game_entities ON game_entities.id = game_entity_id
            ORDER BY CAST(external_id AS INTEGER)
            """
        ).fetchall()
        assert [tuple(row) for row in mappings] == [
            ("10", 10, "application"),
            ("20", 20, "application"),
        ]
        assert storage.get_account("primary") is not None
        assert storage._connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 24


def test_original_populated_v6_upgrade_preserves_only_proven_legacy_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v6-upgrade.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        _apply_migrations_through(connection, 6)
        connection.execute(
            "INSERT INTO accounts(alias, provider, provider_account_id, source_kind, "
            "created_at, updated_at) VALUES "
            "('primary', 'steam', '76561198000000000', 'upgrade-test', ?, ?)",
            (T0, T0),
        )
        account_id = int(
            connection.execute(
                "SELECT id FROM accounts WHERE alias = 'primary'"
            ).fetchone()[0]
        )
        connection.executemany(
            "INSERT INTO steam_apps(appid, name, app_type, updated_at) "
            "VALUES (?, NULL, 'unknown', ?)",
            ((10, T0), (20, T0), (30, T0)),
        )
        run_id = int(
            connection.execute(
                """
                INSERT INTO sync_runs(
                    provider, capability, account_id, started_at, completed_at,
                    status, promoted, records_seen
                ) VALUES (
                    'steam_web_api', 'owned.visible.read', ?, ?, ?,
                    'complete', 1, 2
                )
                """,
                (account_id, T0, T1),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO owned_sync_metadata(
                sync_run_id, account_id, retrieved_at, include_appinfo,
                include_played_free_games, support_level
            ) VALUES (?, ?, ?, 1, 1, 'official_documented')
            """,
            (run_id, account_id, T0),
        )
        for appid, basis in ((10, "visible_owned"), (20, "played_free")):
            evidence_id = int(
                connection.execute(
                    """
                    INSERT INTO evidence(
                        provider, capability, source_kind, source_locator,
                        retrieved_at, support_level, context_json, payload_json,
                        content_hash
                        ) VALUES (
                            'steam_web_api', 'owned.visible.read', 'steam_web_api',
                            ?, ?, 'official_documented', ?, ?, ?
                    )
                    """,
                    (
                        f"GetOwnedGames:app:{appid}",
                        T0,
                            json.dumps(
                                {
                                    "include_appinfo": True,
                                    "include_played_free_games": False,
                                }
                            ),
                            json.dumps({"appid": appid}),
                            f"hash-{appid}",
                    ),
                ).lastrowid
            )
            connection.execute(
                """
                INSERT INTO owned_observations(
                    sync_run_id, evidence_id, account_id, appid, name,
                    playtime_forever_minutes, inclusion_basis, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, evidence_id, account_id, appid, f"Game {appid}", 0, basis, T0),
            )
            connection.execute(
                """
                INSERT INTO owned_current(
                    account_id, appid, evidence_id, promoted_sync_run_id, name,
                    playtime_forever_minutes, inclusion_basis, observed_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (account_id, appid, evidence_id, run_id, f"Game {appid}", basis, T0),
            )
        failed_run_id = int(
            connection.execute(
                """
                INSERT INTO sync_runs(
                    provider, capability, account_id, started_at, completed_at,
                    status, promoted, records_seen, error_code
                ) VALUES (
                    'steam_web_api', 'owned.visible.read', ?, ?, ?,
                    'failed', 0, 1, 'SYNC_FAILED'
                )
                """,
                (account_id, T2, T3),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO owned_sync_metadata(
                sync_run_id, account_id, retrieved_at, include_appinfo,
                include_played_free_games, support_level
            ) VALUES (?, ?, ?, 1, 0, 'official_documented')
            """,
            (failed_run_id, account_id, T2),
        )
        failed_evidence_id = int(
            connection.execute(
                """
                INSERT INTO evidence(
                    provider, capability, source_kind, source_locator,
                    retrieved_at, support_level, context_json, payload_json,
                    content_hash
                ) VALUES (
                    'steam_web_api', 'owned.visible.read', 'steam_web_api',
                    'GetOwnedGames:app:30', ?, 'official_documented', '{}',
                    '{"appid":30}', 'hash-30'
                )
                """,
                (T2,),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO owned_observations(
                sync_run_id, evidence_id, account_id, appid, name,
                playtime_forever_minutes, inclusion_basis, observed_at
            ) VALUES (?, ?, ?, 30, 'Failed Game', 1, 'visible_owned', ?)
            """,
            (failed_run_id, failed_evidence_id, account_id, T2),
        )
        connection.commit()

    with Storage(path) as storage:
        snapshot = storage.read_owned_snapshot(account_id)
        assert [game.appid for game in snapshot.games] == [10, 20]
        provenance = snapshot.latest_complete_provenance
        assert provenance is not None
        assert provenance.provider == "steam_web_api"
        assert provenance.base_retrieved_at is None
        assert provenance.expanded_retrieved_at == T0
        assert provenance.base_reported_count is None
        assert provenance.expanded_reported_count is None
        assert provenance.base_include_played_free_games is None
        assert provenance.expanded_include_played_free_games is False
        assert provenance.classification_method == "legacy_single_snapshot"
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM game_entities"
        ).fetchone()[0] == 3
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE account_id = ?", (account_id,)
        ).fetchone()[0] == 2
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM owned_observations"
        ).fetchone()[0] == 2
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM owned_sync_metadata"
        ).fetchone()[0] == 1
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM sync_runs WHERE account_id = ?", (account_id,)
        ).fetchone()[0] == 2
        assert storage._connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 24


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
                base_retrieved_at=T0,
                expanded_retrieved_at=T0,
                base_reported_count=1,
                expanded_reported_count=1,
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
        assert snapshot.latest_complete_provenance is not None
        assert snapshot.latest_complete_provenance.sync_run_id == run_id
        assert snapshot.latest_complete_provenance.provider == "steam_web_api"
        assert snapshot.latest_complete_provenance.support_level == "official_documented"
        assert snapshot.latest_complete_provenance.include_appinfo is True
        assert (
            snapshot.latest_complete_provenance.base_include_played_free_games
            is False
        )
        assert snapshot.latest_complete_provenance.base_reported_count == 1
        assert (
            snapshot.latest_complete_provenance.expanded_include_played_free_games
            is True
        )
        assert snapshot.latest_complete_provenance.expanded_reported_count == 2
        assert (
            snapshot.latest_complete_provenance.classification_method
            == "sequential_set_difference"
        )


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
                base_retrieved_at=at,
                expanded_retrieved_at=at,
                base_reported_count=1,
                expanded_reported_count=1,
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
            "classification_method": "sequential_set_difference",
            "request_pair": {
                "base": {
                    "include_appinfo": True,
                    "include_played_free_games": False,
                    "reported_count": 1,
                    "retrieved_at": T1,
                },
                "expanded": {
                    "include_appinfo": True,
                    "include_played_free_games": True,
                    "reported_count": 1,
                    "retrieved_at": T1,
                },
            },
        }
        assert set(json.loads(evidence["payload_json"])) == {
            "appid",
            "inclusion_basis",
            "name",
            "playtime_forever_minutes",
        }
        assert storage.get_app(10) is None
        assert storage._connection.execute(
            "SELECT 1 FROM external_game_identities WHERE external_id = '10'"
        ).fetchone() is None
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM owned_observations"
        ).fetchone()[0] == 1
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM owned_sync_metadata"
        ).fetchone()[0] == 1
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM evidence"
        ).fetchone()[0] == 1


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
            base_retrieved_at=T2,
            expanded_retrieved_at=T2,
            base_reported_count=1,
            expanded_reported_count=1,
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
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM owned_observations"
        ).fetchone()[0] == 1
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM owned_sync_metadata"
        ).fetchone()[0] == 1
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM evidence"
        ).fetchone()[0] == 1


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
                base_retrieved_at=T0,
                expanded_retrieved_at=T0,
                base_reported_count=2,
                expanded_reported_count=2,
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
                base_retrieved_at=T0,
                expanded_retrieved_at=T0,
                base_reported_count=2,
                expanded_reported_count=2,
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


def test_atomic_owned_completion_rolls_back_payload_and_promotion(
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
        storage._connection.execute(
            """
            CREATE TRIGGER reject_owned_promotion
            BEFORE INSERT ON owned_current
            BEGIN
                SELECT RAISE(ABORT, 'promotion failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="promotion failure"):
            storage.complete_owned_snapshot(
                run.id,
                [_owned(10, T0)],
                base_retrieved_at=T0,
                expanded_retrieved_at=T1,
                base_reported_count=1,
                expanded_reported_count=1,
                completed_at=T2,
            )

        assert storage.get_sync_run(run.id).status == "running"
        for table in (
            "owned_observations",
            "owned_current",
            "owned_sync_metadata",
            "evidence",
        ):
            assert storage._connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
        storage.finish_owned_sync(
            run.id,
            status="failed",
            completed_at=T3,
            error_code="SYNC_FAILED",
        )
        assert storage.get_sync_run(run.id).status == "failed"


def test_stable_game_id_survives_account_deletion_and_readdition(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        first_account = _account(storage)
        _consent(storage, first_account)
        _complete_owned(storage, first_account, [_owned(10, T0)], T0, T1)
        first_id = dict(
            storage.read_owned_snapshot(first_account).stable_game_ids_by_appid
        )[10]
        storage.delete_steam_account_data(first_account)

        second_account = _account(storage, alias="replacement", suffix=1)
        _consent(storage, second_account)
        _complete_owned(storage, second_account, [_owned(10, T2)], T2, T3)
        second_id = dict(
            storage.read_owned_snapshot(second_account).stable_game_ids_by_appid
        )[10]

        assert first_id == second_id == steam_application_stable_id(10)


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
        assert joined.stable_game_ids_by_appid == (
            (10, steam_application_stable_id(10)),
        )
        assert joined.owned.stable_game_ids_by_appid == (
            (10, steam_application_stable_id(10)),
        )
        assert storage._connection.execute(
            "SELECT account_id FROM evidence WHERE capability = 'installed'"
        ).fetchone()[0] is None


def test_stable_identity_lookup_occurs_inside_library_read_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = _account(storage)
        _consent(storage, account_id)
        _complete_owned(storage, account_id, [_owned(10, T0)], T0, T1)
        original = Storage._stable_game_ids_for_appids
        observed_transaction: list[bool] = []

        def checked(
            instance: Storage, appids: set[int]
        ) -> tuple[tuple[int, str], ...]:
            observed_transaction.append(instance._connection.in_transaction)
            return original(instance, appids)

        monkeypatch.setattr(Storage, "_stable_game_ids_for_appids", checked)
        snapshot = storage.read_library_snapshot(account_id, "local")

        assert observed_transaction and all(observed_transaction)
        assert snapshot.stable_game_ids_by_appid == (
            (10, steam_application_stable_id(10)),
        )


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
