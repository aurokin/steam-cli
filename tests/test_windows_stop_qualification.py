"""Portable checks for native fixture confinement, without creating windows."""

import importlib.util
import json
from pathlib import Path

import pytest

from steam_agent.execution.runtime import ProbeError, ProcessRecord, PsutilBackend


@pytest.fixture
def script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "qualify_windows_stop.py"
    spec = importlib.util.spec_from_file_location("windows_stop_qualification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(pid=42, created=1.0):
    return ProcessRecord(pid, created, "python.exe", {"SteamAppId": "4294967295"}, True, True)


def test_fixture_scan_reads_only_spawned_identity_and_refuses_recycled_pid(script, monkeypatch):
    calls = []

    def read(_self, pid):
        calls.append(pid)
        return record()

    monkeypatch.setattr(PsutilBackend, "read", read)
    backend = script.OwnedBackend(42, 1.0, None)
    assert backend.scan().processes == (record(),)
    with pytest.raises(ProbeError, match="not_owned"):
        backend.read(43)
    assert calls == [42]
    monkeypatch.setattr(PsutilBackend, "read", lambda *_: record(created=2.0))
    with pytest.raises(ProbeError, match="identity_changed"):
        backend.scan()


@pytest.mark.parametrize("target,force", [(record(pid=43), False), (record(created=2.0), False), (record(), True)])
def test_fixture_stop_refuses_foreign_identity_and_force_before_native_call(script, monkeypatch, target, force):
    monkeypatch.setattr(PsutilBackend, "request_stop", lambda *_args, **_kwargs: pytest.fail("unsafe native request"))
    with pytest.raises(ProbeError, match="stop_not_owned"):
        script.OwnedBackend(42, 1.0, None).request_stop(target, force=force)


def test_fixture_windows_filter_other_owners_and_recheck_before_post(script):
    class Windows:
        posts = []
        owners = {1: 42, 2: 43}

        def windows(self):
            return (1, 2)

        def owner(self, window):
            return self.owners[window]

        def close(self, window):
            self.posts.append(window)
            return True

    native = Windows()
    windows = script.OwnedWindows(42, native, class_name=lambda _: script.WINDOW_CLASS)
    assert windows.windows() == (1,)
    assert windows.close(1) and windows.close_count == 1
    native.owners[1] = 43
    with pytest.raises(ProbeError, match="not_owned"):
        windows.close(1)
    assert native.posts == [1]


def test_fixture_never_posts_to_same_pid_helper_class(script):
    class Windows:
        def windows(self):
            return (1, 2)

        def owner(self, _window):
            return 42

        def close(self, _window):
            pytest.fail("helper window posted")

    windows = script.OwnedWindows(42, Windows(), class_name=lambda window: script.WINDOW_CLASS if window == 1 else "RuntimeHelper")
    assert windows.windows() == (1,)
    with pytest.raises(ProbeError, match="not_owned"):
        windows.close(2)


@pytest.mark.parametrize("changed", ["foreign_parent", "recycled_owner", "recycled_spawn"])
def test_fixture_rejects_unproved_redirector_descendant(script, changed):
    identities = {42: (1.0, 0), 43: (2.0, 42), 44: (3.0, 43)}
    if changed == "foreign_parent":
        identities[43] = (2.0, 0)
    elif changed == "recycled_owner":
        identities[44] = (4.0, 43)
    else:
        identities[42] = (1.5, 0)

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return identities[self.pid][0]

        def ppid(self):
            return identities[self.pid][1]

    with pytest.raises(ProbeError, match="descendant_unconfirmed"):
        script.verify_descendant(44, 3.0, 42, 1.0, process_factory=Process)


def test_fixture_proves_redirector_parent_chain_before_targeting(script):
    identities = {42: (1.0, 0), 43: (2.0, 42), 44: (3.0, 43)}

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return identities[self.pid][0]

        def ppid(self):
            return identities[self.pid][1]

    script.verify_descendant(44, 3.0, 42, 1.0, process_factory=Process)
    script.verify_descendant(42, 1.0, 42, 1.0, process_factory=Process)


def test_fixture_service_session_fails_before_spawning_with_coarse_json(script, monkeypatch, capsys):
    monkeypatch.setattr(script.sys, "platform", "win32")
    monkeypatch.setattr(script, "windows_session_id", lambda _: 0)
    monkeypatch.setattr(script, "qualify_case", lambda *_: pytest.fail("service session spawned child"))
    assert script.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema": "windows-stop-qualification/1", "qualified": False,
        "phase": "interactive_session", "reason_code": "native_qualification_failed", "steam_actions": 0,
    }


def test_fixture_failure_never_formats_native_exception_text(script, monkeypatch, capsys):
    monkeypatch.setattr(script.sys, "platform", "win32")
    monkeypatch.setattr(script, "windows_session_id", lambda _: 1)

    def fail(_mode):
        raise OSError("synthetic-private-canary C:\\private-fixture")

    monkeypatch.setattr(script, "qualify_case", fail)
    assert script.main() == 1
    captured = capsys.readouterr()
    assert "canary" not in captured.out + captured.err
    assert json.loads(captured.out)["phase"] == "graceful"
