from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from steam_agent.system_profile import (
    SYSTEM_PROFILE_DISCLOSURE_VERSION,
    CollectedSystemProfile,
    collect_system_profile,
    fact,
    query_system_profile,
    sync_system_profile,
    unknown,
    validate_system_profile,
)
from steam_agent.storage import InvalidSyncTransition, Machine, Storage


NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
T0 = "2026-07-12T12:00:00Z"
T1 = "2026-07-12T12:01:00Z"
Usage = namedtuple("Usage", "total used free")


def profile(*, name: str = "Test OS") -> dict[str, object]:
    return {
        "schema_id": "system-profile/0.1",
        "os": {
            "family": fact("known", value="linux", evidence_refs=("platform:system",)),
            "name": fact("known", value=name, evidence_refs=("platform:system",)),
            "version": fact("known", value="1.0", evidence_refs=("platform:release",)),
            "build": unknown("not_reported", "platform:build"),
            "kernel": fact("known", value="1.0.0", evidence_refs=("platform:release",)),
        },
        "cpu": {
            "architecture": fact("known", value="x86_64", evidence_refs=("platform:machine",)),
            "model": fact("known", value="Example CPU", evidence_refs=("linux:proc-cpuinfo",)),
            "physical_cores": fact("known", value=4, evidence_refs=("platform:cpu-count",)),
            "logical_processors": fact("known", value=8, evidence_refs=("platform:cpu-count",)),
            "features": fact("known", value=["avx2"], evidence_refs=("linux:proc-cpuinfo",)),
        },
        "memory": {
            "total_bytes": fact("known", value=16 * 1024**3, evidence_refs=("linux:proc-meminfo",))
        },
        "graphics": unknown("not_observed", "linux:drm-allowlist"),
        "storage": fact(
            "known",
            value=[{
                "role": "system", "capacity_bytes": 1000,
                "available_bytes": 400, "filesystem": None, "medium": "unknown",
            }],
            evidence_refs=("filesystem:system-role",),
        ),
        "gamepad": unknown("not_observed", "platform:input"),
        "vr": unknown("not_observed", "platform:vr"),
    }


def configured(tmp_path: Path, machine: str = "local") -> Storage:
    storage = Storage(tmp_path / "steam-agent.sqlite3")
    storage.upsert_machine(Machine(machine, machine, "linux", "x86_64"), observed_at=T0)
    storage.record_system_profile_consent(
        machine_id=machine,
        disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
        accepted_at=T0,
        backups_acknowledged=True,
    )
    return storage


def test_linux_collector_normalizes_only_allowlisted_fixture_facts() -> None:
    files = {
        "/etc/os-release": 'NAME="Test Linux"\nVERSION_ID="42"\nHOME_URL="https://private.invalid"\n',
        "/proc/cpuinfo": "model name : Example CPU\nflags : sse2 avx2 secret_feature\n",
        "/proc/meminfo": "MemTotal:       16777216 kB\nPrivate: /home/alice\n",
        "/sys/class/drm/card0/device/vendor": "0x10de\n",
        "/sys/class/drm/card0/device/device": "0x2684\n",
        "/sys/class/drm/card0/device/mem_info_vram_total": str(8 * 1024**3),
    }

    result = collect_system_profile(
        system="linux",
        machine_architecture="AMD64",
        reader=lambda path, _limit: files.get(str(path)),
        disk_usage=lambda _path: Usage(1000, 600, 400),
    )

    assert result.status == "complete"
    assert result.profile["os"]["name"]["value"] == "Test Linux"  # type: ignore[index]
    assert result.profile["cpu"]["architecture"]["value"] == "x86_64"  # type: ignore[index]
    assert result.profile["cpu"]["features"]["value"] == ["avx2", "sse2"]  # type: ignore[index]
    assert result.profile["graphics"]["value"] == [{  # type: ignore[index]
        "adapter_id": "gpu-0", "name": "Graphics adapter",
        "vendor_id": "10de", "device_id": "2684",
        "memory": {"kind": "dedicated", "bytes": 8 * 1024**3},
        "driver_version": None, "apis": [],
    }]
    encoded = json.dumps(result.profile)
    assert "alice" not in encoded and "secret_feature" not in encoded and "HOME_URL" not in encoded


