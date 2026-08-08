from __future__ import annotations

import os
from pathlib import Path

import pytest

from steam_agent import local_steam
from steam_agent.local_steam import (
    KEYVALUES_PARSER_VERSION,
    MAX_KEYVALUES_BYTES,
    MAX_KEYVALUES_DEPTH,
    KeyValuesError,
    WarningKind,
    parse_keyvalues,
    scan_local_steam,
)
from steam_agent.application import sync_installed
from steam_agent.storage import Storage


FIXTURES = Path(__file__).parent / "fixtures" / "steam"


def test_minimal_keyvalues_parser_supports_comments_nesting_and_escapes() -> None:
    parsed = parse_keyvalues(
        '\ufeff// comment\n"root" { "path" "C:\\\\Games" "label" "A \\"quote\\"" }'
    )

    assert parsed == {"root": {"path": "C:\\Games", "label": 'A "quote"'}}


@pytest.mark.parametrize(
    "text",
    [
        '"root" { "key" "value"',
        '"root" value_without_quotes',
        "}",
    ],
)
def test_minimal_keyvalues_parser_rejects_malformed_input(text: str) -> None:
    with pytest.raises(KeyValuesError):
        parse_keyvalues(text)


def test_parser_error_never_echoes_key_content() -> None:
    secret_key = "super-secret-token-value"

    with pytest.raises(KeyValuesError) as error:
        parse_keyvalues(f'"root" {{ "{secret_key}"')

    assert secret_key not in str(error.value)
    assert "missing value for key" in str(error.value)


def test_parser_rejects_case_insensitive_duplicate_keys_without_echoing() -> None:
    first = "SensitiveKey"
    second = "sensitivekey"

    with pytest.raises(KeyValuesError) as error:
        parse_keyvalues(f'"root" {{ "{first}" "one" "{second}" "two" }}')

    assert "duplicate key" in str(error.value)
    assert first not in str(error.value)
    assert second not in str(error.value)


def test_scans_multiple_library_roots_and_normalizes_manifests() -> None:
    root = FIXTURES / "valid" / "root"

    result = scan_local_steam(root)

    assert result.parser_version == KEYVALUES_PARSER_VERSION
    assert [library.path.name for library in result.libraries] == ["root", "secondary"]
    assert [app.appid for app in result.apps] == [10, 20]
    alpha, beta = result.apps
    assert alpha.name == "Alpha Game"
    assert alpha.install_dir == (root / "steamapps" / "common" / "Alpha Game").resolve()
    assert alpha.build_id == 12345
    assert alpha.size_on_disk_bytes == 987654321
    assert alpha.state_flags == 4
    assert beta.name == 'Beta "Deluxe"'
    assert beta.library_path.name == "secondary"
    assert result.warnings == ()


def test_partial_scan_returns_typed_warnings_and_keeps_safe_evidence() -> None:
    root = FIXTURES / "problems" / "root"

    result = scan_local_steam(root)

    assert [app.appid for app in result.apps] == [10, 40]
    unsafe = result.apps[1]
    assert unsafe.install_dir is None
    assert unsafe.build_id is None
    assert unsafe.size_on_disk_bytes is None

    codes = {warning.code for warning in result.warnings}
    assert {
        "duplicate_app",
        "inaccessible_library",
        "malformed_keyvalues",
        "missing_manifest",
        "unsafe_install_dir",
    } <= codes
    assert {warning.kind for warning in result.warnings} >= {
        WarningKind.MALFORMED,
        WarningKind.INACCESSIBLE,
        WarningKind.DUPLICATE,
        WarningKind.MISSING,
    }


def test_missing_index_still_scans_primary_library(tmp_path: Path) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Seven" "installdir" "Seven" "StateFlags" "4" }',
        encoding="utf-8",
    )

    result = scan_local_steam(tmp_path)

    assert [app.appid for app in result.apps] == [7]
    assert "missing_libraryfolders" in {warning.code for warning in result.warnings}


