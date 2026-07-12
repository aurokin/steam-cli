from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import socket

import pytest

import steam_agent.cli as cli
from steam_agent.compatibility import CompatibilityTarget
from steam_agent.compatibility_adapter import assess_compatibility_snapshot
from steam_agent.storage import (
    DeclaredAppDemand,
    DeclaredAppProjection,
    DeclaredAppSnapshot,
    DeclaredAppSubject,
    EvidenceInput,
    InstalledObservation,
    Machine,
    OwnedObservation,
    Storage,
    SystemProfileProjection,
    SystemProfileSnapshot,
)


NOW = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)


def invoke(tmp_path: Path, capsys: object, *arguments: str):
    code = cli.main(["--data-dir", str(tmp_path), *arguments])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def configure(tmp_path: Path) -> None:
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


def populated_snapshot(tmp_path: Path):
    configure(tmp_path)
    path = tmp_path / "steam-agent.sqlite3"
    with Storage(path) as storage:
        account = storage.get_account("primary")
        assert account is not None
        owned_run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at=NOW,
        )
        storage.complete_owned_snapshot(
            owned_run.id,
            (OwnedObservation(400, 0, "visible_owned", NOW, "Private title"),),
            base_retrieved_at=NOW,
            expanded_retrieved_at=NOW,
            base_reported_count=1,
            expanded_reported_count=1,
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
                400,
                "/private/library",
                "private-install-dir",
                NOW,
                "Private title",
            ),
            EvidenceInput(
                "local_steam",
                "installed",
                "local_file",
                "appmanifest_400.acf",
                NOW,
                "core",
                {"private": "must-not-escape"},
            ),
        )
        storage.finish_installed_sync(
            installed_run.id, status="complete", completed_at=NOW
        )
        snapshot = storage.read_compatibility_snapshot(
            account.id, "local", "US", "english", [400], NOW
        )

    assert snapshot.installed.latest_complete is not None
    system_run = replace(
        snapshot.installed.latest_complete,
        capability="system.profile.read",
        provider="local_system",
    )
    profile = {
        "schema_id": "system-profile/0.1",
        "os": {"family": {"state": "known", "value": "linux"}},
        "cpu": {"architecture": {"state": "known", "value": "x86_64"}},
        "memory": {"total_bytes": {"state": "known", "value": 16 << 30}},
        "storage": {
            "state": "known",
            "value": [{"available_bytes": 50 << 30}],
        },
        "graphics": {"state": "known", "value": {"models": ["PRIVATE GPU"]}},
    }
    facts = {
        "schema_id": "declared-app-facts/0.1",
        "appid": 400,
        "context": {"country": "US", "language": "english"},
        "platforms": {
            "state": "declared",
            "windows": False,
            "macos": False,
            "linux": True,
        },
        "requirements": [],
        "languages": {"state": "declared", "items": [], "unrecognized_count": 0},
        "categories": {"state": "declared", "known_slugs": [], "unknown_ids": []},
        "controller_support": None,
        "external_account_notice": {"state": "undeclared", "text": None},
        "drm_notice": {"state": "undeclared", "text": None},
        "source": {"support_level": "provisional"},
    }
    current = DeclaredAppProjection(
        400,
        "US",
        "english",
        "declared-app-facts/0.1",
        facts,
        "steam_store",
        "provisional",
        "steam_store_appdetails",
        "https://store.steampowered.com/app/400/",
        "2026-07-12T18:00:00Z",
        7,
    )
    demand = DeclaredAppDemand(
        400,
        0,
        True,
        True,
        "ready",
        None,
        None,
        "2026-07-12T18:00:00Z",
        7,
        "complete",
        "2026-07-12T18:00:00Z",
        "2026-07-12T18:00:00Z",
    )
    return replace(
        snapshot,
        system_profile=SystemProfileSnapshot(
            SystemProfileProjection(
                "system-profile/0.1", profile, "2026-07-12T18:00:00Z", 9, 11
            ),
            system_run,
            system_run,
        ),
        declared_apps=DeclaredAppSnapshot(
            (DeclaredAppSubject(400, current, demand),), system_run
        ),
    )


def forbid(*_: object, **__: object):
    raise AssertionError("cache-only assessment crossed an external boundary")


