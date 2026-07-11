from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import steam_agent.application as application
import steam_agent.cli as cli
import steam_agent.contracts as contracts
from steam_agent.cli import main
from steam_agent.storage import Machine, Storage


FIXTURES = Path(__file__).parent / "fixtures" / "steam"


def invoke(argv: list[str], capsys: object) -> tuple[int, dict[str, object], str]:
    code = main(argv)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def test_installed_sync_and_query_end_to_end(tmp_path: Path, capsys: object) -> None:
    common = ["--data-dir", str(tmp_path), "--format", "json"]
    code, synced, stderr = invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            "fixture-machine",
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
        ],
        capsys,
    )
    assert code == 0
    assert stderr == ""
    assert synced["completeness"]["status"] == "complete"  # type: ignore[index]

    code, queried, stderr = invoke(
        common
        + [
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            "fixture-machine",
        ],
        capsys,
    )
    assert code == 0
    assert stderr == ""
    items = queried["data"]["items"]  # type: ignore[index]
    assert [item["appid"] for item in items] == [10, 20]
    assert "install_dir" not in items[0]


def test_missing_root_is_typed_error(tmp_path: Path, capsys: object) -> None:
    code, value, stderr = invoke(
        [
            "--data-dir",
            str(tmp_path),
            "--format",
            "json",
            "sync",
            "installed",
            "--steam-root",
            str(tmp_path / "missing"),
        ],
        capsys,
    )
    assert code == 3
    assert stderr == ""
    assert value["error"]["code"] == "STEAM_ROOT_INACCESSIBLE"  # type: ignore[index]


def test_leaf_format_option_matches_documented_command_shape(
    tmp_path: Path, capsys: object
) -> None:
    code, value, stderr = invoke(
        ["--data-dir", str(tmp_path), "status", "--format", "json"], capsys
    )
    assert code == 0
    assert stderr == ""
    assert value["command"] == "status"


def test_invalid_arguments_return_typed_json(capsys: object) -> None:
    code, value, stderr = invoke(["not-a-command"], capsys)
    assert code == 2
    assert stderr == ""
    assert value["error"]["code"] == "INVALID_ARGUMENT"  # type: ignore[index]


def test_secret_argument_is_rejected_without_echo(capsys: object) -> None:
    secret = "do-not-echo-this-value"
    code, value, stderr = invoke(["--api-key", secret, "status"], capsys)
    captured = json.dumps(value) + stderr
    assert code == 2
    assert value["error"]["code"] == "SECRET_ON_ARGV"  # type: ignore[index]
    assert secret not in captured


