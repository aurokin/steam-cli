import psutil
import pytest

from steam_agent.execution import runtime


class OwnedProcess:
    def __init__(self, environment):
        self.environment = environment
        self.pid = 987654321

    def username(self):
        return "synthetic-owner"

    def create_time(self):
        return 1000.0

    def name(self):
        return "synthetic-owned-fixture.exe"

    def status(self):
        return psutil.STATUS_RUNNING

    def environ(self):
        return self.environment


def read_owned_windows_process(monkeypatch, environment):
    process = OwnedProcess(environment)
    monkeypatch.setattr(runtime.psutil, "Process", lambda pid=None: process)
    monkeypatch.setattr(runtime, "windows_session_id", lambda pid: 2)
    backend = runtime.PsutilBackend(platform="win32")
    return backend.read(987654321)


def test_windows_process_canonicalizes_only_appid_environment(monkeypatch):
    record = read_owned_windows_process(monkeypatch, {
        "STEAMAPPID": "480", "steamgameid": "480", "Steam_Compat_App_Id": "480",
        "STEAM_WEB_API_KEY": "synthetic-secret", "UNRELATED": "synthetic",
    })
    assert record is not None
    assert record.app_environment == {
        "SteamAppId": "480", "SteamGameId": "480", "STEAM_COMPAT_APP_ID": "480",
    }
    assert runtime._attribution(record) == (
        480, ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID"), False,
    )


def test_windows_same_value_aliases_collapse(monkeypatch):
    record = read_owned_windows_process(monkeypatch, {"STEAMAPPID": "480", "SteamAppId": "480"})
    assert record is not None
    assert record.app_environment == {"SteamAppId": "480"}


def test_windows_conflicting_case_aliases_refuse_process_evidence(monkeypatch):
    with pytest.raises(runtime.ProbeError, match="process_probe_failed"):
        read_owned_windows_process(monkeypatch, {"STEAMAPPID": "480", "SteamAppId": "481"})


def test_linux_appid_environment_remains_case_sensitive():
    assert runtime._app_environment({
        "STEAMAPPID": "481", "SteamAppId": "480", "steamgameid": "480",
        "STEAM_WEB_API_KEY": "synthetic-secret",
    }, platform="linux") == {"SteamAppId": "480"}
