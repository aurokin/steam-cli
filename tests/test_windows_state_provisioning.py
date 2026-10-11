import json
from pathlib import Path

import pytest

from steam_agent.execution import broker_main
from steam_agent.execution.policy import PolicyError
from steam_agent.execution.windows_filesystem import WindowsFilesystemError


class Identities:
    def __init__(self, unsafe: Path | None = None):
        self.unsafe = unsafe
        self.inspected: list[tuple[Path, bool]] = []

    def identity(self, path, *, directory=True):
        self.inspected.append((path, directory))
        if path == self.unsafe:
            raise WindowsFilesystemError("WINDOWS_REPARSE_POINT")
        assert path.is_dir() if directory else path.is_file()


class PrivateACL:
    def __init__(self):
        self.protected: list[Path] = []

    def protect_directory(self, path):
        self.protected.append(path)


def provision(path: Path, *, unsafe=None):
    files, acl = Identities(unsafe), PrivateACL()
    broker_main._provision_windows_init_state(path, filesystem=files, scheduler=acl)
    return files, acl


def test_new_default_tree_protects_only_created_ancestors(tmp_path):
    chosen = tmp_path / ".local" / "state" / "steam-broker"
    files, acl = provision(chosen)
    assert acl.protected == [tmp_path / ".local", tmp_path / ".local/state", chosen]
    assert files.inspected[0] == (tmp_path, True)
    assert tmp_path not in acl.protected


def test_empty_existing_target_can_be_provisioned(tmp_path):
    _, acl = provision(tmp_path)
    assert acl.protected == [tmp_path]


def test_known_broker_repair_does_not_walk_private_cache(tmp_path):
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({"library": "synthetic", "steamcmd": ""}))
    policy = tmp_path / "policy.toml"
    policy.write_text("[grants]\n")
    cache = tmp_path / "steamcmd-home"
    cache.mkdir()
    (cache / "private").write_text("synthetic")
    files, acl = provision(tmp_path)
    assert acl.protected == [tmp_path, config, policy]
    assert all(cache not in path.parents and path != cache for path, _ in files.inspected)


@pytest.mark.parametrize("config", [None, "broken", '{"library": 1, "steamcmd": ""}'])
def test_foreign_or_corrupt_nonempty_target_refuses_before_acl_change(tmp_path, config):
    (tmp_path / "unrelated").write_text("synthetic")
    if config is not None:
        (tmp_path / "broker.json").write_text(config)
    acl = PrivateACL()
    with pytest.raises(PolicyError, match="explicit private provisioning"):
        broker_main._provision_windows_init_state(tmp_path, filesystem=Identities(), scheduler=acl)
    assert acl.protected == []


def test_native_identity_denial_blocks_root_and_known_file_repair(tmp_path):
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({"library": "synthetic", "steamcmd": ""}))
    for unsafe in (tmp_path, config):
        acl = PrivateACL()
        with pytest.raises(PolicyError, match="explicit private provisioning"):
            broker_main._provision_windows_init_state(tmp_path, filesystem=Identities(unsafe), scheduler=acl)
        assert acl.protected == []


def test_windows_default_init_invokes_private_provisioning(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    actual = broker_main._provision_windows_init_state
    acl = PrivateACL()
    monkeypatch.setattr(broker_main.sys, "platform", "win32")
    monkeypatch.setattr(broker_main, "_default_state_dir", lambda: state)
    monkeypatch.setattr(broker_main, "_provision_windows_init_state", lambda path: actual(path, filesystem=Identities(), scheduler=acl))
    assert broker_main.main(["init", "--library", str(tmp_path / "library")]) == 0
    assert json.loads(capsys.readouterr().out)["initialized"] is True
    assert acl.protected == [state]
