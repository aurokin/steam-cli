from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.storage import (
    EvidenceInput,
    InstalledObservation,
    Machine,
    OwnedObservation,
    Storage,
)
from steam_agent.steam_declared_facts import DECLARED_FACTS_DISCLOSURE_VERSION


NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def invoke(tmp_path: Path, capsys: object, *arguments: str):
    code = cli.main(["--data-dir", str(tmp_path), *arguments])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def declared_payload(appid: int) -> dict[str, object]:
    return {
        "schema_id": "declared-app-facts/0.2",
        "appid": appid,
        "context": {"country": "US", "language": "english"},
        "platforms": {
            "state": "declared",
            "windows": False,
            "macos": False,
            "linux": True,
        },
        "requirements": [
            {
                "platform": platform,
                "state": "declared" if platform == "linux" else "undeclared",
                "minimum": "Storage: 1 GB available space" if platform == "linux" else None,
                "recommended": None,
            }
            for platform in ("linux", "macos", "windows")
        ],
        "languages": {"state": "undeclared", "items": [], "unrecognized_count": 0},
        "categories": {
            "state": "undeclared",
            "known_slugs": [],
            "unknown_ids": [],
            "source": "steam_store_appdetails",
            "numeric_ids": [],
        },
        "genres": {
            "state": "undeclared",
            "source": "steam_store_appdetails",
            "items": [],
        },
        "coming_soon": {"state": "unknown", "localized_date_display": None},
        "controller_support": None,
        "external_account_notice": {"state": "unknown", "text": None},
        "drm_notice": {"state": "unknown", "text": None},
        "source": {
            "provider": "steam_store",
            "support_level": "provisional",
            "source_locator": "steam_store_appdetails",
            "human_reference_url": (
                f"https://store.steampowered.com/app/{appid}/?cc=US&l=english"
            ),
            "access_mode": "manual_only",
            "automation_supported": False,
        },
    }


def seed(tmp_path: Path) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("local", "Private workstation", "linux", "x86_64"),
            observed_at=NOW,
        )
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198999999999",
            configured_at=NOW,
        )
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="owned-visible-v1",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        owned_run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at=NOW,
        )
        storage.complete_owned_snapshot(
            owned_run.id,
            (
                OwnedObservation(10, 10, "visible_owned", NOW, "Installed private title"),
                OwnedObservation(20, 0, "visible_owned", NOW, "Travel private title"),
                OwnedObservation(30, 5, "played_free", NOW, "Played free title"),
            ),
            base_retrieved_at=NOW,
            expanded_retrieved_at=NOW,
            base_reported_count=2,
            expanded_reported_count=3,
            completed_at=NOW,
        )
        installed_run = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=NOW,
        )
        storage.record_installed_observation(
            installed_run.id,
            InstalledObservation(
                appid=10,
                library_root="/private/library",
                install_dir="/private/library/steamapps/common/private-title",
                observed_at=NOW,
                name="Installed private title",
                build_id="1234",
                size_bytes=2_000_000_000,
                manifest_path="/private/library/steamapps/appmanifest_10.acf",
                manifest_mtime=NOW,
            ),
            EvidenceInput(
                provider="local_steam",
                capability="installed",
                source_kind="local_file",
                source_locator="/private/library/steamapps/appmanifest_10.acf",
                retrieved_at=NOW,
                support_level="local_heuristic",
                payload={"private": "must-not-escape"},
            ),
        )
        storage.finish_installed_sync(
            installed_run.id, status="complete", completed_at=NOW
        )
        storage.record_compatibility_data_consent(
            account_id=account.id,
            disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        declared_run, _, _ = storage.begin_declared_app_sync(
            account_id=account.id,
            machine_id="local",
            demanded_appids=[10, 20],
            country="US",
            language="english",
            max_items=10,
            skip_fresh_terminal=True,
            started_at=NOW,
            disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
        )
        for appid in (10, 20):
            storage.record_declared_app_result(
                declared_run.id,
                account_id=account.id,
                appid=appid,
                state="ready",
                observed_at=NOW,
                facts=declared_payload(appid),
            )
        storage.finish_declared_app_sync(declared_run.id, completed_at=NOW)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)


