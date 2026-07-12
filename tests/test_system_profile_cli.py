from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import steam_agent.cli as cli
from steam_agent.system_profile import CollectedSystemProfile, fact, unknown
from steam_agent.storage import Machine, Storage


NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def profile() -> dict[str, object]:
    return {
        "schema_id": "system-profile/0.1",
        "os": {
            "family": fact("known", value="linux", evidence_refs=("platform:system",)),
            "name": fact("known", value="Linux", evidence_refs=("platform:system",)),
            "version": fact("known", value="1", evidence_refs=("platform:release",)),
            "build": unknown("not_applicable", "platform:build"),
            "kernel": fact("known", value="1", evidence_refs=("platform:release",)),
        },
        "cpu": {
            "architecture": fact("known", value="x86_64", evidence_refs=("platform:machine",)),
            "model": unknown("not_reported", "platform:cpu"),
            "physical_cores": unknown("not_reported", "platform:cpu-count"),
            "logical_processors": fact("known", value=8, evidence_refs=("platform:cpu-count",)),
            "features": fact("known", value=[], evidence_refs=("platform:cpu-features",)),
        },
        "memory": {
            "total_bytes": fact("known", value=8 * 1024**3, evidence_refs=("platform:physical-memory",))
        },
        "graphics": unknown("not_observed", "platform:graphics"),
        "storage": fact(
            "known",
            value=[{
                "role": "system", "capacity_bytes": 1000,
                "available_bytes": 500, "filesystem": None, "medium": "unknown",
            }],
            evidence_refs=("filesystem:system-role",),
        ),
        "gamepad": unknown("not_observed", "platform:input"),
        "vr": unknown("not_observed", "platform:vr"),
    }


def invoke(tmp_path, capsys, *args: str):
    code = cli.main(["--data-dir", str(tmp_path), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def setup(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        cli, "machine_for", lambda alias: Machine(alias, alias, "linux", "x86_64")
    )

    def collector():
        calls.append(1)
        return CollectedSystemProfile(profile(), "complete")

    monkeypatch.setattr(cli, "_system_profile_collector", collector)
    return calls


def test_sync_requires_disclosure_before_collection_and_query_is_cache_only(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = setup(monkeypatch)
    code, blocked, _ = invoke(tmp_path, capsys, "sync", "system")
    assert code == 1
    assert blocked["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"
    assert calls == []

    code, synced, _ = invoke(
        tmp_path, capsys, "sync", "system", "--acknowledge-local-storage"
    )
    assert code == 0 and synced["data"]["promoted"] is True
    assert calls == [1]

    code, queried, _ = invoke(tmp_path, capsys, "system", "query")
    assert code == 0 and queried["data"]["profile"] == profile()
    assert calls == [1]
    encoded = json.dumps(queried).casefold()
    assert "hostname" not in encoded and "username" not in encoded
    assert str(tmp_path).casefold() not in encoded


def test_query_before_sync_is_truthfully_unavailable(tmp_path, capsys) -> None:
    code, result, _ = invoke(tmp_path, capsys, "system", "query")
    assert code == 0
    assert result["completeness"]["status"] == "unavailable"
    assert result["completeness"]["missing_capabilities"] == ["system_profile.read"]
    assert result["data"]["profile"] is None


def test_stale_profile_is_partial_in_json_and_table(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(monkeypatch)
    assert invoke(
        tmp_path, capsys, "sync", "system", "--acknowledge-local-storage"
    )[0] == 0
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(minutes=16))
    code, result, _ = invoke(tmp_path, capsys, "system", "query")
    assert code == 0 and result["completeness"]["status"] == "partial"
    assert result["data"]["freshness"]["storage_available"] == "stale"

    code = cli.main([
        "--data-dir", str(tmp_path), "system", "query", "--format", "table"
    ])
    table = capsys.readouterr()
    assert code == 0 and table.err == ""
    assert table.out.startswith("COMPLETENESS\tpartial\n")


def test_machine_alias_platform_conflict_is_typed_and_does_not_collect(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = setup(monkeypatch)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("local", "Existing", "windows", "x86_64"), observed_at=NOW
        )
    code, result, _ = invoke(
        tmp_path, capsys, "sync", "system", "--acknowledge-local-storage"
    )
    assert code == 1 and result["error"]["code"] == "MACHINE_PROFILE_CONFLICT"
    assert calls == []
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        assert storage.get_machine("local") == Machine("local", "Existing", "windows", "x86_64")


def test_machine_deletion_requires_confirmation_and_preserves_machine(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(monkeypatch)
    invoke(tmp_path, capsys, "sync", "system", "--acknowledge-local-storage")
    code, blocked, _ = invoke(
        tmp_path, capsys, "data", "delete", "--provider", "local-system",
        "--machine", "local",
    )
    assert code == 1 and blocked["error"]["code"] == "CONFIRMATION_REQUIRED"
    code, deleted, _ = invoke(
        tmp_path, capsys, "data", "delete", "--provider", "local-system",
        "--machine", "local", "--yes",
    )
    assert code == 0 and deleted["data"]["machine_preserved"] is True
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        assert storage.get_machine("local") is not None
        assert storage.read_system_profile_snapshot("local")["profile"] is None
