"""Privacy-bounded local system profile acquisition and cache query.

Collectors expose only an allowlist of compatibility facts. They never use a
shell and never retain command output, filesystem paths, device nodes, or stable
hardware identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from steam_agent.storage import Storage, SyncRun


SYSTEM_PROFILE_SCHEMA_ID = "system-profile/0.1"
SYSTEM_PROFILE_DISCLOSURE_VERSION = "2026-07-12.m5"
SYSTEM_PROFILE_CAPABILITY = "system_profile.read"
PROFILE_RETENTION = timedelta(days=30)
FACT_FRESHNESS = {
    "os": timedelta(days=30),
    "cpu": timedelta(days=30),
    "memory": timedelta(days=30),
    "graphics": timedelta(days=7),
    "storage_capacity": timedelta(hours=24),
    "storage_available": timedelta(minutes=15),
    "gamepad": timedelta(minutes=15),
    "vr": timedelta(minutes=15),
}
FACT_STATES = frozenset(
    {"known", "unknown", "unavailable", "permission_denied", "not_applicable", "error"}
)
REQUIRED_FACTS = (("os", "family"), ("cpu", "architecture"), ("memory", "total_bytes"))
_FEATURE_ALLOWLIST = frozenset(
    {"aes", "asimd", "avx", "avx2", "neon", "sse2", "sse4_1", "sse4_2"}
)
_FORBIDDEN_KEY_PARTS = (
    "hostname",
    "user_name",
    "username",
    "serial",
    "uuid",
    "mac_address",
    "ip_address",
    "mount_point",
    "device_node",
    "device_path",
    "home_path",
    "filesystem_path",
)
_PATH = re.compile(
    r"(?:"
    r"(?:^|[\s=(:,;\[{'\"`])/(?!/)[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*"
    r"|/(?:Users|home|private|tmp|var|etc|dev|Volumes|mnt|media|root)(?:[/\\]|$)"
    r"|(?:^|[\s=(:,;\[{'\"`])[A-Za-z]:[\\/]"
    r"|(?:^|[\s=(:,;\[{'\"`])(?:\\\\|//)[A-Za-z0-9._~-]"
    r")",
    re.IGNORECASE,
)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_UUID = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)
_EVIDENCE_REF = re.compile(r"[a-z0-9_-]{1,32}:[a-z0-9_.-]{1,64}\Z")
_MAX_COMMAND_OUTPUT = 64 * 1024
_COMMAND_TIMEOUT_SECONDS = 4.0
_WINDOWS_POWERSHELL = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)
_WINDOWS_GPU_COMMAND = (
    _WINDOWS_POWERSHELL,
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion | ConvertTo-Json -Compress",
)

Clock = Callable[[], datetime]
Reader = Callable[[Path, int], str | None]
Runner = Callable[[Sequence[str], float, int], str | None]
DiskUsage = Callable[[str], Any]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SystemProfileError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CollectedSystemProfile:
    profile: Mapping[str, Any]
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SystemProfileSyncResult:
    run: SyncRun
    profile: Mapping[str, Any] | None


def fact(
    state: str,
    *,
    value: Any = None,
    reason_code: str | None = None,
    evidence_refs: Sequence[str] = (),
) -> dict[str, Any]:
    if state not in FACT_STATES:
        raise ValueError("invalid fact state")
    result: dict[str, Any] = {
        "state": state,
        "evidence_refs": sorted(set(evidence_refs)),
    }
    if state == "known":
        if value is None:
            raise ValueError("known facts require a value")
        result["value"] = value
    elif reason_code:
        result["reason_code"] = reason_code
    return result


def unknown(reason: str, *refs: str) -> dict[str, Any]:
    return fact("unknown", reason_code=reason, evidence_refs=refs)


def collect_system_profile(
    *,
    system: str | None = None,
    machine_architecture: str | None = None,
    reader: Reader | None = None,
    runner: Runner | None = None,
    disk_usage: DiskUsage | None = None,
    windows_native: Mapping[str, Any] | None = None,
) -> CollectedSystemProfile:
    """Collect one normalized profile with injectable, bounded OS boundaries."""

    system_name = (system or platform.system()).casefold()
    architecture = canonical_architecture(machine_architecture or platform.machine())
    read = reader or _bounded_read
    run = runner or _bounded_run
    usage = disk_usage or shutil.disk_usage
    if system_name in {"darwin", "mac", "macos"}:
        sections = _collect_macos(architecture, run)
    elif system_name == "linux":
        sections = _collect_linux(architecture, read)
    elif system_name in {"windows", "win32"}:
        sections = _collect_windows(
            architecture, run,
            _windows_native_facts() if windows_native is None else windows_native,
        )
    else:
        sections = _collect_common(system_name or "unknown", architecture)
    sections["storage"] = _collect_storage(usage)
    sections["gamepad"] = unknown("presence_not_conclusively_observed", "platform:input")
    sections["vr"] = unknown("presence_not_conclusively_observed", "platform:vr")
    profile: dict[str, Any] = {
        "schema_id": SYSTEM_PROFILE_SCHEMA_ID,
        **sections,
    }
    validate_system_profile(profile)
    complete = all(_nested_fact_known(profile, section, name) for section, name in REQUIRED_FACTS)
    return CollectedSystemProfile(
        profile=profile,
        status="complete" if complete else "partial",
        error_code=None if complete else "REQUIRED_SYSTEM_FACTS_UNAVAILABLE",
    )


def _collect_common(system_name: str, architecture: str | None) -> dict[str, Any]:
    family = {
        "darwin": "macos",
        "windows": "windows",
        "linux": "linux",
    }.get(system_name, system_name)
    os_version = platform.release() or None
    return {
        "os": {
            "family": fact("known", value=family, evidence_refs=("platform:system",)),
            "name": fact("known", value=family, evidence_refs=("platform:system",)),
            "version": (
                fact("known", value=os_version, evidence_refs=("platform:release",))
                if os_version else unknown("not_reported", "platform:release")
            ),
            "build": unknown("not_reported", "platform:build"),
            "kernel": (
                fact("known", value=os_version, evidence_refs=("platform:release",))
                if os_version else unknown("not_reported", "platform:release")
            ),
        },
        "cpu": _base_cpu(architecture),
        "memory": {"total_bytes": _common_memory()},
        "graphics": unknown("not_observed", "platform:graphics"),
    }


def _collect_linux(architecture: str | None, reader: Reader) -> dict[str, Any]:
    release = _parse_os_release(reader(Path("/etc/os-release"), 16 * 1024) or "")
    cpuinfo = reader(Path("/proc/cpuinfo"), 256 * 1024) or ""
    meminfo = reader(Path("/proc/meminfo"), 32 * 1024) or ""
    cpu = _base_cpu(architecture)
    cpu["model"] = _known_text(_first_cpu_value(cpuinfo, ("model name", "hardware")), "linux:proc-cpuinfo")
    logical = os.cpu_count()
    cpu["logical_processors"] = (
        fact("known", value=logical, evidence_refs=("platform:cpu-count",))
        if isinstance(logical, int) and logical > 0 else unknown("not_reported", "platform:cpu-count")
    )
    cpu["physical_cores"] = unknown("not_reported", "linux:proc-cpuinfo")
    flags = set((_first_cpu_value(cpuinfo, ("flags", "features")) or "").casefold().split())
    features = sorted(flags & _FEATURE_ALLOWLIST)
    cpu["features"] = (
        fact("known", value=features, evidence_refs=("linux:proc-cpuinfo",))
        if flags else unknown("not_reported", "linux:proc-cpuinfo")
    )
    total = _linux_memory_bytes(meminfo)
    pretty = release.get("PRETTY_NAME") or release.get("NAME")
    version = release.get("VERSION_ID")
    return {
        "os": {
            "family": fact("known", value="linux", evidence_refs=("platform:system",)),
            "name": _known_text(pretty, "linux:os-release"),
            "version": _known_text(version, "linux:os-release"),
            "build": unknown("not_applicable", "linux:os-release"),
            "kernel": _known_text(platform.release() or None, "platform:release"),
        },
        "cpu": cpu,
        "memory": {
            "total_bytes": (
                fact("known", value=total, evidence_refs=("linux:proc-meminfo",))
                if total is not None else _common_memory()
            )
        },
        "graphics": _collect_linux_graphics(reader),
    }


def _collect_macos(architecture: str | None, runner: Runner) -> dict[str, Any]:
    version = runner(("/usr/bin/sw_vers", "-productVersion"), _COMMAND_TIMEOUT_SECONDS, 4096)
    build = runner(("/usr/bin/sw_vers", "-buildVersion"), _COMMAND_TIMEOUT_SECONDS, 4096)
    sysctls = {
        key: runner(("/usr/sbin/sysctl", "-n", key), _COMMAND_TIMEOUT_SECONDS, 4096)
        for key in (
            "machdep.cpu.brand_string", "hw.physicalcpu", "hw.logicalcpu", "hw.memsize"
        )
    }
    cpu = _base_cpu(architecture)
    cpu["model"] = _known_text(sysctls["machdep.cpu.brand_string"], "macos:sysctl-cpu")
    cpu["physical_cores"] = _known_positive_int(sysctls["hw.physicalcpu"], "macos:sysctl-cpu")
    cpu["logical_processors"] = _known_positive_int(sysctls["hw.logicalcpu"], "macos:sysctl-cpu")
    cpu["features"] = unknown("not_collected", "macos:sysctl-cpu")
    memory = _known_positive_int(sysctls["hw.memsize"], "macos:sysctl-memory")
    display_json = runner(
        ("/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"),
        _COMMAND_TIMEOUT_SECONDS,
        _MAX_COMMAND_OUTPUT,
    )
    adapters = _parse_macos_displays(display_json)
    graphics = (
        fact("known", value=adapters, evidence_refs=("macos:display-allowlist",))
        if adapters else unknown("adapter_inventory_not_available", "macos:display-allowlist")
    )
    return {
        "os": {
            "family": fact("known", value="macos", evidence_refs=("platform:system",)),
            "name": fact("known", value="macOS", evidence_refs=("platform:system",)),
            "version": _known_text(version, "macos:sw-vers"),
            "build": _known_text(build, "macos:sw-vers"),
            "kernel": _known_text(platform.release() or None, "platform:release"),
        },
        "cpu": cpu,
        "memory": {"total_bytes": memory},
        "graphics": graphics,
    }


def _collect_windows(
    architecture: str | None, runner: Runner, native: Mapping[str, Any]
) -> dict[str, Any]:
    cpu = _base_cpu(architecture)
    cpu["model"] = _known_text(_string(native.get("cpu_model")), "windows:native-cpu")
    cpu["physical_cores"] = _known_positive_int(native.get("physical_cores"), "windows:native-cpu")
    logical = native.get("logical_processors", os.cpu_count())
    cpu["logical_processors"] = _known_positive_int(logical, "windows:native-cpu")
    features = native.get("cpu_features")
    if isinstance(features, (list, tuple)):
        allowed = sorted({str(item).casefold() for item in features} & _FEATURE_ALLOWLIST)
        cpu["features"] = fact("known", value=allowed, evidence_refs=("windows:native-cpu",))
    else:
        cpu["features"] = unknown("not_reported", "windows:native-cpu")
    gpu_json = runner(
        _WINDOWS_GPU_COMMAND,
        _COMMAND_TIMEOUT_SECONDS,
        _MAX_COMMAND_OUTPUT,
    )
    adapters = _parse_windows_displays(gpu_json)
    total_memory = _positive_int(native.get("memory_total_bytes"))
    version = _string(native.get("os_version")) or platform.version() or None
    build = _string(native.get("os_build"))
    return {
        "os": {
            "family": fact("known", value="windows", evidence_refs=("platform:system",)),
            "name": fact("known", value="Windows", evidence_refs=("platform:system",)),
            "version": _known_text(version, "windows:native-version"),
            "build": _known_text(build, "windows:native-version"),
            "kernel": _known_text(platform.release() or None, "platform:release"),
        },
        "cpu": cpu,
        "memory": {
            "total_bytes": (
                fact("known", value=total_memory, evidence_refs=("windows:native-memory",))
                if total_memory else _common_memory()
            )
        },
        "graphics": (
            fact("known", value=adapters, evidence_refs=("windows:cim-gpu-allowlist",))
            if adapters else unknown("adapter_inventory_not_available", "windows:cim-gpu-allowlist")
        ),
    }


def _collect_storage(disk_usage: DiskUsage) -> dict[str, Any]:
    try:
        usage = disk_usage(os.path.abspath(os.sep))
        total = _positive_int(getattr(usage, "total", None))
        free = _positive_int(getattr(usage, "free", None), allow_zero=True)
    except (OSError, PermissionError):
        return unknown("capacity_unavailable", "filesystem:system-role")
    if total is None or free is None:
        return unknown("capacity_unavailable", "filesystem:system-role")
    return fact(
        "known",
        value=[{
            "role": "system",
            "capacity_bytes": total,
            "available_bytes": free,
            "filesystem": None,
            "medium": "unknown",
        }],
        evidence_refs=("filesystem:system-role",),
    )


def _collect_linux_graphics(reader: Reader) -> dict[str, Any]:
    adapters: list[dict[str, Any]] = []
    for card_index in range(16):
        base = f"/sys/class/drm/card{card_index}/device"
        vendor = _safe_hex(reader(Path(f"{base}/vendor"), 64))
        device = _safe_hex(reader(Path(f"{base}/device"), 64))
        memory = _positive_int(reader(Path(f"{base}/mem_info_vram_total"), 64))
        if vendor is None and device is None:
            continue
        adapters.append({
            "adapter_id": f"gpu-{len(adapters)}",
            "name": "Graphics adapter",
            "vendor_id": vendor,
            "device_id": device,
            "memory": {
                "kind": "dedicated" if memory is not None else "unknown",
                "bytes": memory,
            },
            "driver_version": None,
            "apis": [],
        })
    return (
        fact("known", value=adapters, evidence_refs=("linux:drm-allowlist",))
        if adapters else unknown("adapter_inventory_not_available", "linux:drm-allowlist")
    )


def _windows_native_facts() -> dict[str, Any]:
    """Read bounded Windows facts through native APIs, without identifiers."""

    values: dict[str, Any] = {
        "os_version": platform.version() or None,
        "os_build": platform.win32_ver()[1] or None,
        "cpu_model": platform.processor() or None,
        "logical_processors": os.cpu_count(),
    }
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            values["memory_total_bytes"] = int(status.total_physical)
    except (AttributeError, OSError):
        pass
    return values


def validate_system_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate schema shape and recursively reject identifying material."""

    if not isinstance(profile, Mapping) or profile.get("schema_id") != SYSTEM_PROFILE_SCHEMA_ID:
        raise ValueError("system profile schema is invalid")
    expected = {"schema_id", "os", "cpu", "memory", "graphics", "storage", "gamepad", "vr"}
    if set(profile) != expected:
        raise ValueError("system profile fields are invalid")
    section_fields = {
        "os": {"family", "name", "version", "build", "kernel"},
        "cpu": {"architecture", "model", "physical_cores", "logical_processors", "features"},
        "memory": {"total_bytes"},
    }
    for section in ("os", "cpu", "memory"):
        if not isinstance(profile[section], Mapping):
            raise ValueError("system profile section is invalid")
        if set(profile[section]) != section_fields[section]:
            raise ValueError("system profile section fields are invalid")
        for item in profile[section].values():
            _validate_fact(item)
    for section in ("graphics", "storage", "gamepad", "vr"):
        _validate_fact(profile[section])
    _reject_private_material(profile)
    _validate_profile_values(profile)
    # JSON round-trip creates a plain immutable-at-boundary copy and rejects
    # custom values before storage canonicalization.
    return json.loads(json.dumps(profile, ensure_ascii=False, allow_nan=False))