def test_inaccessible_library_index_inspection_is_not_reported_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    library_file = steamapps / "libraryfolders.vdf"
    original_lstat = Path.lstat

    def deny_lstat(path: Path):
        if path == library_file:
            raise PermissionError("denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", deny_lstat)
    result = scan_local_steam(tmp_path)

    codes = [warning.code for warning in result.warnings]
    assert codes.count("inaccessible_libraryfolders") == 1
    assert "missing_libraryfolders" not in codes
    assert "duplicate_library" not in codes


def test_present_unreadable_library_index_is_inaccessible_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    library_file = steamapps / "libraryfolders.vdf"
    library_file.write_text('"libraryfolders" {}', encoding="utf-8")
    original_read_text = Path.read_text

    def deny_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == library_file:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_read)
    result = scan_local_steam(tmp_path)

    codes = [warning.code for warning in result.warnings]
    assert codes.count("inaccessible_file") == 1
    assert "missing_libraryfolders" not in codes
    assert "duplicate_library" not in codes


def test_manifest_filename_fallback_and_appid_mismatch_are_explicit(tmp_path: Path) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_8.acf").write_text(
        '"AppState" { "name" "Eight" "installdir" "Eight" "StateFlags" "4" }', encoding="utf-8"
    )
    (steamapps / "appmanifest_9.acf").write_text(
        '"AppState" { "appid" "90" "name" "Ninety" "installdir" "Ninety" "StateFlags" "4" }',
        encoding="utf-8",
    )

    result = scan_local_steam(tmp_path)

    assert [app.appid for app in result.apps] == [8, 90]
    assert {warning.code for warning in result.warnings} >= {
        "missing_manifest_appid",
        "manifest_appid_mismatch",
    }


def test_scanner_does_not_write_to_fake_root(tmp_path: Path) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    manifest = steamapps / "appmanifest_1.acf"
    manifest.write_text(
        '"AppState" { "appid" "1" "name" "One" "installdir" "One" }',
        encoding="utf-8",
    )
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    scan_local_steam(tmp_path)

    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize(
    ("filename_appid", "manifest_appid"),
    [(0, "0"), (8, "-8")],
)
def test_nonpositive_appid_warns_skips_and_syncs_partial(
    tmp_path: Path, filename_appid: int, manifest_appid: str
) -> None:
    steam_root = tmp_path / "steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / f"appmanifest_{filename_appid}.acf").write_text(
        (
            '"AppState" { '
            f'"appid" "{manifest_appid}" '
            '"name" "Invalid" "installdir" "Invalid" }'
        ),
        encoding="utf-8",
    )

    with Storage(tmp_path / "data" / "steam-agent.sqlite3") as storage:
        result = sync_installed(
            storage,
            steam_root=steam_root,
            machine_id="test-machine",
        )

    assert result.run.status == "partial"
    assert result.scan.apps == ()
    assert "invalid_manifest_appid" in {
        warning.code for warning in result.scan.warnings
    }


def test_legacy_string_library_entry_warns_instead_of_being_silent(
    tmp_path: Path,
) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "libraryfolders.vdf").write_text(
        '"LibraryFolders" { "0" "/legacy/steam" }', encoding="utf-8"
    )

    result = scan_local_steam(tmp_path)

    assert [library.path for library in result.libraries] == [tmp_path.resolve()]
    assert "unsupported_library_entry" in {
        warning.code for warning in result.warnings
    }


def test_unsafe_and_nonregular_manifest_candidates_warn_without_being_read(
    tmp_path: Path,
) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    valid = steamapps / "appmanifest_7.acf"
    valid.write_text(
        '"AppState" { "appid" "7" "name" "Seven" "installdir" "Seven" "StateFlags" "4" }',
        encoding="utf-8",
    )
    (steamapps / "appmanifest_not-a-number.acf").mkdir()
    symlink = steamapps / "appmanifest_8.acf"
    try:
        symlink.symlink_to(valid)
    except OSError as exc:  # pragma: no cover - platform privilege limitation
        pytest.skip(f"symlinks unavailable: {exc}")

    result = scan_local_steam(tmp_path)

    assert [app.appid for app in result.apps] == [7]
    assert {warning.code for warning in result.warnings} >= {
        "nonregular_manifest_entry",
        "unsafe_manifest_entry",
    }


