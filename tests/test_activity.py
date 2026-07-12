from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from steam_agent.activity import (
    ACTIVITY_DISCLOSURE_VERSION,
    ActivitySyncError,
    query_activity,
    query_achievements,
    sync_activity,
    sync_achievements,
)
from steam_agent.credentials import SecretValue
from steam_agent.steam_activity_api import (
    AchievementDefinition,
    ActivityAcquisition,
    ActivityGame,
    ActivityList,
    GameAchievementSchema,
    PlayerAchievement,
    PlayerAchievements,
    SteamActivityApiError,
)
from steam_agent.storage import InvalidSyncTransition, Storage


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self) -> None:
        self.fail_activity = False
        self.player: dict[int, PlayerAchievements | Exception] = {}
        self.schema: dict[int, GameAchievementSchema | Exception] = {}
        self.schema_calls: list[int] = []

    def fetch_activity(self, **_: object) -> ActivityAcquisition:
        if self.fail_activity:
            raise SteamActivityApiError("PROVIDER_UNAVAILABLE", retryable=True)
        owned = ActivityGame(10, 120, None, 100, 10, 10, 5, 2, 1_720_000_000)
        recent = ActivityGame(10, 125, 30, 105, 10, 10, 5, 2, 1_720_000_100)
        return ActivityAcquisition(ActivityList("ready", (owned,), 1), ActivityList("ready", (recent,), 1))

    def fetch_player_achievements(self, *, appid: int, **_: object) -> PlayerAchievements:
        value = self.player[appid]
        if isinstance(value, Exception):
            raise value
        return value

    def fetch_achievement_schema(self, *, appid: int, **_: object) -> GameAchievementSchema:
        self.schema_calls.append(appid)
        value = self.schema[appid]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def configured() -> tuple[Storage, int]:
    storage = Storage(":memory:")
    account = storage.configure_steam_account(alias="primary", steam_id64="76561198000000001", configured_at=NOW)
    storage.record_activity_data_consent(
        account_id=account.id,
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        accepted_at=NOW,
        backups_acknowledged=True,
    )
    yield storage, account.id
    storage.close()


def test_activity_promotes_atomically_and_failure_preserves_last_good(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    first = sync_activity(
        storage, account_id=account_id, steamid="76561198000000001",
        api_key=SecretValue("secret"), client=client, clock=lambda: NOW,
    )
    assert first.owned_count == 1 and first.recent_count == 1
    item = query_activity(storage, account_id=account_id, clock=lambda: NOW)["items"][0]
    assert item["playtime"]["lifetime_minutes"] == 125
    assert item["playtime"]["recent_window_minutes"] == 30
    assert item["freshness"] == {"activity": "fresh", "recent_window": "fresh"}

    client.fail_activity = True
    with pytest.raises(ActivitySyncError, match="PROVIDER_UNAVAILABLE"):
        sync_activity(
            storage, account_id=account_id, steamid="76561198000000001",
            api_key=SecretValue("secret"), client=client, clock=lambda: NOW,
        )
    result = query_activity(storage, account_id=account_id, clock=lambda: NOW)
    assert len(result["items"]) == 1
    assert result["snapshot"]["using_last_good"] is True


def test_valid_empty_activity_clears_projection(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    sync_activity(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), client=client, clock=lambda: NOW)
    client.fetch_activity = lambda **_: ActivityAcquisition(ActivityList("ready", (), 0), ActivityList("ready", (), 0))  # type: ignore[method-assign]
    sync_activity(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), client=client, clock=lambda: NOW)
    assert query_activity(storage, account_id=account_id, clock=lambda: NOW)["items"] == []


def test_achievement_demand_is_bounded_and_hidden_locked_text_is_suppressed(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    for appid in (10, 20, 30):
        client.player[appid] = PlayerAchievements(
            appid, "ready", (PlayerAchievement("LOCKED", False, None), PlayerAchievement("DONE", True, 1_720_000_000))
        )
        client.schema[appid] = GameAchievementSchema(
            appid, "ready", "english",
            (AchievementDefinition("LOCKED", "Secret title", "Secret text", True), AchievementDefinition("DONE", "Done", "Public", False)),
        )
    result = sync_achievements(
        storage, account_id=account_id, steamid="76561198000000001",
        api_key=SecretValue("s"), scope="owned", explicit_appids=(30, 10, 20),
        max_items=2, client=client, clock=lambda: NOW,
    )
    assert result.targeted_count == 2
    assert result.state_counts == {"ready": 2, "unevaluated": 1}
    items = query_achievements(storage, account_id=account_id, clock=lambda: NOW)["items"]
    assert [item["appid"] for item in items] == [10, 20, 30]
    assert items[1]["state"] == "unevaluated" and items[1]["evaluated"] is False
    locked = items[2]["achievements"][1]
    assert locked["api_name"] == "LOCKED"
    assert locked["display_name"] is None and locked["description"] is None


def test_achievement_failure_is_per_app_and_preserves_previous_current(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = PlayerAchievements(10, "ready", (PlayerAchievement("A", True, 1),))
    client.schema[10] = GameAchievementSchema(10, "ready", "english", (AchievementDefinition("A", "A", None, False),))
    sync_achievements(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), scope="owned", explicit_appids=(10,), client=client, clock=lambda: NOW)
    client.player[10] = SteamActivityApiError("PROVIDER_UNAVAILABLE", retryable=True)
    result = sync_achievements(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), scope="owned", explicit_appids=(10,), client=client, clock=lambda: NOW)
    assert result.state_counts == {"failed": 1}
    item = query_achievements(storage, account_id=account_id, clock=lambda: NOW)["items"][0]
    assert item["state"] == "failed"
    assert item["achievements"] == []
    with storage._connection as connection:
        assert connection.execute("SELECT COUNT(*) FROM achievement_player_current WHERE account_id=?", (account_id,)).fetchone()[0] == 1