def system_profile_is_complete(profile: Mapping[str, Any]) -> bool:
    return all(
        _nested_fact_known(profile, section, name)
        for section, name in REQUIRED_FACTS
    )


def sync_system_profile(
    storage: Storage,
    *,
    machine_id: str,
    collector: Callable[[], CollectedSystemProfile] = collect_system_profile,
    clock: Clock = now_utc,
) -> SystemProfileSyncResult:
    run = storage.begin_system_profile_sync(
        machine_id=machine_id,
        disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
        started_at=clock(),
    )
    try:
        collected = collector()
        observed = clock()
        if collected.status != "complete":
            failed = storage.finish_system_profile_sync_failed(
                run.id,
                status="partial",
                error_code=collected.error_code or "SYSTEM_PROFILE_PARTIAL",
                completed_at=clock(),
            )
            return SystemProfileSyncResult(failed, None)
        completed = storage.complete_system_profile_sync(
            run.id,
            profile=collected.profile,
            observed_at=observed,
            completed_at=clock(),
            disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
        )
        return SystemProfileSyncResult(completed, collected.profile)
    except Exception as exc:
        try:
            storage.finish_system_profile_sync_failed(
                run.id,
                status="failed",
                error_code=(exc.code if isinstance(exc, SystemProfileError) else "SYSTEM_PROFILE_COLLECTION_FAILED"),
                completed_at=clock(),
            )
        except Exception:
            pass
        if isinstance(exc, SystemProfileError):
            raise
        raise SystemProfileError("SYSTEM_PROFILE_COLLECTION_FAILED") from None


