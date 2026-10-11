"""Runtime tests use synthetic identities and never inspect or stop host processes."""

from collections.abc import Callable
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from steam_agent.execution.runtime import (
    ObservedProcess, ProcessRecord, ProcessScan, ProbeError, PsutilBackend,
    RuntimeObserver, StopRequestError, StopResult, _windows_close,
)


def game(pid: int = 100, appid: str = "480", **changes) -> ProcessRecord:
    return replace(ProcessRecord(pid, 1.0, "game.exe", {"SteamAppId": appid}, True), **changes)


class FakeBackend:
    def __init__(self, *processes: ProcessRecord) -> None:
        self.processes = {process.pid: process for process in processes}
        self.reasons: tuple[str, ...] = ()
        self.requests: list[tuple[int, bool]] = []
        self.exit_on_request = True
        self.windowless: set[int] = set()
        self.read_replacement: ProcessRecord | None = None
        self.read_error = False

    def scan(self) -> ProcessScan:
        return ProcessScan(tuple(self.processes.values()), self.reasons)

    def read(self, pid: int) -> ProcessRecord | None:
        if self.read_error:
            raise ProbeError("synthetic")
        return self.read_replacement or self.processes.get(pid)

    def request_stop(
        self, process: ProcessRecord, *, force: bool,
        before_request: Callable[[], bool] | None = None,
    ) -> bool:
        self.requests.append((process.pid, force))
        if process.pid in self.windowless and not force:
            return False
        if self.exit_on_request:
            self.processes.pop(process.pid, None)
        return True


def observer(backend: FakeBackend, platform: str = "linux") -> RuntimeObserver:
    return RuntimeObserver(platform=platform, backend=backend, sleep=lambda _: None)


@pytest.mark.parametrize("variable", ["SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID"])
def test_observes_manually_started_game_by_environment(variable: str) -> None:
    record = game(app_environment={variable: "480"})
    result = observer(FakeBackend(record)).observe(480)
    assert result.state == "observed"
    assert result.processes == (ObservedProcess(480, 100, 1.0, (variable,)),)


def test_name_alone_and_other_user_or_session_never_attribute_game() -> None:
    records = [game(app_environment={}), game(101, same_user=False), game(102, same_session=False)]
    assert observer(FakeBackend(*records), "win32").observe().state == "absent"


@pytest.mark.parametrize("name", ["steam.exe", "steamwebhelper", "wineserver", "reaper"])
def test_shared_infrastructure_is_never_a_game_stop_target(name: str) -> None:
    backend = FakeBackend(game(name=name))
    assert observer(backend).stop_app(480).state == "absent"
    assert backend.requests == []


@pytest.mark.parametrize("environment", [
    {"SteamAppId": "480", "SteamGameId": "481"},
    {"SteamAppId": "invalid"}, {"SteamAppId": "0"}, {"SteamAppId": "4294967296"},
    {"SteamAppId": "9" * 5000}, {"SteamAppId": "0" * 5000 + "480"},
])
def test_invalid_or_conflicting_environment_is_unknown(environment) -> None:
    backend = FakeBackend(game(app_environment=environment))
    result = observer(backend).stop_app(480)
    assert result.state == "unknown"
    assert backend.requests == []


def test_partial_scan_requests_only_positive_target_and_cannot_confirm_absence() -> None:
    backend = FakeBackend(game())
    backend.reasons = ("process_probe_failed",)
    result = observer(backend).observe(480)
    assert result.state == "unknown"
    assert len(result.processes) == 1
    stopped = observer(backend).stop_app(480)
    assert stopped.state == "unknown"
    assert stopped.reason_code == "stop_confirmation_unknown"
    assert stopped.process_count == stopped.requested_count == 1
    assert backend.requests == [(100, False)]
    assert backend.processes == {}


def test_partial_scan_without_positive_target_refuses_stop() -> None:
    backend = FakeBackend(game(101, "481"))
    backend.reasons = ("process_probe_failed",)
    result = observer(backend).stop_app(480)
    assert result.state == "unknown"
    assert result.reason_code == "runtime_observation_unknown"
    assert result.process_count == result.requested_count == 0
    assert backend.requests == []