def test_typed_adapter_maps_system_owned_installed_and_omits_them_for_deck(
    tmp_path: Path,
) -> None:
    snapshot = populated_snapshot(tmp_path)
    machine = assess_compatibility_snapshot(
        snapshot,
        target=CompatibilityTarget("machine", "local", "linux"),
    )
    gates = {gate.name: gate for gate in machine.assessment.results[0].gates}
    assert gates["effective_execution_support"].original == "pass"
    assert gates["readiness:installed"].original == "pass"
    assert gates["readiness:visible_owned"].original == "pass"
    assert machine.completeness.missing_capabilities == ("operations.ready.read",)

    deck = assess_compatibility_snapshot(
        snapshot,
        target=CompatibilityTarget("valve_deck", "steam-deck", "steamos"),
    )
    deck_gates = {gate.name: gate for gate in deck.assessment.results[0].gates}
    assert deck_gates["readiness:installed"].original == "unknown"
    assert deck_gates["readiness:visible_owned"].original == "pass"
    assert deck_gates["exact_target_review"].effective == "unknown"
    assert "system_profile.read" in deck.completeness.missing_capabilities


def test_typed_adapter_preserves_all_run_lineage_for_same_second_attempts(
    tmp_path: Path,
) -> None:
    snapshot = populated_snapshot(tmp_path)
    declared_subject = snapshot.declared_apps.subjects[0]
    declared_demand = replace(
        declared_subject.latest_demand,
        state="failed",
        sync_run_id=8,
        sync_status="failed",
    )
    system_latest = snapshot.system_profile.latest
    installed_latest = snapshot.installed.latest
    owned_latest = snapshot.owned.latest
    assert system_latest is not None
    assert installed_latest is not None
    assert owned_latest is not None
    snapshot = replace(
        snapshot,
        system_profile=replace(
            snapshot.system_profile,
            latest=replace(system_latest, id=10, status="partial", promoted=False),
        ),
        declared_apps=replace(
            snapshot.declared_apps,
            subjects=(replace(declared_subject, latest_demand=declared_demand),),
        ),
        installed=replace(
            snapshot.installed,
            latest=replace(
                installed_latest,
                id=installed_latest.id + 100,
                status="partial",
                promoted=False,
            ),
        ),
        owned=replace(
            snapshot.owned,
            latest=replace(
                owned_latest,
                id=owned_latest.id + 100,
                status="failed",
                promoted=False,
            ),
        ),
    )

    query = assess_compatibility_snapshot(
        snapshot,
        target=CompatibilityTarget("machine", "local", "linux"),
    )
    gates = {gate.name: gate for gate in query.assessment.results[0].gates}
    assert gates["declared_native_build"].original_freshness == "stale"
    assert gates["effective_execution_support"].original_freshness == "stale"
    assert gates["readiness:installed"].original_freshness == "stale"
    assert gates["readiness:visible_owned"].original_freshness == "stale"


def test_typed_adapter_accepts_established_uppercase_machine_alias(
    tmp_path: Path,
) -> None:
    snapshot = populated_snapshot(tmp_path)
    snapshot = replace(
        snapshot,
        machine_id="Desk-A",
        machine=replace(snapshot.machine, id="Desk-A"),
    )
    query = assess_compatibility_snapshot(
        snapshot,
        target=CompatibilityTarget("machine", "Desk-A", "linux"),
    )
    assert query.assessment.target.key == "Desk-A"
    assert "declared_native_build" not in query.assessment.results[0].conflicts