def query_system_profile(
    storage: Storage,
    *,
    machine_id: str,
    clock: Clock = now_utc,
) -> dict[str, Any]:
    snapshot = storage.read_system_profile_snapshot(machine_id)
    profile = snapshot["profile"]
    latest = snapshot["latest"]
    last_good = snapshot["latest_complete"]
    observed_at = snapshot["observed_at"]
    freshness: dict[str, str] = {}
    if observed_at is not None:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        age = clock().astimezone(timezone.utc) - observed
        for name, duration in FACT_FRESHNESS.items():
            freshness[name] = "unknown" if age < timedelta(0) else (
                "fresh" if age <= duration else "stale"
            )
    return {
        "profile": profile,
        "freshness": freshness,
        "snapshot": {
            "last_attempt_status": None if latest is None else latest.status,
            "last_error_code": None if latest is None else latest.error_code,
            "last_successful_sync_at": None if last_good is None else last_good.completed_at,
            "observed_at": observed_at,
            "using_last_good": latest is not None and latest.status != "complete" and profile is not None,
        },
    }


def _base_cpu(architecture: str | None) -> dict[str, Any]:
    return {
        "architecture": (
            fact("known", value=architecture, evidence_refs=("platform:machine",))
            if architecture else unknown("not_reported", "platform:machine")
        ),
        "model": unknown("not_reported", "platform:cpu"),
        "physical_cores": unknown("not_reported", "platform:cpu-count"),
        "logical_processors": unknown("not_reported", "platform:cpu-count"),
        "features": unknown("not_reported", "platform:cpu-features"),
    }