def test_hidden_same_app_sibling_is_not_selected_after_partial_scan() -> None:
    class PartialBackend(FakeBackend):
        def scan(self) -> ProcessScan:
            # The sibling's environment was inaccessible on the first scan.
            if not self.requests:
                return ProcessScan((self.processes[100],), ("process_probe_failed",))
            return super().scan()

    backend = PartialBackend(game(100), game(101))
    result = observer(backend).stop_app(480, timeout=0)
    assert result.state == "deferred"
    assert result.reason_code == "stop_timeout"
    assert result.process_count == result.requested_count == 1
    assert backend.requests == [(100, False)]
    assert 101 in backend.processes


def test_partial_scan_can_confirm_exit_only_after_complete_scan() -> None:
    class RecoveringBackend(FakeBackend):
        def scan(self) -> ProcessScan:
            if not self.requests:
                return ProcessScan(tuple(self.processes.values()), ("process_probe_failed",))
            return super().scan()

    backend = RecoveringBackend(game())
    result = observer(backend).stop_app(480)
    assert result.state == "stopped"
    assert result.requested_count == 1
    assert backend.requests == [(100, False)]


def test_partial_scan_still_rejects_reused_pid_before_request() -> None:
    backend = FakeBackend(game())
    backend.reasons = ("process_probe_failed",)
    backend.read_replacement = game(create_time=2.0)
    result = observer(backend).stop_app(480)
    assert result.reason_code == "process_identity_changed"
    assert result.requested_count == 0
    assert backend.requests == []


def test_identity_failure_preserves_count_of_requests_already_sent() -> None:
    class ReusedSiblingBackend(FakeBackend):
        def read(self, pid: int) -> ProcessRecord | None:
            return game(101, create_time=2.0) if pid == 101 else super().read(pid)

    backend = ReusedSiblingBackend(game(100), game(101))
    backend.reasons = ("process_probe_failed",)
    result = observer(backend).stop_app(480)
    assert result.reason_code == "process_identity_changed"
    assert result.process_count == 2
    assert result.requested_count == 1
    assert backend.requests == [(100, False)]


def test_dispatch_gate_is_checked_after_identity_read_before_each_request() -> None:
    backend = FakeBackend(game(100), game(101))
    reads: list[int] = []
    original_read = backend.read

    def read(pid: int) -> ProcessRecord | None:
        reads.append(pid)
        return original_read(pid)

    backend.read = read

    def permitted() -> bool:
        assert reads[-1] == 100 + len(backend.requests)
        return not backend.requests

    result = observer(backend).stop_app(480, before_request=permitted)
    assert result.state == "deferred"
    assert result.reason_code == "stop_dispatch_gate_denied"
    assert result.process_count == 2
    assert result.requested_count == 1
    assert backend.requests == [(100, False)]
    assert 101 in backend.processes


@pytest.mark.parametrize("error", [
    OSError("private gate path"), ProbeError("private gate detail"),
    ValueError("private malformed policy"),
])
def test_unreadable_dispatch_gate_preserves_prior_requests_and_sanitizes(error) -> None:
    backend = FakeBackend(game(100), game(101))

    def permitted() -> bool:
        if backend.requests:
            raise error
        return True

    result = observer(backend).stop_app(480, before_request=permitted)
    assert result.state == "unknown"
    assert result.reason_code == "stop_dispatch_gate_unreadable"
    assert result.requested_count == 1
    assert backend.requests == [(100, False)]
    assert "private" not in json.dumps(result.to_json())


def test_partial_scan_no_window_never_escalates_and_remains_unknown() -> None:
    backend = FakeBackend(game())
    backend.reasons = ("process_probe_failed",)
    backend.windowless.add(100)
    result = observer(backend, "win32").stop_app(480, timeout=0)
    assert result.state == "unknown"
    assert result.reason_code == "stop_confirmation_unknown"
    assert result.requested_count == 0
    assert backend.requests == [(100, False)]


def test_stop_result_existing_positional_fields_remain_compatible() -> None:
    assert StopResult("unknown", "synthetic", 1, False).to_json()["requested_count"] == 0


def test_stop_only_requests_selected_app_and_confirms_exit() -> None:
    backend = FakeBackend(game(), game(101, "481"))
    result = observer(backend).stop_app(480)
    assert result.state == "stopped"
    assert result.requested_count == 1
    assert backend.requests == [(100, False)]
    assert 101 in backend.processes