def test_unrecognized_architecture_is_unknown_and_prevents_promotion() -> None:
    result = collect_system_profile(
        system="linux", machine_architecture="vendor-private-architecture",
        reader=lambda path, _limit: (
            "NAME=Linux\nVERSION_ID=1\n" if str(path) == "/etc/os-release"
            else "MemTotal: 1024 kB\n" if str(path) == "/proc/meminfo"
            else "model name: CPU\n"
        ),
        disk_usage=lambda _path: Usage(1000, 600, 400),
    )
    assert result.status == "partial"
    assert result.profile["cpu"]["architecture"]["state"] == "unknown"  # type: ignore[index]


def test_macos_collector_uses_fixed_bounded_commands_and_ignores_private_keys() -> None:
    calls: list[tuple[tuple[str, ...], float, int]] = []

    def runner(argv, timeout, limit):
        calls.append((tuple(argv), timeout, limit))
        values = {
            ("/usr/bin/sw_vers", "-productVersion"): "15.5",
            ("/usr/bin/sw_vers", "-buildVersion"): "24F74",
            ("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"): "Apple M3",
            ("/usr/sbin/sysctl", "-n", "hw.physicalcpu"): "8",
            ("/usr/sbin/sysctl", "-n", "hw.logicalcpu"): "8",
            ("/usr/sbin/sysctl", "-n", "hw.memsize"): str(16 * 1024**3),
            ("/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"): json.dumps({
                "SPDisplaysDataType": [{
                    "_name": "Apple M3", "spdisplays_vendor-id": "0x106b",
                    "serial_number": "DO-NOT-KEEP", "spdisplays_metal": "supported",
                }]
            }),
        }
        return values.get(tuple(argv))

    result = collect_system_profile(
        system="macos", machine_architecture="arm64", runner=runner,
        disk_usage=lambda _path: Usage(1000, 600, 400),
    )
    assert result.status == "complete"
    assert all(timeout <= 4 and limit <= 65536 for _, timeout, limit in calls)
    assert all(call[0][0] in {"/usr/bin/sw_vers", "/usr/sbin/sysctl", "/usr/sbin/system_profiler"} for call in calls)
    assert "DO-NOT-KEEP" not in json.dumps(result.profile)


def test_windows_collector_never_requests_stable_device_identity() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv, _timeout, _limit):
        calls.append(tuple(argv))
        return json.dumps({
            "Name": "Example GPU", "AdapterRAM": 8 * 1024**3,
            "DriverVersion": "31.0.15.1234",
        })

    result = collect_system_profile(
        system="windows", machine_architecture="AMD64", runner=runner,
        windows_native={
            "os_version": "11", "os_build": "26100", "cpu_model": "Example CPU",
            "physical_cores": 8, "logical_processors": 16,
            "memory_total_bytes": 32 * 1024**3,
        },
        disk_usage=lambda _path: Usage(1000, 600, 400),
    )
    command = " ".join(calls[0]).casefold()
    assert all(word.casefold() not in command for word in (
        "PNPDeviceID", "DeviceID", "SystemName", "SerialNumber"
    ))
    assert result.profile["graphics"]["value"][0]["adapter_id"] == "gpu-0"  # type: ignore[index]
    assert result.profile["graphics"]["value"][0]["vendor_id"] is None  # type: ignore[index]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("hostname", "gaming-pc"), ("username", "alice"),
        ("serial_number", "ABC123"), ("uuid", "550e8400-e29b-41d4-a716-446655440000"),
        ("mac_address", "00:11:22:33:44:55"), ("ip_address", "192.168.1.2"),
        ("mount_point", "/private/games"), ("device_node", "/dev/disk1"),
    ],
)
def test_validator_rejects_private_keys_and_values(key: str, value: str) -> None:
    candidate = profile()
    candidate["storage"]["value"][0][key] = value  # type: ignore[index]
    with pytest.raises(ValueError, match="private"):
        validate_system_profile(candidate)