def test_unresolvable_declared_library_is_typed_partial_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop)
    except OSError as exc:  # pragma: no cover - platform privilege limitation
        pytest.skip(f"symlinks unavailable: {exc}")
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" {'
        ' "0" { "path" "." "apps" { "7" "1" } }'
        ' "1" { "path" "../loop" "apps" { "8" "1" } }'
        " }",
        encoding="utf-8",
    )
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Seven" "installdir" "Seven" "StateFlags" "4" }',
        encoding="utf-8",
    )

    result = scan_local_steam(root)

    assert [app.appid for app in result.apps] == [7]
    assert "unresolvable_library_path" in {
        warning.code for warning in result.warnings
    }


@pytest.mark.parametrize(
    ("state_flags", "included"),
    [
        ("4", True),
        ("6", True),
        (str(4 | (1 << 20)), True),
        ("2", False),
        (str(1 << 20), False),
        ("0", False),
        ("not-a-number", False),
    ],
)
def test_installed_scope_requires_fully_installed_state_bit(
    tmp_path: Path, state_flags: str, included: bool
) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_7.acf").write_text(
        (
            '"AppState" { "appid" "7" "name" "Seven" '
            f'"installdir" "Seven" "StateFlags" "{state_flags}" }}'
        ),
        encoding="utf-8",
    )

    result = scan_local_steam(tmp_path)

    assert ([app.appid for app in result.apps] == [7]) is included
    if not included:
        assert {warning.code for warning in result.warnings} & {
            "not_fully_installed",
            "missing_or_invalid_state_flags",
        }


def test_installed_scope_skips_manifest_without_state_flags(tmp_path: Path) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Seven" "installdir" "Seven" }',
        encoding="utf-8",
    )

    result = scan_local_steam(tmp_path)

    assert result.apps == ()
    assert "missing_or_invalid_state_flags" in {
        warning.code for warning in result.warnings
    }


def test_not_fully_installed_manifest_yields_partial_sync_not_projection(
    tmp_path: Path,
) -> None:
    steam_root = tmp_path / "steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Downloading" '
        '"installdir" "Downloading" "StateFlags" "1048576" }',
        encoding="utf-8",
    )

    with Storage(tmp_path / "data" / "steam-agent.sqlite3") as storage:
        result = sync_installed(
            storage, steam_root=steam_root, machine_id="test-machine"
        )
        projected = storage.list_installed("test-machine")

    assert result.run.status == "partial"
    assert projected == []
    assert "not_fully_installed" in {
        warning.code for warning in result.scan.warnings
    }


def test_updating_manifest_with_existing_install_dir_is_installed_without_warning(
    tmp_path: Path,
) -> None:
    steamapps = tmp_path / "steamapps"
    install_dir = steamapps / "common" / "Updating"
    install_dir.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "." "apps" { "7" "1" } } }',
        encoding="utf-8",
    )
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Updating" '
        '"installdir" "Updating" "StateFlags" "1048576" }',
        encoding="utf-8",
    )

    result = scan_local_steam(tmp_path)

    assert [app.appid for app in result.apps] == [7]
    assert result.apps[0].state_flags == 1 << 20
    assert result.warnings == ()


def test_manifest_enumeration_error_is_typed_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()

    def deny_scandir(path: object):
        raise PermissionError("denied")

    monkeypatch.setattr("steam_agent.local_steam.os.scandir", deny_scandir)
    result = scan_local_steam(tmp_path)

    assert result.apps == ()
    assert "inaccessible_library" in {warning.code for warning in result.warnings}