@pytest.mark.parametrize(
    "argument",
    [
        "--api-key=do-not-echo-api-key",
        "--token=do-not-echo-token",
        "--password=do-not-echo-password",
        "--cookie=do-not-echo-cookie",
        "--client-secret=do-not-echo-client-secret",
    ],
)
def test_secret_equals_argument_is_rejected_without_echo(
    argument: str, capsys: object
) -> None:
    code = main([argument, "status"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 2
    assert captured.err == ""
    assert argument.split("=", 1)[1] not in captured.out
    assert json.loads(captured.out)["error"]["code"] == "SECRET_ON_ARGV"


def test_secret_argument_honors_explicit_table_format_without_echo(
    capsys: object,
) -> None:
    secret = "never-print-this-secret"
    code = main(["--format", "table", "--api-key", secret, "status"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 2
    assert captured.out == ""
    assert captured.err == "SECRET_ON_ARGV: Secrets are not accepted as command-line arguments.\n"
    assert secret not in captured.err


def test_secret_value_named_table_does_not_select_table_output(capsys: object) -> None:
    code = main(["--api-key", "table", "status"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 2
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "SECRET_ON_ARGV"


def test_default_query_json_hides_all_local_paths(
    tmp_path: Path, capsys: object
) -> None:
    common = ["--data-dir", str(tmp_path), "--format", "json"]
    root = (FIXTURES / "valid" / "root").resolve()
    code, _, _ = invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            "fixture-machine",
            "--steam-root",
            str(root),
        ],
        capsys,
    )
    assert code == 0

    code = main(
        common
        + [
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            "fixture-machine",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    value = json.loads(captured.out)

    assert code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert str(root) not in captured.out
    assert all(
        {"library_root", "install_dir", "manifest_path"}.isdisjoint(item)
        for item in value["data"]["items"]
    )


def test_include_paths_is_an_explicit_opt_in(tmp_path: Path, capsys: object) -> None:
    common = ["--data-dir", str(tmp_path), "--format", "json"]
    root = (FIXTURES / "valid" / "root").resolve()
    invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            "fixture-machine",
            "--steam-root",
            str(root),
        ],
        capsys,
    )

    code, value, stderr = invoke(
        common
        + [
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            "fixture-machine",
            "--include-paths",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert str(root) in json.dumps(value)
    assert {"library_root", "install_dir", "manifest_path"} <= value["data"][
        "items"
    ][0].keys()


def test_unexpected_exception_redacts_message_and_emits_one_json_document(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "private-path-or-token"

    def fail_dispatch(*_: object) -> int:
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "_dispatch", fail_dispatch)
    code = main(["--data-dir", str(tmp_path), "status"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    value = json.loads(captured.out)

    assert code == 1
    assert value["error"]["code"] == "INTERNAL_ERROR"
    assert captured.out.count("\n") == 1
    assert secret not in captured.out + captured.err
    assert captured.err == "steam-agent: RuntimeError\n"


def test_status_json_is_byte_deterministic_with_fixed_clock(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(contracts, "utc_now", lambda: fixed)
    argv = ["--data-dir", str(tmp_path), "status", "--format", "json"]

    assert main(argv) == 0
    first = capsys.readouterr()  # type: ignore[attr-defined]
    assert main(argv) == 0
    second = capsys.readouterr()  # type: ignore[attr-defined]

    assert first.err == second.err == ""
    assert first.out == second.out
    assert first.out.count("\n") == 1


def test_query_before_any_sync_is_not_a_confirmed_empty_library(
    tmp_path: Path, capsys: object
) -> None:
    code, value, stderr = invoke(
        [
            "--data-dir",
            str(tmp_path),
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            "never-scanned",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert value["data"]["items"] == []
    assert value["completeness"]["status"] == "unavailable"
    assert "installed.read" in value["completeness"]["missing_capabilities"]


def test_query_after_partial_sync_marks_last_good_projection_stale(
    tmp_path: Path, capsys: object
) -> None:
    common = ["--data-dir", str(tmp_path), "--format", "json"]
    machine = "partial-machine"
    complete_code, _, _ = invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
        ],
        capsys,
    )
    partial_code, partial, _ = invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "problems" / "root"),
        ],
        capsys,
    )
    code, queried, stderr = invoke(
        common
        + ["games", "query", "--scope", "installed", "--machine", machine],
        capsys,
    )

    assert complete_code == partial_code == code == 0
    assert partial["completeness"]["status"] == "partial"
    assert stderr == ""
    assert [item["appid"] for item in queried["data"]["items"]] == [10, 20]
    assert queried["completeness"]["status"] == "partial"
    assert "installed.read" in queried["completeness"]["stale_capabilities"]


def test_query_after_failed_sync_marks_last_good_projection_stale(
    tmp_path: Path, capsys: object
) -> None:
    common = ["--data-dir", str(tmp_path), "--format", "json"]
    machine = "failed-machine"
    code, _, _ = invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
        ],
        capsys,
    )
    assert code == 0

    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        failed = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id=machine,
            started_at="2026-07-10T13:00:00Z",
        )
        storage.finish_installed_sync(
            failed.id,
            status="failed",
            completed_at="2026-07-10T13:01:00Z",
            error_code="SCAN_FAILED",
            error_detail="OSError",
        )

    code, queried, stderr = invoke(
        common
        + ["games", "query", "--scope", "installed", "--machine", machine],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert [item["appid"] for item in queried["data"]["items"]] == [10, 20]
    assert queried["completeness"]["status"] == "partial"
    assert "installed.read" in queried["completeness"]["stale_capabilities"]


def test_partial_sync_warning_json_does_not_expose_root_or_home_paths(
    tmp_path: Path, capsys: object
) -> None:
    root = (FIXTURES / "problems" / "root").resolve()
    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "sync",
            "installed",
            "--steam-root",
            str(root),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    value = json.loads(captured.out)

    assert code == 0
    assert value["completeness"]["status"] == "partial"
    assert value["completeness"]["warnings"]
    assert str(root) not in captured.out
    assert str(Path.home()) not in captured.out
    assert all("/" not in warning.get("source", "") for warning in value["completeness"]["warnings"])
    assert all(
        warning.get("source") != "steamapps"
        for warning in value["completeness"]["warnings"]
    )
    safe_sources = [
        warning["source"]
        for warning in value["completeness"]["warnings"]
        if "source" in warning
    ]
    assert all(
        source == "libraryfolders.vdf"
        or (
            source.startswith("appmanifest_")
            and source.endswith(".acf")
            and source.removeprefix("appmanifest_").removesuffix(".acf").isdigit()
        )
        for source in safe_sources
    )


def test_invalid_configured_root_is_unavailable_for_doctor_and_capabilities(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid = tmp_path / "not-a-steam-root"
    invalid.mkdir()
    monkeypatch.setenv("STEAM_AGENT_STEAM_ROOT", str(invalid))

    doctor_code, doctor, doctor_stderr = invoke(
        ["--data-dir", str(tmp_path / "data"), "doctor", "--format", "json"],
        capsys,
    )
    capabilities_code, capabilities, capabilities_stderr = invoke(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "capabilities",
            "--format",
            "json",
        ],
        capsys,
    )

    assert doctor_code == capabilities_code == 0
    assert doctor_stderr == capabilities_stderr == ""
    assert doctor["completeness"]["status"] == "unavailable"
    assert doctor["completeness"]["missing_capabilities"] == ["installed.read"]
    assert doctor["data"]["installed_read"] == "unavailable"
    assert doctor["completeness"]["warnings"][0]["code"] == "STEAM_ROOT_INACCESSIBLE"
    assert capabilities["data"]["capabilities"][0]["state"] == "unavailable"
    assert capabilities["completeness"] == doctor["completeness"]


def test_corrupt_sqlite_returns_typed_database_error(
    tmp_path: Path, capsys: object
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "steam-agent.sqlite3"
    database.write_bytes(b"this is not a sqlite database")

    code, value, stderr = invoke(
        ["--data-dir", str(data_dir), "status", "--format", "json"], capsys
    )

    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "DATABASE_ERROR"
    assert str(database) not in json.dumps(value)


def test_data_dir_expands_user_home(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    code, value, stderr = invoke(
        [
            "--data-dir",
            "~/agent-data",
            "games",
            "query",
            "--scope",
            "installed",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert value["completeness"]["status"] == "unavailable"
    assert (home / "agent-data" / "steam-agent.sqlite3").is_file()


def test_arbitrary_table_argument_does_not_switch_parse_error_to_table(
    capsys: object,
) -> None:
    code = main(["table"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 2
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "format_args", [["--format", "table"], ["--format=table"]]
)
def test_actual_table_format_controls_parse_error_output(
    format_args: list[str], capsys: object
) -> None:
    code = main(format_args + ["not-a-command"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 2
    assert captured.out == ""
    assert captured.err == "INVALID_ARGUMENT: The command arguments are invalid.\n"


def test_invalid_configured_root_sync_is_inaccessible_not_not_found(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid = tmp_path / "not-a-steam-root"
    invalid.mkdir()
    monkeypatch.setenv("STEAM_AGENT_STEAM_ROOT", str(invalid))

    code, value, stderr = invoke(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "sync",
            "installed",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 3
    assert stderr == ""
    assert value["error"]["code"] == "STEAM_ROOT_INACCESSIBLE"


def test_unenumerable_selected_primary_root_is_typed_inaccessible(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "steam"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    original_scandir = application.os.scandir

    def deny_primary(path: object) -> object:
        if Path(path) == steamapps:
            raise PermissionError("denied")
        return original_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(application.os, "scandir", deny_primary)
    code, value, stderr = invoke(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "sync",
            "installed",
            "--steam-root",
            str(root),
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 3
    assert stderr == ""
    assert value["error"]["code"] == "STEAM_ROOT_INACCESSIBLE"


def test_table_query_surfaces_unavailable_completeness(
    tmp_path: Path, capsys: object
) -> None:
    code = main(
        [
            "--data-dir",
            str(tmp_path),
            "games",
            "query",
            "--scope",
            "installed",
            "--format",
            "table",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 0
    assert captured.err == ""
    assert "COMPLETENESS\tunavailable\n" in captured.out
    assert "MISSING_CAPABILITY\tinstalled.read\n" in captured.out
    assert "WARNING\tNOT_SYNCED\t" in captured.out
    assert captured.out.endswith("APPID\tNAME\tSTATE\tSIZE\n")


def test_table_query_surfaces_stale_last_good_snapshot(
    tmp_path: Path, capsys: object
) -> None:
    common = ["--data-dir", str(tmp_path)]
    machine = "table-machine"
    assert main(
        common
        + [
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
            "--format",
            "json",
        ]
    ) == 0
    capsys.readouterr()  # type: ignore[attr-defined]
    assert main(
        common
        + [
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "problems" / "root"),
            "--format",
            "json",
        ]
    ) == 0
    capsys.readouterr()  # type: ignore[attr-defined]

    code = main(
        common
        + [
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            machine,
            "--format",
            "table",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 0
    assert captured.err == ""
    assert "COMPLETENESS\tpartial\n" in captured.out
    assert "STALE_CAPABILITY\tinstalled.read\n" in captured.out
    assert "WARNING\tSTALE_LAST_GOOD\t" in captured.out
    assert "10\tAlpha Game\tinstalled\t987654321\n" in captured.out


def test_running_sync_without_last_good_is_unavailable_not_partial(
    tmp_path: Path, capsys: object
) -> None:
    machine = "running-only"
    data_dir = tmp_path / "data"
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine(machine, machine, "linux", "x86_64"),
            observed_at="2026-07-10T14:00:00Z",
        )
        storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id=machine,
            started_at="2026-07-10T14:00:00Z",
        )

    code, value, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            machine,
            "--format",
            "json",
        ],
        capsys,
    )

    warning_codes = {
        warning["code"] for warning in value["completeness"]["warnings"]
    }
    assert code == 0
    assert stderr == ""
    assert value["data"]["items"] == []
    assert value["completeness"]["status"] == "unavailable"
    assert value["completeness"]["missing_capabilities"] == ["installed.read"]
    assert warning_codes == {"SYNC_IN_PROGRESS"}
    assert {"PARTIAL_SCAN", "STALE_LAST_GOOD"}.isdisjoint(warning_codes)
    assert value["data"]["snapshot"] == {
        "last_attempt_status": "running",
        "last_successful_sync_at": None,
    }


def test_running_sync_with_last_good_keeps_complete_snapshot(
    tmp_path: Path, capsys: object
) -> None:
    machine = "refreshing"
    data_dir = tmp_path / "data"
    common = ["--data-dir", str(data_dir), "--format", "json"]
    code, _, _ = invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
        ],
        capsys,
    )
    assert code == 0
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        successful = storage.latest_sync(
            capability="installed", machine_id=machine, status="complete"
        )
        assert successful is not None
        storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id=machine,
            started_at="2026-07-10T15:00:00Z",
        )

    code, value, stderr = invoke(
        common
        + ["games", "query", "--scope", "installed", "--machine", machine],
        capsys,
    )

    warning_codes = {
        warning["code"] for warning in value["completeness"]["warnings"]
    }
    assert code == 0
    assert stderr == ""
    assert [item["appid"] for item in value["data"]["items"]] == [10, 20]
    assert value["completeness"]["status"] == "complete"
    assert value["completeness"]["stale_capabilities"] == []
    assert warning_codes == {"SYNC_IN_PROGRESS"}
    assert {"PARTIAL_SCAN", "STALE_LAST_GOOD"}.isdisjoint(warning_codes)
    assert value["data"]["snapshot"] == {
        "last_attempt_status": "running",
        "last_successful_sync_at": successful.completed_at,
    }


def test_older_abandoned_running_row_does_not_override_newer_complete(
    tmp_path: Path, capsys: object
) -> None:
    machine = "abandoned-older"
    data_dir = tmp_path / "data"
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine(machine, machine, "linux", "x86_64"),
            observed_at="2000-01-01T00:00:00Z",
        )
        storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id=machine,
            started_at="2000-01-01T00:00:00Z",
        )

    code, _, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
            "--format",
            "json",
        ],
        capsys,
    )
    assert code == 0

    code, value, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            machine,
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert value["completeness"]["status"] == "complete"
    assert value["completeness"]["warnings"] == []
    assert value["data"]["snapshot"]["last_attempt_status"] == "complete"


def test_capabilities_without_root_has_typed_unavailable_completeness(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STEAM_AGENT_STEAM_ROOT", raising=False)
    monkeypatch.setattr(cli, "discover_steam_root", lambda: None)

    code, value, stderr = invoke(
        [
            "--data-dir",
            str(tmp_path),
            "capabilities",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert value["data"]["capabilities"][0]["state"] == "unavailable"
    assert value["completeness"]["status"] == "unavailable"
    assert value["completeness"]["missing_capabilities"] == ["installed.read"]
    assert value["completeness"]["warnings"] == [
        {
            "code": "STEAM_NOT_FOUND",
            "message": "No default Steam installation was found; pass --steam-root when syncing.",
        }
    ]


def test_games_table_escapes_control_characters_and_preserves_zero_size(
    capsys: object,
) -> None:
    envelope = {
        "completeness": {
            "status": "partial\nINJECTED",
            "missing_capabilities": ["installed.read\tEXTRA"],
            "stale_capabilities": [],
            "warnings": [
                {
                    "code": "BAD\tCODE",
                    "message": "first line\nFAKE\tROW\r\x00",
                    "source": "appmanifest_1.acf",
                }
            ],
        },
        "data": {
            "items": [
                {
                    "appid": 1,
                    "name": "Game\tName\nFAKE ROW\x07\u202e\u2028LINE\u2029PARAGRAPH",
                    "state": "installed\rstate",
                    "size_bytes": 0,
                },
                {
                    "appid": 2,
                    "name": None,
                    "state": "installed",
                    "size_bytes": None,
                },
            ]
        },
    }

    cli._print_table("games.query", envelope)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    lines = captured.out.splitlines()

    assert captured.err == ""
    assert len(lines) == 6
    assert lines[0] == "COMPLETENESS\tpartial\\nINJECTED"
    assert lines[1] == "MISSING_CAPABILITY\tinstalled.read\\tEXTRA"
    assert lines[2] == (
        "WARNING\tBAD\\tCODE\tfirst line\\nFAKE\\tROW\\r\\u0000\tappmanifest_1.acf"
    )
    assert lines[4] == (
        "1\tGame\\tName\\nFAKE ROW\\u0007\\u202e\\u2028LINE"
        "\\u2029PARAGRAPH\tinstalled\\rstate\t0"
    )
    assert lines[5] == "2\t\tinstalled\t"


def test_generic_table_escapes_keys_and_values(capsys: object) -> None:
    cli._print_table(
        "status",
        {"data": {"bad\nkey": "value\tcolumn\x00"}},
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert captured.err == ""
    assert captured.out == "bad\\nkey\tvalue\\tcolumn\\u0000\n"


def test_canceled_cli_sync_does_not_leave_running_attempt(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    machine = "canceled-machine"

    def cancel_scan(_: str | Path) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(application, "scan_local_steam", cancel_scan)
    code, canceled, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "sync",
            "installed",
            "--machine",
            machine,
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 1
    assert stderr == ""
    assert canceled["error"]["message"] == "Operation canceled."
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        latest = storage.latest_sync(capability="installed", machine_id=machine)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error_code == "SCAN_CANCELED"

    code, queried, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "installed",
            "--machine",
            machine,
            "--format",
            "json",
        ],
        capsys,
    )
    warning_codes = {
        warning["code"] for warning in queried["completeness"]["warnings"]
    }

    assert code == 0
    assert stderr == ""
    assert queried["data"]["snapshot"]["last_attempt_status"] == "failed"
    assert "SYNC_IN_PROGRESS" not in warning_codes
