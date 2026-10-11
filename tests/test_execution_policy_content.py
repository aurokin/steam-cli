"""Policy fail-closed behavior and content-plane parsing/adoption."""

from __future__ import annotations

from pathlib import Path

import pytest

from steam_agent.execution.content_plane import (
    adopt_manifest,
    locate_manifest,
    manifest_appid,
    manifest_state_flags,
    reconcile_adoption,
)
from steam_agent.execution.policy import (
    PolicyError,
    load_policy,
    write_policy_template,
)

_MANIFEST = '''"AppState"
{
\t"appid"\t\t"1902490"
\t"installdir"\t\t"steamcmd_dir"
\t"StateFlags"\t\t"4"
}
'''


@pytest.mark.parametrize("field", ["appid", "StateFlags"])
@pytest.mark.parametrize("value", ["9" * 5000, "0" * 5000 + "4", "4294967296", "٤"])
def test_malformed_manifest_numbers_are_unknown(
    tmp_path: Path, field: str, value: str,
) -> None:
    manifest = tmp_path / "manifest.acf"
    manifest.write_text(f'"{field}" "{value}"', encoding="utf-8")

    reader = manifest_appid if field == "appid" else manifest_state_flags
    assert reader(manifest) is None


@pytest.mark.parametrize("value", [0, 0xFFFFFFFF])
def test_manifest_state_flags_accept_unsigned_32_bit_boundaries(
    tmp_path: Path, value: int,
) -> None:
    manifest = tmp_path / "manifest.acf"
    manifest.write_text(f'"StateFlags" "{value}"', encoding="utf-8")

    assert manifest_state_flags(manifest) == value


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


def test_policy_allow_with_floor_parses(tmp_path: Path) -> None:
    policy = load_policy(
        _write_policy(
            tmp_path,
            '[grants]\ninstall = "allow"\n[limits]\nmin_free_gb = 25\n',
        )
    )
    assert policy.grant_for("install") == "allow"
    assert policy.min_free_gb == 25


def test_policy_allow_without_floor_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write_policy(tmp_path, '[grants]\ninstall = "allow"\n'))


@pytest.mark.parametrize(
    "value", ["true", "-1", '"25"', "25.5"], ids=["bool", "negative", "str", "float"]
)
def test_policy_bad_limit_type_fails_closed(tmp_path: Path, value: str) -> None:
    with pytest.raises(PolicyError):
        load_policy(
            _write_policy(
                tmp_path,
                f'[grants]\ninstall = "allow"\n[limits]\nmin_free_gb = {value}\n',
            )
        )


def test_policy_unknown_limit_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(
            _write_policy(
                tmp_path,
                '[grants]\ninstall = "confirm"\n[limits]\nmax_daily_gb = 10\n',
            )
        )


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


def test_reconcile_client_rewritten_fresh_manifest_counts_completed(
    tmp_path: Path,
) -> None:
    library = _library(tmp_path)
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
    # The client started after the crash and rewrote the adopted manifest
    # in its own formatting; a fresh install (no backup) must not have the
    # landed swap unlinked over a byte mismatch.
    adopted.write_text(
        '"AppState"\n{\n\t"appid"\t\t"1902490"\n'
        '\t"installdir"\t\t"Desk Job"\n\t"StateFlags"\t\t"4"\n}\n',
        encoding="utf-8",
    )
    assert (
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
        == "completed"
    )
    assert adopted.is_file()  # not unlinked