def test_observe_and_reclaim_are_path_free_cache_only_queries(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    database = tmp_path / "steam-agent.sqlite3"
    before = hashlib.sha256(database.read_bytes()).digest()

    code, observed, stderr = invoke(
        tmp_path, capsys, "operations", "observe", "--machine", "local"
    )
    assert code == 0
    assert stderr == ""
    assert observed["data"]["schema"] == "local-operation-state/0.1"
    assert observed["data"]["items"][0]["size_on_disk_bytes"]["value"] == 2_000_000_000
    assert observed["data"]["unsupported_capabilities"]["runtime"] == {
        "availability": "unavailable",
        "reason": "adapter_not_implemented",
    }

    code, ranked, stderr = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "reclaim-space/0.1",
        "--machine",
        "local",
        "--target-bytes",
        "1000000000",
        "--limit",
        "10",
    )
    assert code == 0
    assert stderr == ""
    assert ranked["data"]["results"][0]["appid"] == 10
    assert ranked["data"]["results"][0]["meets_target_alone"] is True
    rendered = json.dumps([observed, ranked]).casefold()
    assert "/private" not in rendered
    assert "stateflags" not in rendered
    assert hashlib.sha256(database.read_bytes()).digest() == before
    assert not (tmp_path / "steam-agent.sqlite3-wal").exists()
    assert not (tmp_path / "steam-agent.sqlite3-shm").exists()


def test_travel_rank_excludes_played_free_and_never_promises_install(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "travel-install/0.1",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--budget-bytes",
        "2000000000",
        "--limit",
        "10",
    )
    assert code == 0
    assert stderr == ""
    results = {item["appid"]: item for item in value["data"]["results"]}
    assert set(results) == {10, 20}
    assert results[10]["eligibility"] == "excluded"
    assert results[20]["eligibility"] == "conditional"
    assert results[20]["declared_minimum_storage_upper_bytes"] == 1 << 30
    assert results[20]["actual_install_footprint"] == "unknown"
    assert results[20]["download_bytes"] == "unknown"
    assert results[20]["download_time"] == "unknown"


def test_plan_is_inert_uses_alias_and_returns_only_official_https(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "operations",
        "plan",
        "verify",
        "10",
        "--account",
        "primary",
        "--machine",
        "local",
    )
    assert code == 0
    assert stderr == ""
    plan = value["data"]["plan"]
    assert plan["schema"] == "operation-plan/0.1"
    assert plan["target"]["account_alias"] == "primary"
    assert "account_id" not in plan["target"]
    assert plan["capability_policy"]["execution"] == "prohibited"
    assert plan["capability_policy"]["execution_authorized"] is False
    assert value["completeness"]["warnings"] == []
    assert all(
        item["url"].startswith("https://") for item in plan["human_open_references"]
    )
    rendered = json.dumps(value).casefold()
    assert "steam://" not in rendered
    assert "/private" not in rendered


def test_install_plan_does_not_promote_visible_ownership_to_license(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "operations",
        "plan",
        "install",
        "20",
        "--account",
        "primary",
        "--machine",
        "local",
    )
    assert code == 0
    assert stderr == ""
    preconditions = {
        item["code"]: item for item in value["data"]["plan"]["preconditions"]
    }
    assert preconditions["license_available"] == {
        "code": "license_available",
        "state": "unknown",
        "detail_code": "visible_owned_does_not_establish_license",
    }
    assert preconditions["not_installed"]["state"] == "pass"


def test_move_requires_bounded_destination_and_other_recipe_args_are_exact(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    code, value, _ = invoke(
        tmp_path,
        capsys,
        "operations",
        "plan",
        "move",
        "10",
        "--account",
        "primary",
        "--machine",
        "local",
    )
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"

    code, value, _ = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "reclaim-space/0.1",
        "--machine",
        "local",
        "--budget-bytes",
        "1",
        "--limit",
        "1",
    )
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_m7_table_output_is_path_free_and_labels_conditional_state(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    code = cli.main(
        [
            "--data-dir",
            str(tmp_path),
            "storage",
            "rank",
            "--recipe",
            "travel-install/0.1",
            "--account",
            "primary",
            "--machine",
            "local",
            "--country",
            "US",
            "--language",
            "english",
            "--budget-bytes",
            "2000000000",
            "--limit",
            "10",
            "--format",
            "table",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert code == 0
    assert captured.err == ""
    assert "conditional" in captured.out
    assert "/private" not in captured.out.casefold()


def test_newer_failed_scan_retains_last_good_but_marks_operations_stale(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        run = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=NOW,
        )
        storage.finish_installed_sync(run.id, status="failed", completed_at=NOW)

    code, value, stderr = invoke(
        tmp_path, capsys, "operations", "observe", "--machine", "local"
    )
    assert code == 0
    assert stderr == ""
    assert value["completeness"]["status"] == "partial"
    assert value["data"]["items"][0]["installed"]["state"] == "present"
    assert value["data"]["items"][0]["installed"]["freshness"] == "stale"


def test_complete_empty_travel_scope_is_successful_empty_result(
    tmp_path: Path, capsys: object
) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("local", "Machine", "linux", "x86_64"), observed_at=NOW
        )
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198999999999",
            configured_at=NOW,
        )
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="owned-visible-v1",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        owned = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at=NOW,
        )
        storage.complete_owned_snapshot(
            owned.id,
            (),
            base_retrieved_at=NOW,
            expanded_retrieved_at=NOW,
            base_reported_count=0,
            expanded_reported_count=0,
            completed_at=NOW,
        )
        installed = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=NOW,
        )
        storage.finish_installed_sync(
            installed.id, status="complete", completed_at=NOW
        )

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "travel-install/0.1",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--budget-bytes",
        "1",
        "--limit",
        "10",
    )
    assert code == 0
    assert stderr == ""
    assert value["data"]["results"] == []
    assert value["data"]["counts"]["candidates"] == 0