def test_assess_is_cache_only_exact_and_does_not_persist_overrides(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    path = tmp_path / "steam-agent.sqlite3"
    with Storage(path) as storage:
        before = tuple(storage._connection.iterdump())  # noqa: SLF001

    monkeypatch.setattr(cli, "_declared_facts_client", forbid)
    monkeypatch.setattr(cli, "_steam_web_api_client", forbid)
    monkeypatch.setattr(cli, "_system_profile_collector", forbid)
    monkeypatch.setattr(cli, "_credential_store", forbid)
    monkeypatch.setattr(cli, "discover_steam_root", forbid)
    monkeypatch.setattr(cli.time, "sleep", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:local",
        "--country",
        "US",
        "--language",
        "english",
        "--require",
        "language:english",
        "--override",
        "400:manual-check:effective_execution_support=pass",
        "--explain",
    )

    assert code == 0
    assert stderr == ""
    assert value["command"] == "compatibility.assess"
    assert value["generated_at"] == "2026-07-12T18:00:00Z"
    assert value["context"] == {
        "account_alias": "primary",
        "cache_only": True,
        "country": "US",
        "evidence_machine": "local",
        "language": "english",
        "overrides_ephemeral": True,
        "requirements": ["language:english"],
        "target": "machine:local",
    }
    assert value["data"]["requested_appids"] == [400]
    assert value["data"]["source_completeness"] == {
        "missing_capabilities": [
            "account.visible_owned.read",
            "compatibility.declared.read",
            "library.installed.read",
            "operations.ready.read",
            "system_profile.read",
        ],
        "stale_capabilities": [],
    }
    item = value["data"]["results"][0]
    override_gate = next(
        gate for gate in item["gates"] if gate["name"] == "effective_execution_support"
    )
    assert override_gate["original"] == "unknown"
    assert override_gate["effective"] == "pass"
    assert override_gate["override_name"] == "manual-check"
    references = value["data"]["references"][0]["items"]
    assert {item["provider"] for item in references} == {
        "steam",
        "steamdb",
        "protondb",
        "pcgamingwiki",
    }
    assert all(
        item["access_mode"] == "manual_only"
        and item["automation_supported"] is False
        for item in references
    )
    encoded = json.dumps(value)
    assert "76561198999999999" not in encoded
    assert "Private workstation" not in encoded
    assert "/Users/" not in encoded

    with Storage(path) as storage:
        after = tuple(storage._connection.iterdump())  # noqa: SLF001
    assert after == before


def test_assess_envelope_reports_active_m5_scope_but_retains_future_and_deck_gaps(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = populated_snapshot(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        Storage,
        "read_compatibility_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )

    common = (
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--country",
        "US",
        "--language",
        "english",
    )
    code, machine, stderr = invoke(
        tmp_path, capsys, *common, "--target", "machine:local"
    )
    assert code == 0 and stderr == ""
    assert machine["completeness"] == {
        "missing_capabilities": [],
        "stale_capabilities": [],
        "status": "complete",
        "warnings": [],
    }
    assert machine["data"]["source_completeness"]["missing_capabilities"] == [
        "operations.ready.read"
    ]

    code, deck, stderr = invoke(
        tmp_path, capsys, *common, "--target", "valve:steam-deck"
    )
    assert code == 0 and stderr == ""
    assert deck["completeness"]["status"] == "complete"
    assert deck["completeness"]["warnings"] == []
    assert deck["data"]["source_completeness"]["missing_capabilities"] == [
        "library.installed.read",
        "operations.ready.read",
        "system_profile.read",
    ]
    assert deck["data"]["source_gaps"][0]["missing"] == [
        "library.installed.read",
        "operations.ready.read",
        "system_profile.read",
    ]


def test_assess_opens_existing_cache_read_only_and_does_not_create_missing_store(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    real_storage = cli.Storage
    readonly_flags: list[bool] = []

    def open_storage(path: Path, *, readonly: bool = False):
        readonly_flags.append(readonly)
        return real_storage(path, readonly=readonly)

    monkeypatch.setattr(cli, "Storage", open_storage)
    code, _, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:local",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 0 and stderr == ""
    assert readonly_flags == [True]

    missing_dir = tmp_path / "missing"
    code, missing, stderr = invoke(
        missing_dir,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:local",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 1 and stderr == ""
    assert missing["error"]["code"] == "ACCOUNT_NOT_CONFIGURED"
    assert not (missing_dir / "steam-agent.sqlite3").exists()


def test_assess_outdated_readonly_schema_returns_actionable_typed_error(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    path = tmp_path / "steam-agent.sqlite3"
    with Storage(path) as storage:
        storage._connection.execute(  # noqa: SLF001
            "DELETE FROM schema_migrations WHERE version = 22"
        )
        storage._connection.commit()  # noqa: SLF001

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:local",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "DATABASE_ERROR"
    assert "migration" in value["error"]["remediation"]
    assert "Traceback" not in json.dumps(value)


def test_assess_corrupt_cached_timestamp_returns_database_error_with_resync(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = populated_snapshot(tmp_path)
    current = snapshot.system_profile.current
    assert current is not None
    corrupt = replace(
        snapshot,
        system_profile=replace(
            snapshot.system_profile,
            current=replace(current, observed_at="not-a-timestamp"),
        ),
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        Storage,
        "read_compatibility_snapshot",
        lambda *_args, **_kwargs: corrupt,
    )

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:local",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "DATABASE_ERROR"
    assert "Resync" in value["error"]["remediation"]
    assert "Traceback" not in json.dumps(value)


def test_assess_malformed_request_is_not_misclassified_as_corrupt_cache(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:local",
        "--country",
        "US",
        "--language",
        "english",
        "--require",
        "missing-colon",
    )

    assert code == 2
    assert stderr == ""
    assert value["error"]["code"] == "INVALID_ARGUMENT"
    assert "Resync" not in value["error"]["remediation"]


@pytest.mark.parametrize(
    "arguments",
    [
        ("0", "--target", "machine:local"),
        ("4294967296", "--target", "machine:local"),
        ("400", "--target", "machine:missing"),
        ("400", "--target", "valve:steamdeck"),
        ("400", "--target", "machine:local", "--require", "english"),
        (
            "400",
            "--target",
            "machine:local",
            "--override",
            "401:manual:effective_execution_support=pass",
        ),
    ],
)
def test_assess_rejects_invalid_bounded_contract(
    tmp_path: Path, capsys: object, arguments: tuple[str, ...]
) -> None:
    configure(tmp_path)
    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        *arguments,
        "--account",
        "primary",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 2
    assert stderr == ""
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_assess_default_json_and_table_remain_compact_and_safe(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    common = (
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "valve:steam-deck",
        "--country",
        "US",
        "--language",
        "english",
    )
    code, value, stderr = invoke(tmp_path, capsys, *common)
    assert code == 0
    assert stderr == ""
    assert value["data"]["target"] == {
        "key": "steam-deck",
        "kind": "valve_deck",
        "platform": "steamos",
    }
    assert "gates" not in value["data"]["results"][0]
    assert value["data"]["explain"] is False

    code = cli.main(["--data-dir", str(tmp_path), *common, "--format", "table"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert "APPID\tCOMPATIBILITY\tPLAYABLE_NOW\tCOMPLETENESS\tUNKNOWNS" in captured.out
    reference_lines = [
        line for line in captured.out.splitlines() if line.startswith("REFERENCE\t")
    ]
    assert len(reference_lines) == 4
    assert {line.split("\t")[2] for line in reference_lines} == {
        "steam",
        "steamdb",
        "protondb",
        "pcgamingwiki",
    }
    assert all("\tmanual_only\tfalse\thttps://" in line for line in reference_lines)
    assert "76561198999999999" not in captured.out


def test_deck_target_uses_the_only_configured_nonlocal_machine_context(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "steam-agent.sqlite3"
    with Storage(path) as storage:
        storage.upsert_machine(
            Machine("desktop", "Private desktop", "linux", "x86_64"),
            observed_at=NOW,
        )
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198999999999",
            configured_at=NOW,
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "valve:steam-deck",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 0
    assert stderr == ""
    assert value["context"]["evidence_machine"] == "desktop"


def test_deck_target_requires_context_when_multiple_machines_exist(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("desktop", "Private desktop", "linux", "x86_64"),
            observed_at=NOW,
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    common = (
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "valve:steam-deck",
        "--country",
        "US",
        "--language",
        "english",
    )

    code, value, stderr = invoke(tmp_path, capsys, *common)
    assert code == 2
    assert stderr == ""
    assert value["error"]["code"] == "INVALID_ARGUMENT"

    code, value, stderr = invoke(
        tmp_path, capsys, *common, "--context-machine", "desktop"
    )
    assert code == 0
    assert stderr == ""
    assert value["context"]["evidence_machine"] == "desktop"


def test_assess_missing_account_is_typed_without_identity_disclosure(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "missing",
        "--target",
        "machine:local",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "ACCOUNT_NOT_CONFIGURED"
    assert "76561198999999999" not in json.dumps(value)


@pytest.mark.parametrize(
    ("country", "language"),
    (("us", "english"), ("USA", "english"), ("US", "English"), ("US", "en glish")),
)
def test_assess_rejects_noncanonical_locale_context(
    tmp_path: Path, capsys: object, country: str, language: str
) -> None:
    configure(tmp_path)
    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:local",
        "--country",
        country,
        "--language",
        language,
    )
    assert code == 2
    assert stderr == ""
    assert value["error"]["code"] == "INVALID_ARGUMENT"