def test_reconcile_foreign_destination_journal_rejected(tmp_path: Path) -> None:
    import json as json_module

    from steam_agent.execution.content_plane import AdoptionError

    library = _library(tmp_path)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    victim = tmp_path / "victim.acf"
    victim.write_text("do not touch", encoding="utf-8")
    # Valid JSON, but the destination points outside this configuration.
    (journal_dir / "adoption-1902490.json").write_text(
        json_module.dumps(
            {
                "appid": 1902490,
                "destination": str(victim),
                "checksum": "0" * 64,
                "backup": None,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AdoptionError):
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
    assert victim.read_text(encoding="utf-8") == "do not touch"


def test_reconcile_missing_backup_defers_instead_of_unlinking(
    tmp_path: Path,
) -> None:
    from steam_agent.execution.content_plane import AdoptionError

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
    adopted.write_text('"AppState" { torn', encoding="utf-8")  # crash mid-swap
    # The promised backup vanished: recovery must defer, not take the
    # fresh-install unlink path over an update's manifest.
    (journal_dir / "appmanifest_1902490.acf.backup").unlink()

    with pytest.raises(AdoptionError):
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
    assert adopted.exists()  # nothing unlinked on incomplete evidence


def test_reconcile_mount_changed_journal_defers(tmp_path: Path) -> None:
    import json as json_module

    from steam_agent.execution.content_plane import AdoptionError

    library = _library(tmp_path)
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
    # Simulate a different volume exposed at the library path: the journaled
    # identity no longer matches the live directory.
    journal = journal_dir / "adoption-1902490.json"
    record = json_module.loads(journal.read_text(encoding="utf-8"))
    record["library_ino"] += 1
    journal.write_text(json_module.dumps(record), encoding="utf-8")

    with pytest.raises(AdoptionError):
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
    assert adopted.is_file()  # nothing destroyed on the mismatched volume


def test_reconcile_corrupt_journal_raises_adoption_error(tmp_path: Path) -> None:
    from steam_agent.execution.content_plane import AdoptionError

    library = _library(tmp_path)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "adoption-1902490.json").write_text(
        '{"appid": 1902', encoding="utf-8"  # truncated
    )
    with pytest.raises(AdoptionError):
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)


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
    """Stands in for Popen: writes canned output into the spool handles."""

    def __init__(self, stdout: str | bytes, stderr: str | bytes = "") -> None:
        self.pid = 4242
        self.returncode = 0
        self._stdout = stdout
        self._stderr = stderr

    def bind(self, kwargs) -> "_FakeProcess":
        for data, key in ((self._stdout, "stdout"), (self._stderr, "stderr")):
            handle = kwargs.get(key)
            if handle is not None and hasattr(handle, "write"):
                handle.write(
                    data if isinstance(data, bytes) else data.encode("utf-8")
                )
        return self

    def wait(self, timeout=None) -> int:
        return self.returncode


class _PollingProcess(_FakeProcess):
    def __init__(self, output: str = "Success! App fully installed", *, wait_error=None) -> None:
        super().__init__(output)
        self.running = True
        self.returncode = None
        self.wait_error = wait_error
        self.cleanup_wait_error = None
        self.kill_error = None
        self.kill_calls = 0
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout=None) -> int:
        import subprocess

        self.wait_timeouts.append(timeout)
        if self.running:
            if self.wait_error is not None:
                raise self.wait_error
            raise subprocess.TimeoutExpired("synthetic-owned-process", timeout)
        if self.cleanup_wait_error is not None:
            raise self.cleanup_wait_error
        self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        self.running = False


def _polling_adapter(tmp_path: Path, monkeypatch, process: _PollingProcess):
    from steam_agent.execution.content_plane import SteamcmdAdapter

    groups: list[int] = []
    monkeypatch.setattr(
        "steam_agent.execution.content_plane.subprocess.Popen",
        lambda *args, **kwargs: process.bind(kwargs),
    )

    def kill_group(pid: int, sig: int) -> None:
        import signal

        assert sig == signal.SIGKILL
        groups.append(pid)
        process.running = False

    monkeypatch.setattr("steam_agent.execution.content_plane.os.killpg", kill_group)
    adapter = SteamcmdAdapter(
        steamcmd_script=tmp_path / "synthetic-script",
        private_home=tmp_path / "home",
        log_dir=tmp_path / "logs",
        timeout_seconds=30,
        abort_poll_seconds=0.01,
    )
    return adapter, groups


