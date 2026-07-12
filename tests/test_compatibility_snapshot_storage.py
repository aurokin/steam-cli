from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event
from typing import Mapping

import pytest

from steam_agent.steam_declared_facts import (
    HttpResponse,
    SteamDeclaredFactsClient,
    declared_facts_payload,
)
from steam_agent.storage import (
    EvidenceInput,
    InstalledObservation,
    Machine,
    OwnedObservation,
    Storage,
    StorageError,
    SystemProfileSnapshot,
    steam_application_stable_id,
)
from steam_agent.system_profile import fact, unknown


T0 = "2026-07-12T12:00:00Z"
T1 = "2026-07-12T12:01:00Z"
T2 = "2026-07-12T12:02:00Z"
T3 = "2026-07-12T12:03:00Z"
FIXTURES = Path(__file__).parent / "fixtures" / "steam_declared_facts"


class FixtureTransport:
    def __init__(self, appid: int) -> None:
        self.appid = appid

    def request(self, **_: object) -> HttpResponse:
        filename = "legacy_shape.json" if self.appid == 400 else "list_shape.json"
        return HttpResponse(
            200,
            (FIXTURES / filename).read_bytes(),
            {"Content-Type": "application/json"},
        )


def declared_payload(appid: int) -> Mapping[str, object]:
    result = SteamDeclaredFactsClient(transport=FixtureTransport(appid)).fetch(
        appid, country="US", language="english"
    )
    assert result.facts is not None
    return declared_facts_payload(result.facts)


def system_profile(name: str = "Test OS") -> dict[str, object]:
    return {
        "schema_id": "system-profile/0.1",
        "os": {
            "family": fact("known", value="linux", evidence_refs=("platform:system",)),
            "name": fact("known", value=name, evidence_refs=("platform:system",)),
            "version": fact("known", value="1", evidence_refs=("platform:release",)),
            "build": unknown("not_reported", "platform:build"),
            "kernel": fact("known", value="1", evidence_refs=("platform:release",)),
        },
        "cpu": {
            "architecture": fact(
                "known", value="x86_64", evidence_refs=("platform:machine",)
            ),
            "model": fact("known", value="CPU", evidence_refs=("linux:proc-cpuinfo",)),
            "physical_cores": fact(
                "known", value=4, evidence_refs=("platform:cpu-count",)
            ),
            "logical_processors": fact(
                "known", value=8, evidence_refs=("platform:cpu-count",)
            ),
            "features": fact(
                "known", value=["avx2"], evidence_refs=("linux:proc-cpuinfo",)
            ),
        },
        "memory": {
            "total_bytes": fact(
                "known", value=16 * 1024**3, evidence_refs=("linux:proc-meminfo",)
            )
        },
        "graphics": unknown("not_observed", "linux:drm-allowlist"),
        "storage": fact(
            "known",
            value=[
                {
                    "role": "system",
                    "capacity_bytes": 1000,
                    "available_bytes": 400,
                    "filesystem": None,
                    "medium": "unknown",
                }
            ],
            evidence_refs=("filesystem:system-role",),
        ),
        "gamepad": unknown("not_observed", "platform:input"),
        "vr": unknown("not_observed", "platform:vr"),
    }


def configured(path: Path) -> tuple[Storage, int, int]:
    storage = Storage(path)
    for machine in ("one", "two"):
        storage.upsert_machine(
            Machine(machine, f"Machine {machine}", "linux", "x86_64"),
            observed_at=T0,
        )
        storage.record_system_profile_consent(
            machine_id=machine,
            disclosure_version="system-profile-0.1-v1",
            accepted_at=T0,
            backups_acknowledged=True,
        )
    accounts = []
    for index, alias in enumerate(("primary", "other")):
        account = storage.configure_steam_account(
            alias=alias,
            steam_id64=str(76561198000000000 + index),
            configured_at=T0,
        )
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="owned-v1",
            accepted_at=T0,
            backups_acknowledged=True,
        )
        storage.record_compatibility_data_consent(
            account_id=account.id,
            disclosure_version="m5-v1",
            accepted_at=T0,
            backups_acknowledged=True,
        )
        accounts.append(account.id)
    return storage, accounts[0], accounts[1]


def complete_owned(
    storage: Storage, account_id: int, appids: list[int], at: str
) -> int:
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="owned.visible.read",
        account_id=account_id,
        started_at=at,
    )
    observations = [
        OwnedObservation(appid, 10, "visible_owned", at, name=f"Game {appid}")
        for appid in appids
    ]
    storage.complete_owned_snapshot(
        run.id,
        observations,
        base_retrieved_at=at,
        expanded_retrieved_at=at,
        base_reported_count=len(appids),
        expanded_reported_count=len(appids),
        completed_at=at,
    )
    return run.id