def test_primary_library_symlink_spelling_is_canonicalized_without_duplicate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform privilege limitation
        pytest.skip(f"symlinks unavailable: {exc}")
    (steamapps / "libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{alias}" "apps" {{ "7" "1" }} }} }}',
        encoding="utf-8",
    )
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Seven" '
        '"installdir" "Seven" "StateFlags" "4" }',
        encoding="utf-8",
    )

    result = scan_local_steam(root)

    assert len(result.libraries) == 1
    assert result.libraries[0].path == root.resolve()
    assert "duplicate_library" not in {warning.code for warning in result.warnings}


def test_out_of_range_manifest_integers_warn_without_sqlite_overflow(
    tmp_path: Path,
) -> None:
    steam_root = tmp_path / "steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    huge = str(1 << 63)
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Huge fields" '
        f'"installdir" "Huge" "StateFlags" "4" "buildid" "{huge}" '
        f'"SizeOnDisk" "{huge}" }}',
        encoding="utf-8",
    )
    (steamapps / f"appmanifest_{huge}.acf").write_text(
        f'"AppState" {{ "appid" "{huge}" "name" "Huge app" '
        '"installdir" "Huge app" "StateFlags" "4" }',
        encoding="utf-8",
    )

    with Storage(tmp_path / "data" / "steam-agent.sqlite3") as storage:
        result = sync_installed(
            storage, steam_root=steam_root, machine_id="test-machine"
        )

    assert result.run.status == "partial"
    assert [app.appid for app in result.scan.apps] == [7]
    assert result.scan.apps[0].build_id is None
    assert result.scan.apps[0].size_on_disk_bytes is None
    assert "manifest_integer_out_of_range" in {
        warning.code for warning in result.scan.warnings
    }


def test_uninstalled_bit_disqualifies_even_with_fully_installed_bit(
    tmp_path: Path,
) -> None:
    steam_root = tmp_path / "steam"
    steamapps = steam_root / "steamapps"
    install_dir = steamapps / "common" / "Contradictory"
    install_dir.mkdir(parents=True)
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Contradictory" '
        '"installdir" "Contradictory" "StateFlags" "5" }',
        encoding="utf-8",
    )

    with Storage(tmp_path / "data" / "steam-agent.sqlite3") as storage:
        result = sync_installed(
            storage, steam_root=steam_root, machine_id="test-machine"
        )
        projected = storage.list_installed("test-machine")

    assert result.run.status == "partial"
    assert result.scan.apps == ()
    assert projected == []
    assert "uninstalled_app_state" in {
        warning.code for warning in result.scan.warnings
    }


def test_duplicate_manifest_key_yields_content_safe_partial_sync(
    tmp_path: Path,
) -> None:
    secret_key = "private-field-name"
    steam_root = tmp_path / "steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "." "apps" { "7" "1" } } }',
        encoding="utf-8",
    )
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "StateFlags" "4" '
        f'"{secret_key}" "one" "{secret_key.upper()}" "two" }}',
        encoding="utf-8",
    )

    with Storage(tmp_path / "data" / "steam-agent.sqlite3") as storage:
        result = sync_installed(
            storage, steam_root=steam_root, machine_id="test-machine"
        )

    assert result.run.status == "partial"
    assert result.scan.apps == ()
    malformed = next(
        warning
        for warning in result.scan.warnings
        if warning.code == "malformed_keyvalues"
    )
    assert secret_key not in malformed.message.lower()


def test_absent_optional_manifest_integers_are_none_without_warning(
    tmp_path: Path,
) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "." "apps" { "7" "1" } } }',
        encoding="utf-8",
    )
    (steamapps / "appmanifest_7.acf").write_text(
        '"AppState" { "appid" "7" "name" "Seven" '
        '"installdir" "Seven" "StateFlags" "4" }',
        encoding="utf-8",
    )

    result = scan_local_steam(tmp_path)

    assert [app.appid for app in result.apps] == [7]
    assert result.apps[0].build_id is None
    assert result.apps[0].size_on_disk_bytes is None
    assert result.warnings == ()