@pytest.mark.parametrize("error_kind", ["os", "database", "probe"])
@pytest.mark.parametrize("output", ["Success! App fully installed", "Invalid Password"])
def test_failed_abort_poll_terminates_owned_group_reaps_and_reports_failed(
    tmp_path: Path, monkeypatch, error_kind: str, output: str,
) -> None:
    import sqlite3

    process = _PollingProcess(output)
    adapter, groups = _polling_adapter(tmp_path, monkeypatch, process)
    error_type = {"os": OSError, "database": sqlite3.OperationalError, "probe": RuntimeError}[error_kind]

    def broken_poll() -> bool:
        raise error_type("PRIVATE-POLL-ERROR-CANARY")

    result = adapter.install(
        account="synthetic", appid=480, install_dir=tmp_path / "install",
        operation_id=1, abort_when=broken_poll,
    )

    assert result.outcome == "failed"
    assert groups == [process.pid]
    assert process.kill_calls == 0
    assert not process.running
    assert len(process.wait_timeouts) == 2
    assert process.wait_timeouts[-1] == 5.0
    assert "PRIVATE-POLL-ERROR-CANARY" not in result.log_path.read_text()
    assert not list((tmp_path / "logs").glob("*.tmp"))


@pytest.mark.parametrize("error", [OSError("PRIVATE-WAIT-ERROR"), RuntimeError("PRIVATE-WAIT-ERROR")])
def test_process_wait_failure_cleans_owned_process_before_sanitized_error(
    tmp_path: Path, monkeypatch, error: Exception,
) -> None:
    from steam_agent.execution.content_plane import ContentProcessError

    process = _PollingProcess(wait_error=error)
    adapter, groups = _polling_adapter(tmp_path, monkeypatch, process)

    with pytest.raises(ContentProcessError) as caught:
        adapter.install(
            account="synthetic", appid=480, install_dir=tmp_path / "install",
            operation_id=1,
        )

    assert groups == [process.pid]
    assert not process.running
    assert len(process.wait_timeouts) == 2
    assert process.wait_timeouts[-1] == 5.0
    assert "PRIVATE" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert not list((tmp_path / "logs").glob("*.tmp"))


def test_process_cancellation_cleans_owned_group_before_propagating(
    tmp_path: Path, monkeypatch,
) -> None:
    process = _PollingProcess(wait_error=KeyboardInterrupt())
    adapter, groups = _polling_adapter(tmp_path, monkeypatch, process)

    with pytest.raises(KeyboardInterrupt):
        adapter.install(
            account="synthetic", appid=480, install_dir=tmp_path / "install",
            operation_id=1,
        )

    assert groups == [process.pid]
    assert not process.running
    assert process.wait_timeouts[-1] == 5.0


def test_owned_group_already_gone_still_reaps_leader(tmp_path: Path, monkeypatch) -> None:
    process = _PollingProcess()
    adapter, _ = _polling_adapter(tmp_path, monkeypatch, process)

    def missing_group(pid: int, sig: int) -> None:
        raise ProcessLookupError("PRIVATE-GONE-GROUP")

    monkeypatch.setattr("steam_agent.execution.content_plane.os.killpg", missing_group)

    result = adapter.install(
        account="synthetic", appid=480, install_dir=tmp_path / "install",
        operation_id=1, abort_when=lambda: True,
    )

    assert result.outcome == "failed"
    assert process.kill_calls == 1
    assert not process.running
    assert process.wait_timeouts[-1] == 5.0
    assert "PRIVATE-GONE-GROUP" not in result.log_path.read_text()