def complete_installed(
    storage: Storage, machine_id: str, appids: list[int], at: str
) -> int:
    run = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id=machine_id,
        started_at=at,
    )
    for appid in appids:
        observation = InstalledObservation(
            appid=appid,
            name=f"Game {appid}",
            app_type="game",
            library_root="/synthetic/library",
            install_dir=f"game-{appid}",
            observed_at=at,
        )
        evidence = EvidenceInput(
            provider="local_steam",
            capability="installed",
            source_kind="local_file",
            source_locator=f"appmanifest_{appid}.acf",
            retrieved_at=at,
            support_level="core",
            payload={"appid": appid},
        )
        storage.record_installed_observation(run.id, observation, evidence)
    storage.finish_installed_sync(run.id, status="complete", completed_at=at)
    return run.id


def complete_system(storage: Storage, machine_id: str, at: str) -> int:
    run = storage.begin_system_profile_sync(
        machine_id=machine_id,
        disclosure_version="system-profile-0.1-v1",
        started_at=at,
    )
    storage.complete_system_profile_sync(
        run.id,
        profile=system_profile(),
        observed_at=at,
        completed_at=at,
        disclosure_version="system-profile-0.1-v1",
    )
    return run.id


def declared_run(
    storage: Storage,
    account_id: int,
    machine_id: str,
    appids: list[int],
    at: str,
    *,
    maximum: int = 100,
):
    return storage.begin_declared_app_sync(
        account_id=account_id,
        machine_id=machine_id,
        demanded_appids=appids,
        country="US",
        language="english",
        max_items=maximum,
        skip_fresh_terminal=False,
        started_at=at,
        disclosure_version="m5-v1",
    )[0]


def test_snapshot_preserves_missing_identity_and_chunks_more_than_sqlite_limit(
    tmp_path: Path,
) -> None:
    storage, account_id, _ = configured(tmp_path / "snapshot.sqlite3")
    try:
        requested = list(range(1, 1206))
        snapshot = storage.read_compatibility_snapshot(
            account_id, "one", "US", "english", requested, now=T0
        )

        assert len(snapshot.requested) == 1205
        assert len(snapshot.declared_apps.subjects) == 1205
        assert snapshot.requested[-1].stable_game_id == steam_application_stable_id(
            1205
        )
        assert (
            snapshot.declared_apps.subjects[-1].latest_demand.error_code == "NOT_SYNCED"
        )
        assert storage.get_app(1205) is None
    finally:
        storage.close()


def test_snapshot_scopes_accounts_machines_and_context_without_narrow_masking(
    tmp_path: Path,
) -> None:
    storage, primary, other = configured(tmp_path / "scope.sqlite3")
    try:
        owned_primary = complete_owned(storage, primary, [400], T0)
        owned_other = complete_owned(storage, other, [620], T0)
        installed_one = complete_installed(storage, "one", [400], T0)
        installed_two = complete_installed(storage, "two", [620], T0)
        broad = declared_run(storage, primary, "one", [400, 620], T0)
        for appid in (400, 620):
            storage.record_declared_app_result(
                broad.id,
                account_id=primary,
                appid=appid,
                state="ready",
                facts=declared_payload(appid),
                observed_at=T1,
            )
        storage.finish_declared_app_sync(broad.id, completed_at=T1)
        narrow = declared_run(storage, primary, "one", [400], T2)
        storage.record_declared_app_result(
            narrow.id,
            account_id=primary,
            appid=400,
            state="not_found",
            observed_at=T2,
        )
        storage.finish_declared_app_sync(narrow.id, completed_at=T2)

        snapshot = storage.read_compatibility_snapshot(
            primary, "one", "US", "english", [400, 620], now=T3
        )
        subjects = {item.appid: item for item in snapshot.declared_apps.subjects}
        assert subjects[400].current is not None
        assert subjects[400].latest_demand.sync_run_id == narrow.id
        assert subjects[400].latest_demand.state == "not_found"
        assert subjects[620].latest_demand.sync_run_id == broad.id
        assert [game.appid for game in snapshot.owned.games] == [400]
        assert [game.appid for game in snapshot.installed.games] == [400]
        assert snapshot.owned.latest_complete is not None
        assert snapshot.owned.latest_complete.id == owned_primary
        assert snapshot.installed.latest_complete is not None
        assert snapshot.installed.latest_complete.id == installed_one

        isolated = storage.read_compatibility_snapshot(
            other, "two", "US", "english", [400], now=T3
        )
        assert isolated.declared_apps.subjects[0].current is not None
        assert isolated.declared_apps.subjects[0].latest_demand.sync_run_id is None
        assert isolated.machine.id == "two"
        assert [game.appid for game in isolated.owned.games] == [620]
        assert [game.appid for game in isolated.installed.games] == [620]
        assert isolated.owned.latest is not None
        assert isolated.owned.latest.id == owned_other
        assert isolated.installed.latest is not None
        assert isolated.installed.latest.id == installed_two

        alternate_context = storage.read_compatibility_snapshot(
            primary, "one", "CA", "english", [400], now=T3
        )
        assert alternate_context.declared_apps.subjects[0].current is None
        assert (
            alternate_context.declared_apps.subjects[0].latest_demand.sync_run_id
            is None
        )

        storage._connection.execute("BEGIN")  # noqa: SLF001
        try:
            with pytest.raises(StorageError, match="inside a transaction"):
                storage.read_declared_app_snapshot(
                    account_id=primary,
                    machine_id="one",
                    country="US",
                    language="english",
                    appids=[400],
                )
        finally:
            storage._connection.rollback()  # noqa: SLF001
    finally:
        storage.close()