@pytest.mark.parametrize("replacement", [
    game(create_time=2.0), game(app_environment={"SteamAppId": "481"}),
    game(same_user=False), game(same_session=False), game(name="steam.exe"),
])
def test_identity_and_attribution_rechecked_before_stop(replacement) -> None:
    backend = FakeBackend(game())
    backend.read_replacement = replacement
    assert observer(backend).stop_app(480).reason_code == "process_identity_changed"
    assert backend.requests == []


def test_pre_stop_probe_failure_never_requests_termination() -> None:
    backend = FakeBackend(game())
    backend.read_error = True
    assert observer(backend).stop_app(480).state == "unknown"
    assert backend.requests == []


def test_timeout_never_escalates_to_force() -> None:
    backend = FakeBackend(game())
    backend.exit_on_request = False
    result = observer(backend).stop_app(480, timeout=0)
    assert result.reason_code == "stop_timeout"
    assert result.state == "deferred"
    assert backend.requests == [(100, False)]


def test_windowless_process_does_not_block_close_request_to_game_window() -> None:
    backend = FakeBackend(game(100), game(101))
    backend.windowless.add(100)
    result = observer(backend, "win32").stop_app(480, timeout=0)
    assert backend.requests == [(100, False), (101, False)]
    assert result.state == "deferred"
    assert 100 in backend.processes


def test_force_is_only_sent_when_explicitly_requested() -> None:
    backend = FakeBackend(game())
    backend.windowless.add(100)
    assert observer(backend, "win32").stop_app(480).reason_code == "close_request_unavailable"
    assert observer(backend, "win32").stop_app(480, force=True).state == "stopped"
    assert backend.requests == [(100, False), (100, True)]


def test_runtime_json_has_no_process_name_environment_or_account_data() -> None:
    backend = FakeBackend(game(name="private-name.exe", app_environment={
        "SteamAppId": "480", "SECRET": "sensitive", "HOME": "/private/example",
    }))
    payload = json.dumps(observer(backend).observe().to_json())
    assert "os_process_observed" in payload
    assert all(value not in payload for value in ["private-name", "sensitive", "/private", "SECRET"])


def test_unsupported_platform_fails_closed() -> None:
    backend = FakeBackend(game())
    assert observer(backend, "darwin").stop_app(480).state == "unknown"
    assert backend.requests == []


def test_psutil_windows_graceful_stop_uses_close_not_terminate(monkeypatch) -> None:
    record = game()

    class SyntheticProcess:
        def create_time(self) -> float:
            return 1.0

        def kill(self) -> None:
            calls.append("kill")

        def terminate(self) -> None:
            raise AssertionError("Windows terminate is forceful and must not be used for graceful stop")

    calls: list[str] = []
    backend = PsutilBackend(platform="win32")
    monkeypatch.setattr(backend, "read", lambda _: record)
    monkeypatch.setattr("steam_agent.execution.runtime.psutil.Process", lambda _: SyntheticProcess())
    monkeypatch.setattr(
        "steam_agent.execution.runtime._windows_close",
        lambda _, **kwargs: calls.append("close") or True,
    )
    assert backend.request_stop(record, force=False) is True
    assert backend.request_stop(record, force=True) is True
    assert calls == ["close", "kill"]


class SyntheticWindows:
    def __init__(self) -> None:
        self.owners = {10: 100, 11: 999, 12: 100}
        self.closed: list[int] = []
        self.next_owner: int | None = None
        self.owner_calls: dict[int, int] = {}

    def windows(self) -> tuple[int, ...]:
        return tuple(self.owners)

    def owner(self, window: int) -> int:
        self.owner_calls[window] = self.owner_calls.get(window, 0) + 1
        if self.next_owner is not None and self.owner_calls[window] > 1:
            return self.next_owner
        return self.owners[window]

    def close(self, window: int) -> bool:
        self.closed.append(window)
        return True


def test_windows_close_only_posts_to_attributed_process_windows() -> None:
    windows = SyntheticWindows()
    assert _windows_close(game(), windows=windows, identity_check=lambda: True)
    assert windows.closed == [10, 12]


def test_windows_close_rechecks_window_owner_before_each_request() -> None:
    windows = SyntheticWindows()
    windows.next_owner = 999
    with pytest.raises(ProbeError, match="window_identity_changed"):
        _windows_close(game(), windows=windows, identity_check=lambda: True)
    assert windows.closed == []