def _common_memory() -> dict[str, Any]:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        value = pages * page_size
    except (AttributeError, OSError, ValueError):
        value = 0
    return (
        fact("known", value=value, evidence_refs=("platform:physical-memory",))
        if isinstance(value, int) and value > 0
        else unknown("not_reported", "platform:physical-memory")
    )


def _bounded_read(path: Path, limit: int) -> str | None:
    if limit < 1 or limit > 256 * 1024:
        raise ValueError("read limit is invalid")
    try:
        with path.open("rb") as handle:
            payload = handle.read(limit + 1)
        if len(payload) > limit:
            return None
        return payload.decode("utf-8", errors="replace")
    except (OSError, PermissionError):
        return None


def _bounded_run(argv: Sequence[str], timeout: float, output_limit: int) -> str | None:
    command = tuple(argv)
    if not _command_is_allowlisted(command) or not 0 < timeout <= _COMMAND_TIMEOUT_SECONDS:
        raise ValueError("command is not allowlisted")
    if not 1 <= output_limit <= _MAX_COMMAND_OUTPUT:
        raise ValueError("command output limit is invalid")
    try:
        process = subprocess.Popen(
            command, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        return None
    assert process.stdout is not None
    captured: list[bytes] = []
    read_done = threading.Event()

    def read_bounded() -> None:
        try:
            captured.append(process.stdout.read(output_limit + 1))
        finally:
            read_done.set()

    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    oversized = False
    while process.poll() is None and time.monotonic() < deadline:
        if read_done.wait(0.01) and captured and len(captured[0]) > output_limit:
            oversized = True
            break
    timed_out = process.poll() is None and time.monotonic() >= deadline
    if oversized or timed_out:
        process.kill()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return None
    reader.join(timeout=0.5)
    try:
        process.stdout.close()
    except OSError:
        pass
    if reader.is_alive() or oversized or timed_out or process.returncode != 0:
        return None
    payload = captured[0] if captured else b""
    if len(payload) > output_limit:
        return None
    return payload.decode("utf-8", errors="replace").strip() or None


def _command_is_allowlisted(argv: tuple[str, ...]) -> bool:
    if argv in {
        ("/usr/bin/sw_vers", "-productVersion"),
        ("/usr/bin/sw_vers", "-buildVersion"),
        ("/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"),
        _WINDOWS_GPU_COMMAND,
    }:
        return True
    return (
        len(argv) == 3
        and argv[:2] == ("/usr/sbin/sysctl", "-n")
        and argv[2] in {
            "machdep.cpu.brand_string", "hw.physicalcpu", "hw.logicalcpu", "hw.memsize"
        }
    )


def _parse_os_release(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in value.splitlines()[:128]:
        key, separator, raw = line.partition("=")
        if separator and key in {"NAME", "PRETTY_NAME", "VERSION_ID"}:
            result[key] = raw.strip().strip('"\'')[:256]
    return result


def _first_cpu_value(value: str, keys: Sequence[str]) -> str | None:
    wanted = {key.casefold() for key in keys}
    for line in value.splitlines()[:4096]:
        key, separator, raw = line.partition(":")
        if separator and key.strip().casefold() in wanted:
            text = raw.strip()
            return text[:256] if text else None
    return None


def _linux_memory_bytes(value: str) -> int | None:
    match = re.search(r"(?m)^MemTotal:\s+(\d+)\s+kB\s*$", value)
    if match is None:
        return None
    kib = int(match.group(1))
    return kib * 1024 if kib > 0 else None


def _parse_macos_displays(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        root = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    rows = root.get("SPDisplaysDataType", []) if isinstance(root, dict) else []
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:16] if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        name = _safe_text(row.get("sppci_model") or row.get("_name"))
        if not name:
            continue
        result.append({
            "adapter_id": f"gpu-{index}", "name": name,
            "vendor_id": _safe_hex(row.get("spdisplays_vendor-id")),
            "device_id": _safe_hex(row.get("spdisplays_device-id")),
            "memory": {"kind": "reported_text", "bytes": None},
            "driver_version": None,
            "apis": (["metal"] if row.get("spdisplays_metal") else []),
        })
    return result


def _parse_windows_displays(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    rows = decoded if isinstance(decoded, list) else [decoded]
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:16]):
        if not isinstance(row, dict):
            continue
        name = _safe_text(row.get("Name"))
        if not name:
            continue
        result.append({
            "adapter_id": f"gpu-{index}", "name": name,
            "vendor_id": None, "device_id": None,
            # Win32_VideoController.AdapterRAM is often truncated or otherwise
            # unreliable; do not turn it into compatibility evidence.
            "memory": {"kind": "unknown", "bytes": None},
            "driver_version": _safe_text(row.get("DriverVersion")), "apis": [],
        })
    return result


def _validate_fact(value: Any) -> None:
    if not isinstance(value, Mapping) or value.get("state") not in FACT_STATES:
        raise ValueError("fact wrapper is invalid")
    allowed = {"state", "value", "reason_code", "evidence_refs"}
    if not set(value) <= allowed or "evidence_refs" not in value:
        raise ValueError("fact wrapper fields are invalid")
    refs = value["evidence_refs"]
    if not isinstance(refs, list) or not refs or refs != sorted(set(refs)) or any(
        not isinstance(ref, str) or _EVIDENCE_REF.fullmatch(ref) is None for ref in refs
    ):
        raise ValueError("fact evidence references are invalid")
    if value["state"] == "known":
        if "value" not in value or value["value"] is None or "reason_code" in value:
            raise ValueError("known fact is invalid")
    else:
        if "value" in value:
            raise ValueError("unknown fact cannot contain a value")
        if "reason_code" not in value:
            raise ValueError("non-known fact requires a reason code")
    reason = value.get("reason_code")
    if reason is not None and (
        not isinstance(reason, str)
        or re.fullmatch(r"[a-z0-9_]{1,64}", reason) is None
    ):
        raise ValueError("fact reason code is invalid")


def _validate_profile_values(profile: Mapping[str, Any]) -> None:
    def known(section: str, name: str) -> Any:
        item = profile[section][name]
        return item.get("value") if item["state"] == "known" else None

    for name in ("family", "name", "version", "build", "kernel", "model"):
        section = "cpu" if name == "model" else "os"
        value = known(section, name)
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 256):
            raise ValueError("system profile text fact is invalid")
    architecture = known("cpu", "architecture")
    family = known("os", "family")
    if family is not None and family not in {"linux", "macos", "windows"}:
        raise ValueError("OS family is invalid")
    if architecture is not None and architecture not in {
        "x86_64", "x86", "arm64", "armv7", "ppc64le", "riscv64"
    }:
        raise ValueError("CPU architecture is invalid")
    for name in ("physical_cores", "logical_processors"):
        value = known("cpu", name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4096
        ):
            raise ValueError("CPU count is invalid")
    features = known("cpu", "features")
    if features is not None and (
        not isinstance(features, list)
        or features != sorted(set(features))
        or any(item not in _FEATURE_ALLOWLIST for item in features)
    ):
        raise ValueError("CPU features are invalid")
    memory = known("memory", "total_bytes")
    if memory is not None and (
        isinstance(memory, bool) or not isinstance(memory, int) or not 1 <= memory <= (1 << 63) - 1
    ):
        raise ValueError("memory total is invalid")

    graphics = profile["graphics"]
    if graphics["state"] == "known":
        adapters = graphics["value"]
        if not isinstance(adapters, list) or not 1 <= len(adapters) <= 16:
            raise ValueError("graphics adapters are invalid")
        for index, adapter in enumerate(adapters):
            if not isinstance(adapter, Mapping) or set(adapter) != {
                "adapter_id", "name", "vendor_id", "device_id", "memory",
                "driver_version", "apis",
            }:
                raise ValueError("graphics adapter fields are invalid")
            if adapter["adapter_id"] != f"gpu-{index}" or _safe_text(adapter["name"]) is None:
                raise ValueError("graphics adapter identity is invalid")
            for key in ("vendor_id", "device_id"):
                if adapter[key] is not None and re.fullmatch(r"[0-9a-f]{4}", adapter[key]) is None:
                    raise ValueError("graphics PCI identifier is invalid")
            adapter_memory = adapter["memory"]
            if not isinstance(adapter_memory, Mapping) or set(adapter_memory) != {"kind", "bytes"}:
                raise ValueError("graphics memory is invalid")
            if adapter_memory["kind"] not in {"dedicated", "shared", "unified", "reported_text", "unknown"}:
                raise ValueError("graphics memory kind is invalid")
            if adapter_memory["bytes"] is not None and _positive_int(adapter_memory["bytes"]) is None:
                raise ValueError("graphics memory bytes are invalid")
            if adapter["driver_version"] is not None and _safe_text(adapter["driver_version"]) is None:
                raise ValueError("graphics driver version is invalid")
            apis = adapter["apis"]
            if not isinstance(apis, list) or apis != sorted(set(apis)) or any(
                item not in {"directx", "metal", "opengl", "vulkan"} for item in apis
            ):
                raise ValueError("graphics APIs are invalid")

    storage = profile["storage"]
    if storage["state"] == "known":
        roles = storage["value"]
        if not isinstance(roles, list) or not 1 <= len(roles) <= 8:
            raise ValueError("storage roles are invalid")
        seen_roles: set[str] = set()
        for item in roles:
            if not isinstance(item, Mapping) or set(item) != {
                "role", "capacity_bytes", "available_bytes", "filesystem", "medium"
            }:
                raise ValueError("storage role fields are invalid")
            if item["role"] not in {"system", "steam_primary", "steam_secondary"} or item["role"] in seen_roles:
                raise ValueError("storage role is invalid")
            seen_roles.add(item["role"])
            capacity = _positive_int(item["capacity_bytes"])
            available = _positive_int(item["available_bytes"], allow_zero=True)
            if capacity is None or available is None or available > capacity:
                raise ValueError("storage capacity is invalid")
            if item["filesystem"] is not None and (
                not isinstance(item["filesystem"], str)
                or re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", item["filesystem"]) is None
            ):
                raise ValueError("filesystem kind is invalid")
            if item["medium"] not in {"ssd", "hdd", "removable", "network", "unknown"}:
                raise ValueError("storage medium is invalid")
    for name in ("gamepad", "vr"):
        item = profile[name]
        if item["state"] == "known" and not isinstance(item["value"], bool):
            raise ValueError("peripheral presence is invalid")