def test_account_deletion_reports_and_removes_activity(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    sync_activity(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), client=client, clock=lambda: NOW)
    deletion = storage.delete_steam_account_data(account_id)
    assert deletion.activity_observations_removed == 1
    assert deletion.activity_current_removed == 1
    assert storage._connection.execute("SELECT COUNT(*) FROM activity_current").fetchone()[0] == 0


def test_ready_player_survives_unsupported_schema(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = PlayerAchievements(10, "ready", (PlayerAchievement("A", True, 1),))
    client.schema[10] = GameAchievementSchema(10, "achievements_not_supported", "english")
    sync_achievements(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), scope="owned", explicit_appids=(10,), client=client, clock=lambda: NOW)
    item = query_achievements(storage, account_id=account_id, clock=lambda: NOW)["items"][0]
    assert item["state"] == "ready"
    assert item["summary"]["unlocked"] == 1
    assert item["achievements"][0]["api_name"] == "A"


def test_provider_wide_failure_stops_fanout_and_marks_remaining_unevaluated(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = SteamActivityApiError("RATE_LIMITED", retryable=True)
    result = sync_achievements(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), scope="owned", explicit_appids=(10, 20, 30), max_items=3, client=client, clock=lambda: NOW)
    assert result.state_counts == {"failed": 1, "unevaluated": 2}
    items = query_achievements(storage, account_id=account_id, clock=lambda: NOW)["items"]
    assert [item["state"] for item in items] == ["failed", "unevaluated", "unevaluated"]


@pytest.mark.parametrize(
    ("provider_error", "stored_error"),
    [
        (SteamActivityApiError("RATE_LIMITED", retryable=True), "PROVIDER_RATE_LIMITED"),
        (SteamActivityApiError("PROVIDER_UNAVAILABLE", retryable=True), "PROVIDER_UNAVAILABLE"),
    ],
)
def test_schema_provider_wide_failure_is_not_reported_as_ready_and_stops_fanout(
    configured: tuple[Storage, int],
    provider_error: SteamActivityApiError,
    stored_error: str,
) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = PlayerAchievements(
        10, "ready", (PlayerAchievement("A", True, 1),)
    )
    client.schema[10] = provider_error

    result = sync_achievements(
        storage,
        account_id=account_id,
        steamid="76561198000000001",
        api_key=SecretValue("s"),
        scope="owned",
        explicit_appids=(10, 20, 30),
        max_items=3,
        client=client,
        clock=lambda: NOW,
    )

    assert result.state_counts == {"failed": 1, "unevaluated": 2}
    rows = storage._connection.execute(
        "SELECT state, evaluated, error_code FROM achievement_sync_demand ORDER BY appid"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("failed", 1, stored_error),
        ("unevaluated", 0, stored_error),
        ("unevaluated", 0, stored_error),
    ]
    assert client.schema_calls == [10]
    assert storage._connection.execute(
        "SELECT COUNT(*) FROM achievement_player_current WHERE account_id=?",
        (account_id,),
    ).fetchone()[0] == 0