def test_windows_close_rechecks_process_identity_before_each_request() -> None:
    windows = SyntheticWindows()
    checks = iter([True, False])
    with pytest.raises(ProbeError, match="process_identity_changed"):
        _windows_close(game(), windows=windows, identity_check=lambda: next(checks))
    assert windows.closed == [10]


def _native_windows_observer(monkeypatch, windows: SyntheticWindows) -> RuntimeObserver:
    record = game()

    class SyntheticProcess:
        def create_time(self) -> float:
            return record.create_time

        def kill(self) -> None:
            raise AssertionError("Graceful Windows closing must never force termination")

        def terminate(self) -> None:
            raise AssertionError("Graceful Windows closing must never force termination")

    backend = PsutilBackend(platform="win32", windows=windows)
    monkeypatch.setattr(backend, "read", lambda _: record)
    monkeypatch.setattr(backend, "scan", lambda: ProcessScan((record,)))
    monkeypatch.setattr("steam_agent.execution.runtime.psutil.Process", lambda _: SyntheticProcess())
    return RuntimeObserver(platform="win32", backend=backend)


@pytest.mark.parametrize("failure", ["denied", "unreadable", "exception"])
def test_windows_gate_rechecked_before_each_native_close_preserves_process_count(
    monkeypatch, failure
) -> None:
    windows = SyntheticWindows()
    runtime = _native_windows_observer(monkeypatch, windows)

    def gate() -> bool:
        if not windows.closed:
            return True
        if failure == "unreadable":
            raise OSError("private policy path")
        if failure == "exception":
            raise ValueError("private corrupt policy")
        return False

    result = runtime.stop_app(480, before_request=gate)

    assert windows.closed == [10]
    assert result.requested_count == result.process_count == 1
    assert result.force is False
    assert result.state == ("deferred" if failure == "denied" else "unknown")
    assert result.reason_code == (
        "stop_dispatch_gate_denied" if failure == "denied" else "stop_dispatch_gate_unreadable"
    )
    assert "private" not in json.dumps(result.to_json())


def test_windows_gate_rechecked_after_slow_owner_read_before_first_close(monkeypatch) -> None:
    windows = SyntheticWindows()
    runtime = _native_windows_observer(monkeypatch, windows)
    original_owner = windows.owner
    allowed = True

    def slow_owner(window: int) -> int:
        nonlocal allowed
        owner = original_owner(window)
        if windows.owner_calls[window] > 1:
            allowed = False
        return owner

    windows.owner = slow_owner

    result = runtime.stop_app(480, before_request=lambda: allowed)

    assert result.reason_code == "stop_dispatch_gate_denied"
    assert result.requested_count == 0
    assert windows.closed == []


@pytest.mark.parametrize("failure", ["owner_changed", "owner_error", "close_failed", "close_error", "identity"])
def test_windows_partial_close_probe_failure_keeps_sent_process_count(
    monkeypatch, failure
) -> None:
    windows = SyntheticWindows()
    runtime = _native_windows_observer(monkeypatch, windows)
    original_close = windows.close
    original_owner = windows.owner

    def close(window: int) -> bool:
        if window == 12:
            if failure == "close_failed":
                return False
            if failure == "close_error":
                raise ProbeError("private backend close detail")
        return original_close(window)

    def owner(window: int) -> int:
        if windows.closed and window == 12:
            if failure == "owner_changed":
                return 999
            if failure == "owner_error":
                raise ProbeError("private backend owner detail")
        return original_owner(window)

    windows.close = close
    windows.owner = owner
    if failure == "identity":
        monkeypatch.setattr(
            runtime.backend, "read",
            lambda _: game(create_time=2.0) if windows.closed else game(),
        )

    result = runtime.stop_app(480, timeout=0)

    assert result.state == "unknown"
    assert result.reason_code == "stop_probe_failed"
    assert result.requested_count == 1
    assert windows.closed == [10]
    assert "private" not in json.dumps(result.to_json())


def test_windows_all_native_closes_count_as_one_attributed_process(monkeypatch) -> None:
    windows = SyntheticWindows()
    runtime = _native_windows_observer(monkeypatch, windows)

    result = runtime.stop_app(480, timeout=0)

    assert windows.closed == [10, 12]
    assert result.requested_count == 1
    assert result.reason_code == "stop_timeout"