def test_snapshot_exposes_last_good_and_newest_failed_attempts_and_deletion(
    tmp_path: Path,
) -> None:
    storage, account_id, _ = configured(tmp_path / "last-good.sqlite3")
    try:
        complete_id = complete_system(storage, "one", T0)
        failed = storage.begin_system_profile_sync(
            machine_id="one",
            disclosure_version="system-profile-0.1-v1",
            started_at=T1,
        )
        storage.finish_system_profile_sync_failed(
            failed.id,
            status="failed",
            error_code="COLLECTION_FAILED",
            completed_at=T1,
        )

        snapshot = storage.read_compatibility_snapshot(
            account_id, "one", "US", "english", [999], now=T2
        )
        assert snapshot.system_profile.current is not None
        assert snapshot.system_profile.current.promoted_sync_run_id == complete_id
        assert snapshot.system_profile.current.evidence_id > 0
        assert snapshot.system_profile.latest is not None
        assert snapshot.system_profile.latest.id == failed.id
        assert snapshot.system_profile.latest_complete is not None
        assert snapshot.system_profile.latest_complete.id == complete_id

        storage._connection.execute(  # noqa: SLF001 - corrupt-row regression
            "UPDATE system_profile_current SET profile_json='{}' WHERE machine_id='one'"
        )
        storage._connection.commit()  # noqa: SLF001
        with pytest.raises(ValueError):
            storage.read_compatibility_snapshot(
                account_id, "one", "US", "english", [999], now=T2
            )
        deck = storage.read_compatibility_snapshot(
            account_id,
            "one",
            "US",
            "english",
            [999],
            now=T2,
            include_local_target_evidence=False,
        )
        assert deck.system_profile == SystemProfileSnapshot(None, None, None)
        assert deck.installed.games == ()
        assert deck.installed.latest is None
        storage._connection.execute(  # noqa: SLF001 - restore fixture for deletion
            "UPDATE system_profile_current SET profile_json=? WHERE machine_id='one'",
            (json.dumps(system_profile()),),
        )
        storage._connection.commit()  # noqa: SLF001

        storage.delete_system_profile_data("one")
        deleted = storage.read_compatibility_snapshot(
            account_id, "one", "US", "english", [999], now=T3
        )
        assert deleted.system_profile.current is None
        assert deleted.system_profile.latest is None
        assert deleted.system_profile.latest_complete is None
    finally:
        storage.close()


def test_snapshot_is_atomic_when_writer_promotes_after_read_begins(
    tmp_path: Path,
) -> None:
    path = tmp_path / "atomic.sqlite3"
    storage, account_id, _ = configured(path)
    complete_owned(storage, account_id, [400], T0)
    complete_installed(storage, "one", [400], T0)
    storage._connection.execute("PRAGMA journal_mode=WAL")  # noqa: SLF001
    entered = Event()
    written = Event()
    original = storage._read_system_profile_snapshot  # noqa: SLF001

    def pause_after_snapshot(machine_id: str):
        result = original(machine_id)
        entered.set()
        assert written.wait(5)
        return result

    storage._read_system_profile_snapshot = pause_after_snapshot  # type: ignore[method-assign]  # noqa: SLF001

    def replace_owned() -> None:
        assert entered.wait(5)
        with Storage(path) as writer:
            complete_owned(writer, account_id, [620], T1)
        written.set()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(replace_owned)
            snapshot = storage.read_compatibility_snapshot(
                account_id, "one", "US", "english", [400, 620], now=T2
            )
            future.result(timeout=5)

        assert [game.appid for game in snapshot.owned.games] == [400]
        assert [
            game.appid for game in storage.read_owned_snapshot(account_id).games
        ] == [620]
        assert [game.appid for game in snapshot.installed.games] == [400]
    finally:
        storage.close()