def test_old_complete_empty_installed_snapshot_is_stale_not_current_empty(
    tmp_path: Path, capsys: object
) -> None:
    old = NOW - timedelta(minutes=16)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("local", "Machine", "linux", "x86_64"), observed_at=old
        )
        run = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=old,
        )
        storage.finish_installed_sync(run.id, status="complete", completed_at=old)

    code, value, stderr = invoke(
        tmp_path, capsys, "operations", "observe", "--machine", "local"
    )
    assert code == 0
    assert stderr == ""
    assert value["data"]["items"] == []
    assert value["completeness"]["status"] == "partial"
    assert value["completeness"]["stale_capabilities"] == ["operations.local.read"]


@pytest.mark.parametrize("budget", ["0", "-1", str(1 << 63)])
def test_invalid_travel_budget_is_invalid_argument_before_cache_read(
    tmp_path: Path, capsys: object, budget: str
) -> None:
    code, value, _ = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "travel-install/0.1",
        "--account",
        "primary",
        "--machine",
        "missing",
        "--country",
        "US",
        "--language",
        "english",
        "--budget-bytes",
        budget,
        "--limit",
        "10",
    )
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_travel_machine_is_invalid_argument(
    tmp_path: Path, capsys: object
) -> None:
    seed(tmp_path)
    code, value, _ = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "travel-install/0.1",
        "--account",
        "primary",
        "--machine",
        "missing",
        "--country",
        "US",
        "--language",
        "english",
        "--budget-bytes",
        "1",
        "--limit",
        "10",
    )
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "arguments",
    [
        ("operations", "observe", "--machine", "missing"),
        (
            "storage",
            "rank",
            "--recipe",
            "reclaim-space/0.1",
            "--machine",
            "missing",
            "--target-bytes",
            "1",
            "--limit",
            "10",
        ),
    ],
)
def test_missing_installed_query_machine_is_invalid_argument(
    tmp_path: Path, capsys: object, arguments: tuple[str, ...]
) -> None:
    seed(tmp_path)
    code, value, _ = invoke(tmp_path, capsys, *arguments)
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_first_owned_snapshot_is_unavailable_not_empty_success(
    tmp_path: Path, capsys: object
) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("local", "Machine", "linux", "x86_64"), observed_at=NOW
        )
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198999999999",
            configured_at=NOW,
        )
        installed = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=NOW,
        )
        storage.finish_installed_sync(
            installed.id, status="complete", completed_at=NOW
        )

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "travel-install/0.1",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--budget-bytes",
        "1",
        "--limit",
        "10",
    )
    assert code == 0
    assert stderr == ""
    assert value["data"]["results"] == []
    assert value["completeness"]["status"] == "unavailable"
    assert value["completeness"]["missing_capabilities"] == ["owned.visible.read"]


def test_stale_declared_storage_cannot_exclude_travel_candidate(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(days=8))
    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "storage",
        "rank",
        "--recipe",
        "travel-install/0.1",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--budget-bytes",
        "1",
        "--limit",
        "10",
    )
    assert code == 0
    assert stderr == ""
    result = next(item for item in value["data"]["results"] if item["appid"] == 20)
    storage_gate = next(
        gate for gate in result["gates"] if gate["name"] == "declared_minimum_storage_fit"
    )
    assert storage_gate["state"] == "unknown"
    assert result["eligibility"] == "conditional"


@pytest.mark.parametrize("ordinal", ["0", "-1", "1025"])
def test_invalid_move_ordinal_is_invalid_argument(
    tmp_path: Path, capsys: object, ordinal: str
) -> None:
    seed(tmp_path)
    code, value, _ = invoke(
        tmp_path,
        capsys,
        "operations",
        "plan",
        "move",
        "10",
        "--account",
        "primary",
        "--machine",
        "local",
        "--destination-library-ordinal",
        ordinal,
    )
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"