@pytest.mark.parametrize("failure", ["group", "group_and_leader", "cleanup_wait"])
def test_owned_process_cleanup_failure_is_bounded_and_fail_closed(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    from steam_agent.execution.content_plane import ContentProcessError

    process = _PollingProcess()
    adapter, groups = _polling_adapter(tmp_path, monkeypatch, process)
    if failure in {"group", "group_and_leader"}:
        def failed_group(pid: int, sig: int) -> None:
            groups.append(pid)
            raise PermissionError("PRIVATE-GROUP-ERROR")

        monkeypatch.setattr("steam_agent.execution.content_plane.os.killpg", failed_group)
        if failure == "group_and_leader":
            process.kill_error = PermissionError("PRIVATE-LEADER-ERROR")
    else:
        process.cleanup_wait_error = OSError("PRIVATE-REAP-ERROR")

    with pytest.raises(ContentProcessError) as caught:
        adapter.install(
            account="synthetic", appid=480, install_dir=tmp_path / "install",
            operation_id=1, abort_when=lambda: True,
        )

    assert groups == [process.pid]
    assert process.kill_calls == int(failure in {"group", "group_and_leader"})
    assert len(process.wait_timeouts) == 2
    assert process.wait_timeouts[-1] == 5.0
    assert "PRIVATE" not in str(caught.value)
    assert process.running is (failure == "group_and_leader")


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
            f"\nLogin OK for user 'OWNERNAME'"  # case-normalized echo
            f"\nLogging in user 'ownername' [U:1:99999999] to Steam Public...OK"
        ).bind(kwargs),
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
    assert "ownername" not in log.lower().replace("<account>", "")
    assert str(install_dir) not in log and str(home) not in log
    assert "76561199000000001" not in log
    assert "76561200000000000" not in log  # beyond the 7656119 prefix
    assert "U:1:99999999" not in log  # SteamID3, steamcmd's real login format
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
        ).bind(kwargs),
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


def test_unconfigured_paths_scrubbed_from_recognized_lines(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from steam_agent.execution.content_plane import SteamcmdAdapter

    # An allowlisted marker ("ERROR") with an arbitrary private path: the
    # path is not one of the configured replacements and must still be
    # scrubbed.
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(
            "ERROR opening /home/someuser/private-file for read"
        ).bind(kwargs),
    )
    adapter = SteamcmdAdapter(
        steamcmd_script=tmp_path / "steamcmd.sh",
        private_home=tmp_path / "home",
        log_dir=tmp_path / "logs",
    )
    result = adapter.install(
        account="o", appid=480, install_dir=tmp_path / "i", operation_id=1
    )
    log = result.log_path.read_text(encoding="utf-8")
    assert "/home/someuser" not in log
    assert "<path>" in log


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
        ).bind(kwargs),
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
        lambda *args, **kwargs: _FakeProcess("Success! App '480' fully installed.").bind(kwargs),
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


def test_client_relaunch_kills_steamcmd_mid_run(tmp_path: Path) -> None:
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
        timeout_seconds=30,
        abort_poll_seconds=0.05,
    )
    # abort_when reporting a live client must kill the whole tree promptly,
    # long before the 30s timeout.  (Gated on the pid file so the script has
    # started; also exercises repeated polls.)
    result = adapter.install(
        account="o",
        appid=480,
        install_dir=tmp_path / "i",
        operation_id=1,
        abort_when=pid_file.exists,
    )
    assert result.outcome == "failed"
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


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


def test_default_template_denies_both_operation_classes(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    write_policy_template(path)

    policy = load_policy(path)

    assert policy.grant_for("install") == "deny"
    assert policy.grant_for("verify") == "deny"


def test_an_unknown_operation_class_is_denied_even_when_the_file_omits_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "policy.toml"
    path.write_text('[grants]\nverify = "confirm"\n', encoding="utf-8")

    policy = load_policy(path)

    assert policy.grant_for("verify") == "confirm"
    assert policy.grant_for("install") == "deny"
    assert policy.grant_for("uninstall") == "deny"


def test_verify_allow_still_requires_a_disk_floor(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text('[grants]\nverify = "allow"\n', encoding="utf-8")

    with pytest.raises(PolicyError):
        load_policy(path)


def test_launch_allowlist_rejects_non_appid_entries(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    # true is an int subclass in Python; it must not enroll AppID 1.
    path.write_text(
        '[grants]\nlaunch = "confirm"\n[launch]\nallowed_appids = [true]\n',
        encoding="utf-8",
    )

    with pytest.raises(PolicyError):
        load_policy(path)


def test_launch_allowlist_is_independent_of_the_other_grants(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        '[grants]\ninstall = "confirm"\nlaunch = "confirm"\n'
        "[launch]\nallowed_appids = [480]\n",
        encoding="utf-8",
    )

    policy = load_policy(path)

    assert policy.launch_permitted(480) is True
    assert policy.launch_permitted(220) is False
    assert policy.grant_for("verify") == "deny"