def _reject_private_material(value: Any, key: str = "") -> None:
    normalized_key = key.casefold()
    if any(part in normalized_key for part in _FORBIDDEN_KEY_PARTS):
        raise ValueError("system profile contains a private field")
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise ValueError("system profile keys must be strings")
            _reject_private_material(item, item_key)
    elif isinstance(value, list):
        for item in value:
            _reject_private_material(item, key)
    elif isinstance(value, str):
        ip_sensitive = normalized_key not in {
            "version", "build", "kernel", "driver_version"
        }
        if (
            len(value) > 512
            or _PATH.search(value)
            or (ip_sensitive and _IP.search(value))
            or _MAC.search(value)
            or _UUID.search(value)
        ):
            raise ValueError("system profile contains private material")


def _nested_fact_known(profile: Mapping[str, Any], section: str, field_name: str) -> bool:
    section_value = profile.get(section)
    return isinstance(section_value, Mapping) and isinstance(section_value.get(field_name), Mapping) and section_value[field_name].get("state") == "known"


def canonical_architecture(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().casefold().replace("-", "_")
    canonical = {
        "amd64": "x86_64", "x64": "x86_64", "x86_64": "x86_64",
        "i386": "x86", "i486": "x86", "i586": "x86", "i686": "x86", "x86": "x86",
        "aarch64": "arm64", "arm64": "arm64", "armv7l": "armv7",
        "armv7": "armv7", "ppc64le": "ppc64le", "riscv64": "riscv64",
    }.get(normalized)
    return canonical


def _known_text(value: Any, ref: str) -> dict[str, Any]:
    text = _safe_text(value)
    return fact("known", value=text, evidence_refs=(ref,)) if text else unknown("not_reported", ref)


def _known_positive_int(value: Any, ref: str) -> dict[str, Any]:
    number = _positive_int(value)
    return fact("known", value=number, evidence_refs=(ref,)) if number else unknown("not_reported", ref)


def _positive_int(value: Any, *, allow_zero: bool = False) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or (number == 0 and not allow_zero) or number > (1 << 63) - 1:
        return None
    return number


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_text(value: Any) -> str | None:
    text = _string(value)
    if text is None or len(text) > 256 or _PATH.search(text) or _IP.search(text) or _MAC.search(text) or _UUID.search(text):
        return None
    return text


def _safe_hex(value: Any) -> str | None:
    text = _string(value)
    if text is None:
        return None
    match = re.search(r"(?:0x)?([0-9A-Fa-f]{4})", text)
    return None if match is None else match.group(1).lower()