@pytest.mark.parametrize("failure", ["owner_changed", "close_failed", "close_error"])
def test_windows_failure_before_any_native_effect_reports_zero_requests(
    monkeypatch, failure
) -> None:
    windows = SyntheticWindows()
    runtime = _native_windows_observer(monkeypatch, windows)
    if failure == "owner_changed":
        windows.next_owner = 999
    else:
        def close(_: int) -> bool:
            if failure == "close_error":
                raise ProbeError("private native error")
            return False

        windows.close = close

    result = runtime.stop_app(480, timeout=0)

    assert result.state == "unknown"
    assert result.reason_code == "stop_probe_failed"
    assert result.requested_count == 0
    assert windows.closed == []
    assert "private" not in json.dumps(result.to_json())


def test_process_gone_before_backend_request_is_not_counted_as_dispatched(monkeypatch) -> None:
    record = game()
    reads = iter((record, None))
    backend = PsutilBackend(platform="linux")
    monkeypatch.setattr(backend, "read", lambda _: next(reads))
    scans = iter((ProcessScan((record,)), ProcessScan()))
    monkeypatch.setattr(backend, "scan", lambda: next(scans))

    result = RuntimeObserver(platform="linux", backend=backend).stop_app(480)

    assert result.state == "stopped"
    assert result.requested_count == 0


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("failure", ["denied", "unreadable"])
def test_linux_final_gate_follows_slow_backend_identity_read(
    monkeypatch, force, failure
) -> None:
    record = game()
    reads = 0
    calls: list[str] = []

    class SyntheticProcess:
        def create_time(self) -> float:
            return record.create_time

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

    def read(_: int) -> ProcessRecord:
        nonlocal reads
        reads += 1
        return record

    def gate() -> bool:
        if reads < 2:
            return True
        if failure == "unreadable":
            raise ValueError("private policy error after slow process read")
        return False

    backend = PsutilBackend(platform="linux")
    monkeypatch.setattr(backend, "read", read)
    monkeypatch.setattr(backend, "scan", lambda: ProcessScan((record,)))
    monkeypatch.setattr("steam_agent.execution.runtime.psutil.Process", lambda _: SyntheticProcess())
    result = RuntimeObserver(platform="linux", backend=backend).stop_app(
        480, force=force, before_request=gate,
    )

    assert result.reason_code == (
        "stop_dispatch_gate_denied" if failure == "denied" else "stop_dispatch_gate_unreadable"
    )
    assert result.requested_count == 0
    assert calls == []
    assert "private" not in json.dumps(result.to_json())


def test_partial_request_error_remains_probe_error_compatible() -> None:
    error = StopRequestError("stop_probe_failed", requested=True)
    assert isinstance(error, ProbeError)
    assert error.requested is True


def test_psutil_read_scopes_user_before_reading_environment(monkeypatch) -> None:
    class OtherUserProcess:
        def uids(self):
            return SimpleNamespace(real=2000, effective=2000)

        def environ(self):
            raise AssertionError("Do not inspect another user's environment")

    monkeypatch.setattr("steam_agent.execution.runtime.os.getuid", lambda: 1000, raising=False)
    monkeypatch.setattr("steam_agent.execution.runtime.os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("steam_agent.execution.runtime.os.getpid", lambda: 99)
    monkeypatch.setattr("steam_agent.execution.runtime.psutil.Process", lambda _: OtherUserProcess())
    assert PsutilBackend(platform="linux").read(100) is None


def test_psutil_windows_scopes_session_before_reading_user_environment(monkeypatch) -> None:
    class OtherSessionProcess:
        pid = 100

        def username(self):
            raise AssertionError("Do not inspect another session's account")

        def environ(self):
            raise AssertionError("Do not inspect another session's environment")

    backend = PsutilBackend(platform="win32")
    backend._username = "fixture-user"
    backend._session = 1
    monkeypatch.setattr("steam_agent.execution.runtime.os.getpid", lambda: 99)
    monkeypatch.setattr("steam_agent.execution.runtime.windows_session_id", lambda _: 2)
    monkeypatch.setattr("steam_agent.execution.runtime.psutil.Process", lambda _: OtherSessionProcess())
    assert backend.read(100) is None


def test_windowless_graceful_close_does_not_force_process_exit() -> None:
    windows = SyntheticWindows()
    windows.owners = {11: 999}
    assert _windows_close(game(), windows=windows, identity_check=lambda: True) is False
    assert windows.closed == []