def test_schema_invalid_response_is_a_per_game_failure_not_unsupported_schema(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = PlayerAchievements(
        10, "ready", (PlayerAchievement("A", True, 1),)
    )
    client.schema[10] = SteamActivityApiError(
        "PROVIDER_RESPONSE_INVALID", retryable=False
    )

    result = sync_achievements(
        storage,
        account_id=account_id,
        steamid="76561198000000001",
        api_key=SecretValue("s"),
        scope="owned",
        explicit_appids=(10,),
        client=client,
        clock=lambda: NOW,
    )

    assert result.state_counts == {"failed": 1}
    item = query_achievements(storage, account_id=account_id, clock=lambda: NOW)[
        "items"
    ][0]
    assert item["state"] == "failed"
    assert item["error_code"] == "PROVIDER_RESPONSE_INVALID"
    assert item["achievements"] == []
    assert storage._connection.execute(
        "SELECT COUNT(*) FROM achievement_schema_status WHERE appid=10"
    ).fetchone()[0] == 0


def test_activity_query_hard_deletes_expired_provider_rows(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    sync_activity(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), client=client, clock=lambda: NOW)
    result = query_activity(storage, account_id=account_id, clock=lambda: NOW + timedelta(days=8))
    assert result["items"] == []
    assert storage._connection.execute("SELECT COUNT(*) FROM activity_observations").fetchone()[0] == 0


def test_future_activity_observation_has_unknown_freshness(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    sync_activity(
        storage,
        account_id=account_id,
        steamid="76561198000000001",
        api_key=SecretValue("s"),
        client=FakeClient(),
        clock=lambda: NOW,
    )

    item = query_activity(
        storage, account_id=account_id, clock=lambda: NOW - timedelta(minutes=1)
    )["items"][0]
    assert item["freshness"] == {
        "activity": "unknown",
        "recent_window": "unknown",
    }


def test_future_achievement_observation_has_unknown_freshness_and_schema_is_not_cached(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = PlayerAchievements(
        10, "ready", (PlayerAchievement("A", True, 1),)
    )
    client.schema[10] = GameAchievementSchema(
        10,
        "ready",
        "english",
        (AchievementDefinition("A", "A", None, False),),
    )
    sync_achievements(
        storage,
        account_id=account_id,
        steamid="76561198000000001",
        api_key=SecretValue("s"),
        scope="owned",
        explicit_appids=(10,),
        client=client,
        clock=lambda: NOW,
    )

    earlier = NOW - timedelta(minutes=1)
    item = query_achievements(
        storage, account_id=account_id, clock=lambda: earlier
    )["items"][0]
    assert item["freshness"] == "unknown"
    assert item["summary"]["freshness"] == "unknown"
    assert storage.read_cached_achievement_schema(10, now=earlier) is None


def test_account_deletion_removes_orphan_public_schema(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = PlayerAchievements(10, "ready", (PlayerAchievement("A", True, 1),))
    client.schema[10] = GameAchievementSchema(10, "ready", "english", (AchievementDefinition("A", "A", None, False),))
    sync_achievements(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), scope="owned", explicit_appids=(10,), client=client, clock=lambda: NOW)
    storage.delete_steam_account_data(account_id)
    assert storage._connection.execute("SELECT COUNT(*) FROM achievement_schema_current").fetchone()[0] == 0
    assert storage._connection.execute("SELECT COUNT(*) FROM achievement_schema_status").fetchone()[0] == 0


def test_fresh_schema_cache_is_reused_without_hiding_prior_subjects(configured: tuple[Storage, int]) -> None:
    storage, account_id = configured
    client = FakeClient()
    for appid in (10, 20):
        client.player[appid] = PlayerAchievements(appid, "ready", (PlayerAchievement("A", True, 1),))
        client.schema[appid] = GameAchievementSchema(appid, "ready", "english", (AchievementDefinition("A", "A", None, False),))
    sync_achievements(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), scope="owned", explicit_appids=(10,), client=client, clock=lambda: NOW)
    sync_achievements(storage, account_id=account_id, steamid="76561198000000001", api_key=SecretValue("s"), scope="owned", explicit_appids=(10, 20), client=client, clock=lambda: NOW + timedelta(hours=1))
    assert client.schema_calls == [10, 20]
    items = query_achievements(storage, account_id=account_id, clock=lambda: NOW + timedelta(hours=1))["items"]
    assert [item["appid"] for item in items] == [10, 20]


def test_storage_rejects_behavioral_collection_without_current_consent() -> None:
    storage = Storage(":memory:")
    account = storage.configure_steam_account(
        alias="primary", steam_id64="76561198000000001", configured_at=NOW
    )
    client = FakeClient()
    with pytest.raises(InvalidSyncTransition, match="consent"):
        sync_activity(
            storage,
            account_id=account.id,
            steamid=account.provider_account_id,
            api_key=SecretValue("s"),
            client=client,
            clock=lambda: NOW,
        )
    storage.record_activity_data_consent(
        account_id=account.id,
        disclosure_version="obsolete",
        accepted_at=NOW,
        backups_acknowledged=True,
    )
    with pytest.raises(InvalidSyncTransition, match="consent"):
        sync_achievements(
            storage,
            account_id=account.id,
            steamid=account.provider_account_id,
            api_key=SecretValue("s"),
            scope="owned",
            explicit_appids=(10,),
            client=client,
            clock=lambda: NOW,
        )
    storage.close()


def test_request_gate_failure_terminalizes_achievement_demand(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured

    def throttled() -> None:
        raise ActivitySyncError("REQUEST_THROTTLED", retryable=True)

    with pytest.raises(ActivitySyncError, match="REQUEST_THROTTLED"):
        sync_achievements(
            storage,
            account_id=account_id,
            steamid="76561198000000001",
            api_key=SecretValue("s"),
            scope="owned",
            explicit_appids=(10, 20),
            max_items=2,
            client=FakeClient(),
            request_gate=throttled,
            clock=lambda: NOW,
        )
    rows = storage._connection.execute(
        "SELECT state, evaluated, error_code FROM achievement_sync_demand ORDER BY appid"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("failed", 1, "REQUEST_THROTTLED"),
        ("unevaluated", 0, "REQUEST_THROTTLED"),
    ]
    latest = storage.read_achievement_snapshot(account_id)["latest"]
    assert latest is not None and latest.status == "complete"


def test_achievement_query_hard_deletes_expired_account_evidence(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    client = FakeClient()
    client.player[10] = PlayerAchievements(
        10, "ready", (PlayerAchievement("A", True, 1),)
    )
    client.schema[10] = GameAchievementSchema(
        10, "ready", "english", (AchievementDefinition("A", "A", None, False),)
    )
    sync_achievements(
        storage,
        account_id=account_id,
        steamid="76561198000000001",
        api_key=SecretValue("s"),
        scope="owned",
        explicit_appids=(10,),
        client=client,
        clock=lambda: NOW,
    )
    result = query_achievements(
        storage, account_id=account_id, clock=lambda: NOW + timedelta(days=8)
    )
    assert result["items"] == []
    for table in (
        "sync_runs",
        "achievement_sync_demand",
        "achievement_player_observations",
        "achievement_player_current",
    ):
        assert storage._connection.execute(
            f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed test table names
        ).fetchone()[0] == 0
    assert storage._connection.execute(
        "SELECT COUNT(*) FROM achievement_schema_current"
    ).fetchone()[0] == 1


def test_late_older_activity_completion_cannot_replace_newer_projection(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    older = storage.begin_activity_sync(
        account_id=account_id,
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        started_at=NOW,
    )
    newer = storage.begin_activity_sync(
        account_id=account_id,
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        started_at=NOW + timedelta(seconds=1),
    )

    def game(minutes: int) -> dict[str, object]:
        return {"appid": 10, "playtime_forever_minutes": minutes}

    storage.complete_activity_snapshot(
        newer.id,
        account_id=account_id,
        games=(game(200),),
        observed_at=NOW + timedelta(seconds=1),
        recent_observed_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=2),
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
    )
    completed_older = storage.complete_activity_snapshot(
        older.id,
        account_id=account_id,
        games=(game(100),),
        observed_at=NOW,
        recent_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=3),
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
    )
    assert completed_older.promoted is False
    item = query_activity(storage, account_id=account_id, clock=lambda: NOW)["items"][0]
    assert item["playtime"]["lifetime_minutes"] == 200


def test_late_older_achievement_result_cannot_cross_join_with_newer_demand(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    older = storage.begin_achievement_sync(
        account_id=account_id,
        candidates=(10,),
        targeted=(10,),
        started_at=NOW,
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
    )
    newer = storage.begin_achievement_sync(
        account_id=account_id,
        candidates=(10,),
        targeted=(10,),
        started_at=NOW + timedelta(seconds=1),
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
    )
    common = {
        "account_id": account_id,
        "appid": 10,
        "state": "ready",
        "schema_state": "ready",
        "error_code": None,
        "write_schema": True,
        "disclosure_version": ACTIVITY_DISCLOSURE_VERSION,
    }
    storage.record_achievement_result(
        newer.id,
        player=({"api_name": "NEW", "achieved": True, "unlock_time_unix": 2},),
        schema=({"api_name": "NEW", "display_name": "New", "description": None, "hidden": False},),
        observed_at=NOW + timedelta(seconds=1),
        **common,
    )
    storage.finish_achievement_sync(newer.id, completed_at=NOW + timedelta(seconds=2))
    storage.record_achievement_result(
        older.id,
        player=({"api_name": "OLD", "achieved": True, "unlock_time_unix": 1},),
        schema=({"api_name": "OLD", "display_name": "Old", "description": None, "hidden": False},),
        observed_at=NOW,
        **common,
    )
    storage.finish_achievement_sync(older.id, completed_at=NOW + timedelta(seconds=3))
    item = query_achievements(storage, account_id=account_id, clock=lambda: NOW)["items"][0]
    assert item["state"] == "ready"
    assert [achievement["api_name"] for achievement in item["achievements"]] == ["NEW"]