def test_present_invalid_optional_integers_make_sync_partial_and_preserve_last_good(
    tmp_path: Path,
) -> None:
    steam_root = tmp_path / "steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "." "apps" { "7" "1" } } }',
        encoding="utf-8",
    )
    manifest = steamapps / "appmanifest_7.acf"
    manifest.write_text(
        '"AppState" { "appid" "7" "name" "Seven" '
        '"installdir" "Seven" "StateFlags" "4" '
        '"buildid" "100" "SizeOnDisk" "10" }',
        encoding="utf-8",
    )

    with Storage(tmp_path / "data" / "steam-agent.sqlite3") as storage:
        complete = sync_installed(
            storage, steam_root=steam_root, machine_id="test-machine"
        )
        original = storage.list_installed("test-machine")
        manifest.write_text(
            '"AppState" { "appid" "7" "name" "Seven" '
            '"installdir" "Seven" "StateFlags" "4" '
            '"buildid" "not-a-number" "SizeOnDisk" "-1" }',
            encoding="utf-8",
        )
        partial = sync_installed(
            storage, steam_root=steam_root, machine_id="test-machine"
        )
        retained = storage.list_installed("test-machine")

    assert complete.run.status == "complete"
    assert partial.run.status == "partial"
    assert [warning.code for warning in partial.scan.warnings].count(
        "invalid_manifest_integer"
    ) == 2
    assert retained == original
    assert retained[0].build_id == "100"
    assert retained[0].size_bytes == 10


def test_keyvalues_parser_rejects_excessive_nesting() -> None:
    nested = '"root" {' + ('"child" {' * (MAX_KEYVALUES_DEPTH + 1))
    nested += '"value" "end"' + ("}" * (MAX_KEYVALUES_DEPTH + 2))

    with pytest.raises(KeyValuesError, match="maximum nesting depth"):
        parse_keyvalues(nested)


def test_scanner_rejects_oversized_keyvalues_file(tmp_path: Path) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    manifest = steamapps / "appmanifest_1.acf"
    manifest.write_bytes(b" " * (MAX_KEYVALUES_BYTES + 1))

    result = scan_local_steam(tmp_path)

    assert result.apps == ()
    assert "keyvalues_resource_limit" in {warning.code for warning in result.warnings}


def test_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    target = tmp_path / "outside-manifest.acf"
    target.write_text(
        '"AppState" { "appid" "99" "name" "Outside" "installdir" "Outside" }',
        encoding="utf-8",
    )
    manifest = steamapps / "appmanifest_99.acf"
    try:
        manifest.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable")

    result = scan_local_steam(tmp_path)

    assert result.apps == ()
    assert "unsafe_manifest_entry" in {warning.code for warning in result.warnings}


def _root_with_app(base: Path, appid: int = 8) -> Path:
    steamapps = base / "steamapps"
    (steamapps / "common" / "Eight").mkdir(parents=True)
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        '"AppState" { "appid" "%d" "name" "Eight" "installdir" "Eight"'
        ' "StateFlags" "4" }' % appid,
        encoding="utf-8",
    )
    return steamapps


def test_residual_content_is_measured_per_directory(tmp_path: Path) -> None:
    steamapps = _root_with_app(tmp_path)
    prefix = steamapps / "compatdata" / "8" / "pfx" / "drive_c"
    prefix.mkdir(parents=True)
    (prefix / "user.reg").write_bytes(b"x" * 1200)
    cache = steamapps / "shadercache" / "8"
    cache.mkdir(parents=True)
    (cache / "fozpipelinesv6").write_bytes(b"y" * 800)

    residual = scan_local_steam(tmp_path).apps[0].residual

    assert residual is not None
    assert residual.state == "measured"
    assert residual.compatdata_bytes == 1200
    assert residual.shadercache_bytes == 800
    # Looked for and absent is zero, which is a different claim from unknown.
    assert residual.workshop_bytes == 0


