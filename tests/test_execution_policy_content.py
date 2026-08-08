"""Policy fail-closed behavior and content-plane parsing/adoption."""

from __future__ import annotations

from pathlib import Path

import pytest

from steam_agent.execution.content_plane import (
    adopt_manifest,
    locate_manifest,
    manifest_state_flags,
    reconcile_adoption,
)
from steam_agent.execution.policy import PolicyError, load_policy

_MANIFEST = '''"AppState"
{
\t"appid"\t\t"1902490"
\t"installdir"\t\t"steamcmd_dir"
\t"StateFlags"\t\t"4"
}
'''


def _write_policy(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_policy_grants_install_confirm(tmp_path: Path) -> None:
    policy = load_policy(_write_policy(tmp_path, '[grants]\ninstall = "confirm"\n'))
    assert policy.grant_for("install") == "confirm"
    assert policy.grant_for("uninstall") == "deny"
    assert policy.grant_for("launch") == "deny"


def test_policy_unknown_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write_policy(tmp_path, '[grants]\nuninstall = "confirm"\n'))


def test_policy_unattended_value_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(
            _write_policy(tmp_path, '[grants]\ninstall = "allow_unattended"\n')
        )


def test_policy_unknown_top_level_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(
            _write_policy(
                tmp_path, 'allow_unattended = true\n[grants]\ninstall = "confirm"\n'
            )
        )


def test_policy_composite_value_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write_policy(tmp_path, '[grants]\ninstall = ["confirm"]\n'))


def test_policy_missing_grant_is_deny(tmp_path: Path) -> None:
    policy = load_policy(_write_policy(tmp_path, "[grants]\n"))
    assert policy.grant_for("install") == "deny"


