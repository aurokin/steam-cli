from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from steam_agent import cli
from steam_agent.activity import ACTIVITY_DISCLOSURE_VERSION
from steam_agent.storage import (
    CatalogObservation,
    CatalogPageInput,
    CatalogStreamInput,
    EvidenceInput,
    InstalledObservation,
    Machine,
    OwnedObservation,
    Storage,
)


NOW = datetime(2026, 7, 12, 5, tzinfo=timezone.utc)
T0 = "2026-07-12T04:00:00Z"
T1 = "2026-07-12T04:01:00Z"


def invoke(tmp_path, capsys, *args: str):
    code = cli.main(["--data-dir", str(tmp_path), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out.startswith("{") else captured.out, captured.err


def test_recommendation_query_help_explains_semantic_choices(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["recommendations", "query", "--help"])

    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Select the cached machine key (default: local)." in help_text
    assert "default and only supported value: owned" in help_text
    assert "resume/0.1 to continue something" in help_text
    assert "finishability/0.1 for bounded finishing evidence" in help_text
    assert "preference-fit/0.1 for explicit preferences" in help_text
    assert "exclude filters those candidates" in help_text
    assert "include retains them as conditional (default: exclude)" in help_text
    assert "Record explain=true in the returned context" in help_text
    assert "ranking is unchanged" in help_text


def stream(kind: str) -> CatalogStreamInput:
    games = kind == "games"
    return CatalogStreamInput(
        stream=kind,  # type: ignore[arg-type]
        termination="demand_boundary",
        scanned_through_appid=30,
        filter_context={
            "include_games": games,
            "include_dlc": not games,
            "include_software": not games,
            "include_videos": not games,
            "include_hardware": not games,
        },
        pages=(CatalogPageInput(1, 0, 10, 30, 3, True, T0),),
    )


def populated(tmp_path) -> int:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.configure_steam_account(
            alias="primary", steam_id64="76561198000000000", configured_at=T0
        )
        storage.record_owned_data_consent(
            account_id=account.id, disclosure_version="2026-07-11.m2",
            accepted_at=T0, backups_acknowledged=True,
        )
        owned_run = storage.begin_sync(
            provider="steam_web_api", capability="owned.visible.read",
            account_id=account.id, started_at=T0,
        )
        storage.complete_owned_snapshot(
            owned_run.id,
            tuple(
                OwnedObservation(
                    appid=appid, name=f"Game {appid}", playtime_forever_minutes=minutes,
                    inclusion_basis="visible_owned", observed_at=T0,
                )
                for appid, minutes in ((10, 300), (20, 60), (30, 0))
            ),
            base_retrieved_at=T0, expanded_retrieved_at=T0,
            base_reported_count=3, expanded_reported_count=3, completed_at=T1,
        )
        storage.upsert_machine(Machine("local", "Local", "linux", "x86_64"), observed_at=T0)
        installed_run = storage.begin_sync(
            provider="local_steam", capability="installed", machine_id="local", started_at=T0
        )
        storage.record_installed_observation(
            installed_run.id,
            InstalledObservation(
                appid=10, name="Game 10", app_type="game", library_root="/private",
                install_dir="game10", state="installed", observed_at=T0,
            ),
            EvidenceInput(
                provider="local_steam", capability="installed", source_kind="local_file",
                source_locator="appmanifest_10.acf", retrieved_at=T0,
                support_level="core", context={"machine_id": "local"}, payload={"appid": 10},
            ),
        )
        storage.finish_installed_sync(installed_run.id, status="complete", completed_at=T1)
        catalog_run = storage.begin_catalog_sync(
            provider="steam_store_web_api", account_id=account.id, machine_id="local",
            demanded_appids=[10, 20, 30], started_at=T0,
        )
        storage.complete_catalog_snapshot(
            catalog_run.id, [10, 20, 30],
            [CatalogObservation(10, "game"), CatalogObservation(20, "non_game"), CatalogObservation(30, "not_observed")],
            games=stream("games"), non_games=stream("non_games"), completed_at=T1,
        )
        storage.record_activity_data_consent(
            account_id=account.id, disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            accepted_at=T0, backups_acknowledged=True,
        )
        activity_run = storage.begin_activity_sync(
            account_id=account.id, disclosure_version=ACTIVITY_DISCLOSURE_VERSION, started_at=T0
        )
        storage.complete_activity_snapshot(
            activity_run.id, account_id=account.id,
            games=({"appid": 10, "playtime_forever_minutes": 300, "recent_window_minutes": 45, "last_played_unix": 1783830600},),
            observed_at=T0, recent_observed_at=T0, completed_at=T1,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        achievement_run = storage.begin_achievement_sync(
            account_id=account.id, candidates=(10, 20, 30), targeted=(10,), started_at=T0,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.record_achievement_result(
            achievement_run.id, account_id=account.id, appid=10, state="ready",
            player=({"api_name": "A", "achieved": True, "unlock_time_unix": 1}, {"api_name": "B", "achieved": False, "unlock_time_unix": None}),
            schema_state="ready", schema=({"api_name": "A", "display_name": "A", "description": None, "hidden": False}, {"api_name": "B", "display_name": "B", "description": None, "hidden": False}),
            observed_at=T0, disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.finish_achievement_sync(achievement_run.id, completed_at=T1)
        storage.set_explicit_feedback_field(
            account_id=account.id, appid=10, field_name="rating", value="liked", recorded_at=T1
        )
        storage.set_explicit_feedback_field(
            account_id=account.id, appid=20, field_name="rating", value="disliked", recorded_at=T1
        )
        return account.id


def test_full_cache_only_tracer_is_deterministic_and_truthful(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    populated(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    command = (
        "recommendations", "query", "--account", "primary", "--machine", "local",
        "--recipe", "resume/0.1", "--require", "installed=true", "--unknown", "include", "--explain",
    )
    first = invoke(tmp_path, capsys, *command)
    second = invoke(tmp_path, capsys, *command)
    assert first == second
    code, result, stderr = first
    assert code == 0 and stderr == ""
    assert result["data"]["schema"] == "recommendations/0.1"
    assert result["context"]["cache_only"] is True
    assert result["completeness"]["status"] == "partial"
    assert "catalog.classification" in result["completeness"]["missing_capabilities"]
    by_appid = {item["appid"]: item for item in result["data"]["results"]}
    assert by_appid[10]["eligibility"] == "eligible"
    assert by_appid[20]["eligibility"] == "excluded"  # known non-game and not installed
    assert by_appid[30]["eligibility"] == "excluded"  # unknown gates honor policy/evidence
    assert next(gate for gate in by_appid[20]["gates"] if gate["name"] == "game")["original"] == "fail"
    encoded = json.dumps(result)
    assert "76561198000000000" not in encoded and str(tmp_path) not in encoded
    assert "/private" not in encoded


def test_recommendation_only_read_hard_deletes_expired_behavioral_evidence(
    tmp_path,
) -> None:
    account_id = populated(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        snapshot = storage.read_recommendation_snapshot(
            account_id, "local", now=NOW + timedelta(days=8)
        )

        assert snapshot.activity_items == ()
        assert snapshot.activity_latest is None
        assert snapshot.activity_latest_complete is None
        assert snapshot.achievement_items == ()
        assert snapshot.achievement_latest is None
        for table in (
            "activity_observations",
            "activity_current",
            "achievement_sync_demand",
            "achievement_player_observations",
            "achievement_player_current",
        ):
            assert storage._connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed test table names
            ).fetchone()[0] == 0
        assert storage._connection.execute(
            """SELECT COUNT(*) FROM sync_runs
               WHERE capability IN ('activity.read', 'achievements.read')"""
        ).fetchone()[0] == 0


def test_recommendation_snapshot_preserves_last_good_achievement_aggregate(
    tmp_path,
) -> None:
    account_id = populated(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        failed = storage.begin_achievement_sync(
            account_id=account_id,
            candidates=(10,),
            targeted=(10,),
            started_at="2026-07-12T04:02:00Z",
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.record_achievement_result(
            failed.id,
            account_id=account_id,
            appid=10,
            state="failed",
            player=(),
            schema_state="achievements_not_supported",
            schema=(),
            observed_at="2026-07-12T04:02:00Z",
            error_code="PROVIDER_UNAVAILABLE",
            write_schema=False,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.finish_achievement_sync(
            failed.id, completed_at="2026-07-12T04:03:00Z"
        )

        snapshot = storage.read_recommendation_snapshot(
            account_id, "local", now=NOW
        )

    row = next(item for item in snapshot.achievement_items if item["appid"] == 10)
    assert row["state"] == "failed"
    assert row["unlocked"] == 1
    assert row["total"] == 2
    assert row["player_sync_run_id"] != row["sync_run_id"]
    assert row["player_observed_at"] == T0


def test_recommendation_query_accepts_ready_zero_achievement_aggregate(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = populated(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        run = storage.begin_achievement_sync(
            account_id=account_id,
            candidates=(30,),
            targeted=(30,),
            started_at="2026-07-12T04:02:00Z",
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.record_achievement_result(
            run.id,
            account_id=account_id,
            appid=30,
            state="ready",
            player=(),
            schema_state="achievements_not_supported",
            schema=(),
            observed_at="2026-07-12T04:02:00Z",
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.finish_achievement_sync(run.id, completed_at="2026-07-12T04:03:00Z")
        private = storage.begin_achievement_sync(
            account_id=account_id,
            candidates=(30,),
            targeted=(30,),
            started_at="2026-07-12T04:04:00Z",
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.record_achievement_result(
            private.id,
            account_id=account_id,
            appid=30,
            state="profile_not_public",
            player=(),
            schema_state="achievements_not_supported",
            schema=(),
            observed_at="2026-07-12T04:04:00Z",
            write_schema=False,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.finish_achievement_sync(
            private.id, completed_at="2026-07-12T04:05:00Z"
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, result, stderr = invoke(
        tmp_path,
        capsys,
        "recommendations",
        "query",
        "--account",
        "primary",
        "--recipe",
        "finishability/0.1",
        "--unknown",
        "include",
    )

    assert code == 0 and stderr == ""
    assert any(item["appid"] == 30 for item in result["data"]["results"])
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        snapshot = storage.read_recommendation_snapshot(
            account_id, "local", now=NOW
        )
    row = next(item for item in snapshot.achievement_items if item["appid"] == 30)
    assert row["state"] == "profile_not_public"
    assert row["unlocked"] == 0
    assert row["total"] == 0
    assert row["player_sync_run_id"] != row["sync_run_id"]
    assert row["player_observed_at"] == "2026-07-12T04:02:00Z"


def test_feedback_components_keep_field_specific_event_lineage(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = populated(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        rating = dict(storage.list_explicit_feedback(account_id)[0].field_event_ids)["rating"]
        snooze = storage.set_explicit_feedback_field(
            account_id=account_id, appid=10, field_name="snooze",
            value="2026-07-13T05:00:00Z", recorded_at="2026-07-12T04:03:00Z",
        )
    result = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "preference-fit/0.1", "--unknown", "include",
    )[1]
    item = next(item for item in result["data"]["results"] if item["appid"] == 10)
    rating_component = next(component for component in item["components"] if component["rule_id"] == "explicit_like")
    snooze_gate = next(gate for gate in item["gates"] if gate["name"] == "snoozed")
    assert rating_component["evidence_ids"] == [f"feedback-event:{rating}"]
    assert snooze_gate["evidence_ids"] == [f"feedback-event:{snooze.event_id}"]


def test_feedback_mutation_no_eligible_table_and_deletion(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = populated(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    base = ("recommendations", "query", "--account", "primary", "--recipe", "preference-fit/0.1", "--unknown", "include")
    before = invoke(tmp_path, capsys, *base)[1]
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.set_explicit_feedback_field(account_id=account_id, appid=10, field_name="rating", value="disliked", recorded_at="2026-07-12T04:02:00Z")
    after = invoke(tmp_path, capsys, *base)[1]
    assert before["data"]["results"][0]["score"] != after["data"]["results"][0]["score"]
    code, table, stderr = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1", "--require", "user:impossible=true", "--format", "table",
    )
    assert code == 0 and stderr == "" and "ELIGIBILITY" in table
    assert "\teligible\t" not in table and "\tconditional\t" not in table
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.delete_steam_account_data(account_id)
    deleted = invoke(tmp_path, capsys, *base)[1]
    assert deleted["completeness"]["status"] == "unavailable"
    assert deleted["data"]["results"] == []


def test_failed_last_attempt_and_stale_evidence_remain_distinct(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = populated(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        failed = storage.begin_activity_sync(
            account_id=account_id, disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            started_at="2026-07-12T04:02:00Z",
        )
        storage.finish_activity_sync_failed(
            failed.id, error_code="PROVIDER_UNAVAILABLE", completed_at="2026-07-12T04:03:00Z"
        )
    monkeypatch.setattr(
        cli, "_utc_now", lambda: datetime(2026, 7, 13, 5, tzinfo=timezone.utc)
    )
    result = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1", "--unknown", "include",
    )[1]
    assert result["data"]["snapshots"]["activity_last_attempt_status"] == "failed"
    assert "activity.read" in result["completeness"]["stale_capabilities"]
    game = next(item for item in result["data"]["results"] if item["appid"] == 10)
    assert game["freshness"] == "stale"
    unknown = next(item for item in result["data"]["results"] if item["appid"] == 30)
    assert "achievements_unevaluated" in unknown["unknowns"]


def test_recent_window_stops_scoring_after_one_hour(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    populated(tmp_path)
    monkeypatch.setattr(
        cli, "_utc_now", lambda: datetime(2026, 7, 12, 6, tzinfo=timezone.utc)
    )
    result = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1", "--unknown", "include",
    )[1]
    game = next(item for item in result["data"]["results"] if item["appid"] == 10)
    recent = next(component for component in game["components"] if component["rule_id"] == "recent_sustained_play")
    assert recent["state"] == "stale" and recent["points"] is None
    lifetime = next(component for component in game["components"] if component["rule_id"] == "lifetime_play_momentum")
    assert lifetime["state"] == "applied"


def test_stale_catalog_cannot_pass_game_gate(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    populated(tmp_path)
    monkeypatch.setattr(
        cli, "_utc_now", lambda: datetime(2026, 7, 13, 5, 2, tzinfo=timezone.utc)
    )
    result = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "preference-fit/0.1", "--unknown", "include",
    )[1]
    game = next(item for item in result["data"]["results"] if item["appid"] == 10)
    gate = next(gate for gate in game["gates"] if gate["name"] == "game")
    assert gate["original"] == "unknown"
    assert "catalog.classification" in result["completeness"]["stale_capabilities"]
    assert "owned.visible.read" in result["completeness"]["stale_capabilities"]


def test_fresh_running_refresh_is_informational_but_failed_catalog_is_stale(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = populated(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.begin_sync(
            provider="steam_web_api", capability="owned.visible.read",
            account_id=account_id, started_at="2026-07-12T04:59:00Z",
        )
        storage.begin_activity_sync(
            account_id=account_id, disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            started_at="2026-07-12T04:59:00Z",
        )
        storage.begin_sync(
            provider="local_steam", capability="installed", machine_id="local",
            started_at="2026-07-12T04:59:00Z",
        )
        catalog = storage.begin_catalog_sync(
            provider="steam_store_web_api", account_id=account_id, machine_id="local",
            demanded_appids=[10, 20, 30], started_at="2026-07-12T04:59:00Z",
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    running = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1", "--require", "installed=true", "--unknown", "include",
    )[1]
    assert not {"owned.visible.read", "activity.read", "installed.read", "catalog.application.read"} & set(running["completeness"]["stale_capabilities"])
    assert "SYNC_IN_PROGRESS" in {item["code"] for item in running["completeness"]["warnings"]}
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.finish_catalog_sync(
            catalog.id, status="failed", completed_at="2026-07-12T05:01:00Z",
            error_code="PROVIDER_UNAVAILABLE",
        )
    failed = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1", "--unknown", "include",
    )[1]
    assert "catalog.application.read" in failed["completeness"]["stale_capabilities"]


@pytest.mark.parametrize(
    "arguments",
    [
        ("--require", "installed=yes"),
        ("--require", "installed=true", "--require", "installed=false"),
        ("--override", "appid:10:owned=maybe"),
        ("--override", "appid:10:owned=pass", "--override", "appid:10:owned=fail"),
        ("--time-minutes", "-1"),
    ],
)
def test_recommendation_filters_are_strict(tmp_path, capsys, arguments: tuple[str, ...]) -> None:
    populated(tmp_path)
    code, result, _ = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1", *arguments,
    )
    assert code == 2 and result["error"]["code"] == "INVALID_ARGUMENT"


def test_fresh_profile_is_truthfully_unavailable(tmp_path, capsys) -> None:
    code, result, _ = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1",
    )
    assert code == 0
    assert result["completeness"]["status"] == "unavailable"
    assert result["data"]["empty"] is False


def test_valid_empty_owned_snapshot_is_complete_without_enrichment(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.configure_steam_account(
            alias="primary", steam_id64="76561198000000000", configured_at=T0
        )
        storage.record_owned_data_consent(
            account_id=account.id, disclosure_version="2026-07-11.m2",
            accepted_at=T0, backups_acknowledged=True,
        )
        run = storage.begin_sync(
            provider="steam_web_api", capability="owned.visible.read",
            account_id=account.id, started_at=T0,
        )
        storage.complete_owned_snapshot(
            run.id, (), base_retrieved_at=T0, expanded_retrieved_at=T0,
            base_reported_count=0, expanded_reported_count=0, completed_at=T1,
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    result = invoke(
        tmp_path, capsys, "recommendations", "query", "--account", "primary",
        "--recipe", "resume/0.1", "--require", "installed=true",
    )[1]
    assert result["completeness"]["status"] == "complete"
    assert result["data"]["empty"] is True
    assert result["data"]["results"] == []