def test_storage_requires_current_machine_scoped_consent(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        storage.upsert_machine(Machine("local", "Local", "linux", "x86_64"), observed_at=T0)
        with pytest.raises(InvalidSyncTransition, match="consent"):
            storage.begin_system_profile_sync(
                machine_id="local",
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
                started_at=T0,
            )


def test_storage_refuses_incomplete_or_machine_conflicting_profile(tmp_path: Path) -> None:
    with configured(tmp_path) as storage:
        incomplete = profile()
        incomplete["memory"]["total_bytes"] = unknown(  # type: ignore[index]
            "not_reported", "platform:physical-memory"
        )
        run = storage.begin_system_profile_sync(
            machine_id="local", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            started_at=T0,
        )
        with pytest.raises(ValueError, match="complete"):
            storage.complete_system_profile_sync(
                run.id, profile=incomplete, observed_at=T0, completed_at=T1,
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            )
        storage.finish_system_profile_sync_failed(
            run.id, status="partial", error_code="MISSING_RAM", completed_at=T1
        )

        conflicting = profile()
        conflicting["os"]["family"] = fact(  # type: ignore[index]
            "known", value="windows", evidence_refs=("platform:system",)
        )
        run = storage.begin_system_profile_sync(
            machine_id="local", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            started_at=T1,
        )
        with pytest.raises(InvalidSyncTransition, match="conflicts"):
            storage.complete_system_profile_sync(
                run.id, profile=conflicting, observed_at=T1, completed_at=T1,
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            )


def test_complete_profile_promotes_and_failed_attempt_preserves_last_good(tmp_path: Path) -> None:
    with configured(tmp_path) as storage:
        complete = sync_system_profile(
            storage, machine_id="local",
            collector=lambda: CollectedSystemProfile(profile(), "complete"),
            clock=lambda: NOW,
        )
        assert complete.run.promoted is True
        failed = sync_system_profile(
            storage, machine_id="local",
            collector=lambda: CollectedSystemProfile(profile(), "partial", "MISSING_RAM"),
            clock=lambda: NOW,
        )
        assert failed.run.status == "partial" and failed.run.promoted is False
        queried = query_system_profile(storage, machine_id="local", clock=lambda: NOW)
        assert queried["profile"] == profile()
        assert queried["snapshot"]["using_last_good"] is True


def test_out_of_order_completion_cannot_replace_newer_profile(tmp_path: Path) -> None:
    with configured(tmp_path) as storage:
        older = storage.begin_system_profile_sync(
            machine_id="local", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION, started_at=T0
        )
        newer = storage.begin_system_profile_sync(
            machine_id="local", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION, started_at=T1
        )
        newer_profile = profile(name="newer")
        storage.complete_system_profile_sync(
            newer.id, profile=newer_profile, observed_at=T1, completed_at=T1,
            disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
        )
        old_result = storage.complete_system_profile_sync(
            older.id, profile=profile(name="older"), observed_at=T0, completed_at=T1,
            disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
        )
        assert old_result.promoted is False
        assert storage.read_system_profile_snapshot("local")["profile"] == newer_profile


def test_new_sync_recovers_abandoned_attempt(tmp_path: Path) -> None:
    with configured(tmp_path) as storage:
        abandoned = storage.begin_system_profile_sync(
            machine_id="local", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            started_at="2026-07-12T11:00:00Z",
        )
        current = storage.begin_system_profile_sync(
            machine_id="local", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            started_at=T0,
        )
        recovered = storage.get_sync_run(abandoned.id)
        assert recovered.status == "failed"
        assert recovered.error_code == "SYNC_ABANDONED"
        assert storage.get_sync_run(current.id).status == "running"


def test_profiles_are_isolated_and_delete_preserves_machine(tmp_path: Path) -> None:
    storage = configured(tmp_path, "one")
    try:
        storage.upsert_machine(Machine("two", "Two", "linux", "x86_64"), observed_at=T0)
        storage.record_system_profile_consent(
            machine_id="two", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            accepted_at=T0, backups_acknowledged=True,
        )
        for machine, name in (("one", "first"), ("two", "second")):
            run = storage.begin_system_profile_sync(
                machine_id=machine, disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
                started_at=T0,
            )
            storage.complete_system_profile_sync(
                run.id, profile=profile(name=name), observed_at=T0, completed_at=T1,
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            )
        partial = storage.begin_system_profile_sync(
            machine_id="one", disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            started_at=T1,
        )
        storage.finish_system_profile_sync_failed(
            partial.id, status="partial", error_code="MISSING_RAM", completed_at=T1
        )
        deletion = storage.delete_system_profile_data("one")
        assert deletion["current_removed"] == 1
        assert deletion["sync_runs_removed"] == 2
        assert deletion["evidence_removed"] == 1
        assert storage.get_machine("one") is not None
        assert storage.read_system_profile_snapshot("one")["profile"] is None
        assert storage.read_system_profile_snapshot("two")["profile"] == profile(name="second")
    finally:
        storage.close()