def _library(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    (library / "steamapps").mkdir(parents=True)
    return library


def _target_with_manifest(tmp_path: Path, appid: int = 1902490) -> Path:
    target = tmp_path / "target"
    (target / "steamapps").mkdir(parents=True)
    (target / "steamapps" / f"appmanifest_{appid}.acf").write_text(
        _MANIFEST, encoding="utf-8"
    )
    return target


def test_locate_and_adopt_patches_installdir(tmp_path: Path) -> None:
    library = _library(tmp_path)
    target = _target_with_manifest(tmp_path)
    source = locate_manifest(install_dir=target, appid=1902490)
    assert source is not None

    adopted = adopt_manifest(
        source=source,
        library=library,
        appid=1902490,
        install_dir_name="Desk Job",
        journal_dir=tmp_path / "journal",
        operation_id=7,
    )
    text = adopted.read_text(encoding="utf-8")
    assert '"installdir"\t\t"Desk Job"' in text
    assert manifest_state_flags(adopted) == 4


def test_adoption_backs_up_prior_manifest(tmp_path: Path) -> None:
    library = _library(tmp_path)
    prior = library / "steamapps" / "appmanifest_1902490.acf"
    prior.write_text("old", encoding="utf-8")
    target = _target_with_manifest(tmp_path)
    journal_dir = tmp_path / "journal"

    adopt_manifest(
        source=locate_manifest(install_dir=target, appid=1902490),
        library=library,
        appid=1902490,
        install_dir_name="Desk Job",
        journal_dir=journal_dir,
        operation_id=7,
    )
    backup = journal_dir / "appmanifest_1902490.acf.backup"
    assert backup.read_text(encoding="utf-8") == "old"


def test_reconcile_completed_swap_keeps_journal_for_caller(tmp_path: Path) -> None:
    library = _library(tmp_path)
    target = _target_with_manifest(tmp_path)
    journal_dir = tmp_path / "journal"
    adopt_manifest(
        source=locate_manifest(install_dir=target, appid=1902490),
        library=library,
        appid=1902490,
        install_dir_name="Desk Job",
        journal_dir=journal_dir,
        operation_id=7,
    )
    assert (
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
        == "completed"
    )
    # Retirement is the caller's job after the ledger leaves adopting.
    assert (journal_dir / "adoption-1902490.json").exists()


def test_reconcile_torn_write_restores_backup(tmp_path: Path) -> None:
    library = _library(tmp_path)
    prior = library / "steamapps" / "appmanifest_1902490.acf"
    prior.write_text("old", encoding="utf-8")
    target = _target_with_manifest(tmp_path)
    journal_dir = tmp_path / "journal"
    adopted = adopt_manifest(
        source=locate_manifest(install_dir=target, appid=1902490),
        library=library,
        appid=1902490,
        install_dir_name="Desk Job",
        journal_dir=journal_dir,
        operation_id=7,
    )
    adopted.write_text('"AppState" { torn', encoding="utf-8")  # simulate crash

    assert (
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
        == "restored"
    )
    assert prior.read_text(encoding="utf-8") == "old"


def test_adopt_rejects_manifest_without_installdir(tmp_path: Path) -> None:
    from steam_agent.execution.content_plane import AdoptionError

    library = _library(tmp_path)
    target = tmp_path / "target"
    (target / "steamapps").mkdir(parents=True)
    (target / "steamapps" / "appmanifest_1902490.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"1902490"\n\t"StateFlags"\t\t"4"\n}\n',
        encoding="utf-8",
    )
    with pytest.raises(AdoptionError):
        adopt_manifest(
            source=locate_manifest(install_dir=target, appid=1902490),
            library=library,
            appid=1902490,
            install_dir_name="Desk Job",
            journal_dir=tmp_path / "journal",
            operation_id=7,
        )


def test_adopt_refuses_planted_tmp_symlink(tmp_path: Path) -> None:
    from steam_agent.execution.content_plane import AdoptionError

    library = _library(tmp_path)
    target = _target_with_manifest(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("do not overwrite", encoding="utf-8")
    (library / "steamapps" / "appmanifest_1902490.acf.tmp").symlink_to(victim)

    with pytest.raises((AdoptionError, OSError)):
        adopt_manifest(
            source=locate_manifest(install_dir=target, appid=1902490),
            library=library,
            appid=1902490,
            install_dir_name="Desk Job",
            journal_dir=tmp_path / "journal",
            operation_id=7,
        )
    assert victim.read_text(encoding="utf-8") == "do not overwrite"


def test_reconcile_without_journal_is_clean(tmp_path: Path) -> None:
    library = _library(tmp_path)
    assert (
        reconcile_adoption(
            library=library, appid=42, journal_dir=tmp_path / "journal"
        )
        == "clean"
    )


def test_reconcile_foreign_operation_journal_is_stale(tmp_path: Path) -> None:
    library = _library(tmp_path)
    target = _target_with_manifest(tmp_path)
    journal_dir = tmp_path / "journal"
    adopt_manifest(
        source=locate_manifest(install_dir=target, appid=1902490),
        library=library,
        appid=1902490,
        install_dir_name="Desk Job",
        journal_dir=journal_dir,
        operation_id=7,
    )
    assert (
        reconcile_adoption(
            library=library, appid=1902490, journal_dir=journal_dir, operation_id=8
        )
        == "stale"
    )
    assert not (journal_dir / "adoption-1902490.json").exists()


class _FakeProcess:
    def __init__(self, stdout: str | bytes, stderr: str = "") -> None:
        self.pid = 4242
        self.returncode = 0
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):
        return self._stdout, self._stderr


def test_steamcmd_log_redacts_account_and_paths(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from steam_agent.execution.content_plane import SteamcmdAdapter

    home = tmp_path / "home"
    install_dir = tmp_path / "install"

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(
            f"Logging in user 'ownername' (76561199000000001)"
            f" buddy 76561200000000000 ... dir {install_dir} home {home}"
        ),
    )
    adapter = SteamcmdAdapter(
        steamcmd_script=tmp_path / "steamcmd.sh",
        private_home=home,
        log_dir=tmp_path / "logs",
    )
    result = adapter.install(
        account="ownername", appid=480, install_dir=install_dir, operation_id=1
    )
    log = result.log_path.read_text(encoding="utf-8")
    assert "ownername" not in log
    assert str(install_dir) not in log and str(home) not in log
    assert "76561199000000001" not in log
    assert "76561200000000000" not in log  # beyond the 7656119 prefix
    assert "<account>" in log and "<steamid>" in log


def test_stderr_never_merges_into_a_recognized_stdout_line(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from steam_agent.execution.content_plane import SteamcmdAdapter

    # stdout ends without a newline on a recognized marker; stderr starts
    # with an unrecognized private line.  Merged, the marker would smuggle
    # the private line past the allowlist filter.
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(
            "Success! App '480' fully installed.",
            "/home/someuser/.secret-place diagnostic",
        ),
    )
    adapter = SteamcmdAdapter(
        steamcmd_script=tmp_path / "steamcmd.sh",
        private_home=tmp_path / "home",
        log_dir=tmp_path / "logs",
    )
    result = adapter.install(
        account="o", appid=480, install_dir=tmp_path / "i", operation_id=1
    )
    assert result.outcome == "installed"
    log = result.log_path.read_text(encoding="utf-8")
    assert ".secret-place" not in log
    assert "1 unrecognized line(s) omitted" in log


def test_locale_invalid_output_still_classifies(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from steam_agent.execution.content_plane import SteamcmdAdapter

    # Invalid UTF-8 in steamcmd output must be decoded leniently, never
    # raised out of classification with the client stopped.
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(
            b"Success! App '480' fully installed. \xff\xfe"
        ),
    )
    adapter = SteamcmdAdapter(
        steamcmd_script=tmp_path / "steamcmd.sh",
        private_home=tmp_path / "home",
        log_dir=tmp_path / "logs",
    )
    result = adapter.install(
        account="o", appid=480, install_dir=tmp_path / "i", operation_id=1
    )
    assert result.outcome == "installed"


def test_marker_substring_account_still_classifies(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from steam_agent.execution.content_plane import SteamcmdAdapter

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess("Success! App '480' fully installed."),
    )
    adapter = SteamcmdAdapter(
        steamcmd_script=tmp_path / "steamcmd.sh",
        private_home=tmp_path / "home",
        log_dir=tmp_path / "logs",
    )
    # "all" is a substring of "fully installed"; classification must see
    # the raw output, not the redacted copy.
    result = adapter.install(
        account="all", appid=480, install_dir=tmp_path / "i", operation_id=1
    )
    assert result.outcome == "installed"
    assert "all" not in result.log_path.read_text(encoding="utf-8").replace(
        "<install-dir>", ""
    )


def test_steamcmd_timeout_kills_entire_process_tree(tmp_path: Path) -> None:
    import os

    from steam_agent.execution.content_plane import SteamcmdAdapter

    pid_file = tmp_path / "child.pid"
    script = tmp_path / "steamcmd.sh"
    script.write_text(
        f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n", encoding="utf-8"
    )
    script.chmod(0o755)
    adapter = SteamcmdAdapter(
        steamcmd_script=script,
        private_home=tmp_path / "home",
        log_dir=tmp_path / "logs",
        timeout_seconds=1,
    )
    result = adapter.install(
        account="o", appid=480, install_dir=tmp_path / "i", operation_id=1
    )
    assert result.outcome == "failed"
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # the wrapped child must not survive the timeout