def test_residual_measurement_never_follows_directory_symlinks(tmp_path: Path) -> None:
    # A Proton prefix holds over a thousand links into the shared Proton
    # runtime, so a link inside the tree is counted at its own size and its
    # target is neither followed nor treated as an unmeasured gap.
    steamapps = _root_with_app(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge").write_bytes(b"z" * 5000)
    compatdata = steamapps / "compatdata" / "8"
    compatdata.mkdir(parents=True)
    (compatdata / "escape").symlink_to(outside, target_is_directory=True)

    residual = scan_local_steam(tmp_path).apps[0].residual

    assert residual is not None
    assert residual.state == "measured"
    assert (residual.compatdata_bytes or 0) < 5000


def test_unreadable_residual_subtree_reports_partial_not_a_smaller_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steamapps = _root_with_app(tmp_path)
    compatdata = steamapps / "compatdata" / "8"
    compatdata.mkdir(parents=True)
    (compatdata / "readable").write_bytes(b"a" * 500)
    denied = compatdata / "denied"
    denied.mkdir()
    original_scandir = os.scandir

    def deny(path: object, *args: object, **kwargs: object):
        if Path(path) == denied:
            raise PermissionError("denied")
        return original_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", deny)
    residual = scan_local_steam(tmp_path).apps[0].residual

    assert residual is not None
    assert residual.state == "partial"
    assert residual.compatdata_bytes == 500


def test_residual_walk_stops_at_the_entry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_steam, "MAX_RESIDUAL_ENTRIES", 3)
    steamapps = _root_with_app(tmp_path)
    compatdata = steamapps / "compatdata" / "8"
    compatdata.mkdir(parents=True)
    for index in range(10):
        (compatdata / f"file{index}").write_bytes(b"b" * 100)

    residual = scan_local_steam(tmp_path).apps[0].residual

    assert residual is not None
    assert residual.state == "partial"
    assert 0 <= (residual.compatdata_bytes or 0) < 1000


def test_unreadable_residual_root_is_not_reported_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steamapps = _root_with_app(tmp_path)
    compatdata = steamapps / "compatdata" / "8"
    compatdata.mkdir(parents=True)
    original_lstat = Path.lstat

    def deny(path: Path, *args: object, **kwargs: object):
        if path == compatdata:
            raise PermissionError("denied")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", deny)
    residual = scan_local_steam(tmp_path).apps[0].residual

    assert residual is not None
    assert residual.state == "partial"
    assert residual.compatdata_bytes == 0


def test_a_linked_root_is_unmeasured_rather_than_absent(tmp_path: Path) -> None:
    # The distinction that matters: a link *inside* the tree points at
    # content the tree does not own, but a link *as* the tree means nothing
    # here was measured, so a zero must not be read as an absence.
    steamapps = _root_with_app(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "big").write_bytes(b"d" * 3000)
    (steamapps / "shadercache").mkdir()
    (steamapps / "shadercache" / "8").symlink_to(outside, target_is_directory=True)

    residual = scan_local_steam(tmp_path).apps[0].residual

    assert residual is not None
    assert residual.state == "partial"
    assert residual.shadercache_bytes == 0


def test_a_tree_that_exactly_fits_the_budget_is_measured_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_steam, "MAX_RESIDUAL_ENTRIES", 4)
    steamapps = _root_with_app(tmp_path)
    compatdata = steamapps / "compatdata" / "8"
    compatdata.mkdir(parents=True)
    for index in range(4):
        (compatdata / f"file{index}").write_bytes(b"e" * 250)

    residual = scan_local_steam(tmp_path).apps[0].residual

    assert residual is not None
    assert residual.state == "measured"
    assert residual.compatdata_bytes == 1000
