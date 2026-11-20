"""Linux content platform selection uses synthetic appinfo and manifests only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest

from steam_agent.execution.content_plane import SteamcmdAdapter
from steam_agent.execution.content_platform import (
    PlatformDecision, content_platform, library_content_platform,
)
from support.appinfo_fixture import appinfo_bytes, write_appinfo


@pytest.fixture(autouse=True)
def no_steam_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's own override must not satisfy a synthetic lookup.
    monkeypatch.delenv("STEAM_AGENT_STEAM_ROOT", raising=False)


def manifest(
    library: Path, appid: int, depots: dict[int, int | None], *, installed_files: bool = True,
) -> Path:
    """A client manifest for ``common/Game``; with depots, also that game's files.

    Pass ``installed_files=False`` for a nested SteamCMD manifest inside a
    target, whose own files each test controls.
    """
    entries = "".join(
        f'\t\t"{depot}"\n\t\t{{\n\t\t\t"manifest"\t\t"1"\n'
        + (f'\t\t\t"dlcappid"\t\t"{dlc}"\n' if dlc is not None else "")
        + "\t\t}\n"
        for depot, dlc in depots.items()
    )
    path = library / "steamapps" / f"appmanifest_{appid}.acf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"installdir"\t\t"Game"\n'
        f'\t"InstalledDepots"\n\t{{\n{entries}\t}}\n}}\n',
        encoding="utf-8",
    )
    if installed_files and depots:
        game = library / "steamapps" / "common" / "Game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "installed.bin").write_bytes(b"x")
    return path


def decide(library: Path, appid: int = 10) -> PlatformDecision:
    # ``home`` points at an empty directory so the host's real Steam cache
    # can never satisfy a lookup.
    return library_content_platform(library, appid, home=library / "no-home")


@pytest.mark.parametrize("version", [28, 29])
@pytest.mark.parametrize(("depots", "expected"), [
    ({11: "linux"}, PlatformDecision("linux", "installed_linux_depots")),
    ({11: "windows"}, PlatformDecision("windows", "installed_windows_depots")),
    # A depot served to both platforms is shared content, not a conflict.
    ({11: "linux,windows", 12: "windows"}, PlatformDecision("windows", "installed_windows_depots")),
    ({11: "windows,linux", 12: "linux"}, PlatformDecision("linux", "installed_linux_depots")),
    ({11: "linux", 12: "windows"}, PlatformDecision(None, "installed_depots_mixed_platforms")),
    ({11: "macos"}, PlatformDecision(None, "installed_depots_mixed_platforms")),
])
def test_installed_depots_select_platform(tmp_path: Path, version, depots, expected) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", depots)}, version=version)
    manifest(tmp_path, 10, dict.fromkeys(depots))
    assert decide(tmp_path) == expected


@pytest.mark.parametrize(("app_oslist", "expected"), [
    ("windows,linux", PlatformDecision("linux", "platform_neutral_depots")),
    # Proton title with a single unrestricted depot: SteamCMD needs Windows.
    ("windows", PlatformDecision("windows", "platform_neutral_depots_windows_app")),
    (None, PlatformDecision(None, "app_platform_unknown")),
])
def test_unrestricted_depots_follow_app_platforms(tmp_path: Path, app_oslist, expected) -> None:
    write_appinfo(tmp_path, {10: (app_oslist, {11: None})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == expected


def test_language_and_dlc_linux_depots_do_not_make_shared_install_ambiguous(
    tmp_path: Path,
) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {
        11: None,
        12: {"oslist": "linux", "language": "german"},
        13: {"oslist": "linux", "dlcappid": "20"},
    })})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "platform_neutral_depots")


@pytest.mark.parametrize("depot", [
    {"oslist": "windows", "osarch": "64"}, {"osarch": "32"},
])
def test_windows_selection_with_architecture_depots_is_unqualified(tmp_path: Path, depot) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows", 12: depot})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_linux_selection_keeps_existing_native_behavior_with_architecture(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("linux", {11: {"oslist": "linux", "osarch": "64"}})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_dlc_depot_listed_under_base_app_is_used(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 21: {"oslist": "windows", "dlcappid": "20"}}),
        20: ("windows", {}),
    })
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_windows_selection_refuses_uncached_listed_dlc(tmp_path: Path) -> None:
    # An offline client may not have cached a DLC SteamCMD can download.
    write_appinfo(tmp_path, {10: ("windows", {11: "windows", 21: {"oslist": "windows", "dlcappid": "20"}})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "dlc_metadata_not_cached")


def test_dlc_depot_is_classified_by_its_own_app(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"}), 20: ("windows", {21: "linux"})})
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_installed_depot_missing_from_metadata_is_unknown(tmp_path: Path) -> None:
    # DLC 20 is cached but does not describe depot 21.
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"}), 20: ("windows", {})})
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision(None, "installed_depot_not_in_metadata")


@pytest.mark.parametrize(("apps", "expected"), [
    ({10: ("windows,linux", {11: "linux", 12: "windows"})},
     PlatformDecision("linux", "native_linux_available")),
    # A new Proton install is not qualified; never guess Windows depots.
    ({10: ("windows", {11: "windows"})}, PlatformDecision(None, "new_install_without_linux_depot")),
    ({20: ("linux", {})}, PlatformDecision(None, "app_metadata_not_cached")),
])
def test_new_install_is_native_only(tmp_path: Path, apps, expected) -> None:
    write_appinfo(tmp_path, apps)
    assert decide(tmp_path) == expected


def test_install_without_recorded_depots_uses_new_install_rule(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {})
    assert decide(tmp_path) == PlatformDecision("linux", "native_linux_available")
    # Files already in the directory may belong to either platform, and no
    # broker marker records which one wrote them.
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True, exist_ok=True)
    (game / "game.exe").write_bytes(b"MZ")
    assert decide(tmp_path) == PlatformDecision(None, "owned_content_platform_unknown")


def test_steamcmd_manifest_is_evidence_when_client_manifest_is_missing(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "Game"
    # An earlier Windows-depot run left only SteamCMD's nested manifest.
    manifest(target, 10, {11: None}, installed_files=False)
    (target / "game.bin").write_bytes(b"x")  # the recorded depot's files
    assert library_content_platform(tmp_path, 10, target=target, home=tmp_path / "no-home") == (
        PlatformDecision("windows", "installed_windows_depots")
    )
    assert decide(tmp_path) == PlatformDecision("linux", "native_linux_available")


def test_depotfromapp_depot_uses_its_defining_app(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}, 12: "linux"}),
        30: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None, 13: None})
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_depotfromapp_without_definition_is_unknown(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}})})
    manifest(tmp_path, 10, {11: None, 13: None})
    assert decide(tmp_path) == PlatformDecision(None, "source_depot_not_in_metadata")


def test_dlc_architecture_depots_share_the_windows_gate(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows"}),
        20: ("windows", {21: {"oslist": "windows", "osarch": "64"}}),
    })
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_oversized_depot_keys_are_ignored_not_crashing(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("linux", {11: "linux", "9" * 5000: "windows"})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


@pytest.mark.parametrize("data", [
    b"", b"\x00" * 16, b"\x29\x44\x56\x07\x01\x00\x00\x00" + b"\xff" * 8,
])
def test_unreadable_metadata_is_unknown(tmp_path: Path, data: bytes) -> None:
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(data)
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_truncated_entry_is_unknown(tmp_path: Path) -> None:
    data = appinfo_bytes({10: ("linux", {11: "linux"})}, version=28)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(data[:40])
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_missing_metadata_and_unreadable_manifest_are_unknown(tmp_path: Path) -> None:
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unavailable")
    write_appinfo(tmp_path, {10: ("linux", {11: "linux"})})
    path = manifest(tmp_path, 10, {11: None})
    path.write_text('"AppState" { "InstalledDepots" "oops" }', encoding="utf-8")
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_secondary_library_reads_client_root_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    root = home / ".local" / "share" / "Steam"
    _client(root, {10: ("windows", {11: "windows"})}, [root, library])
    path = manifest(library, 10, {11: None})
    assert content_platform(library=library, appid=10, manifest=path, home=home) == (
        PlatformDecision("windows", "installed_windows_depots")
    )


@pytest.mark.parametrize(("platform", "override"), [
    ("linux", []), ("windows", ["+@sSteamCmdForcePlatformType", "windows"]),
])
def test_adapter_adds_platform_override_only_for_windows_depots(
    tmp_path: Path, monkeypatch, platform, override,
) -> None:
    calls: list[list[str]] = []
    adapter = SteamcmdAdapter(
        steamcmd_script=tmp_path / "steamcmd.sh", private_home=tmp_path / "home",
        log_dir=tmp_path / "logs",
    )

    def spawn(argv, *_args):
        calls.append(argv)
        raise OSError("synthetic")

    monkeypatch.setattr(adapter, "_spawn", spawn)
    adapter.install(
        account="synthetic", appid=10, install_dir=tmp_path / "Game",
        operation_id=1, validate=True, platform=platform,
    )
    (argv,) = calls
    assert argv[3:3 + len(override)] == override
    assert argv.index("+force_install_dir") == 3 + len(override)
    assert argv[-3:] == ["10", "validate", "+quit"]


def test_every_unqualified_decision_uses_the_public_reason_vocabulary(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import UNQUALIFIED_REASONS
    from steam_agent.execution.receipts import reason_code

    execution = Path(__file__).parents[1] / "src/steam_agent/execution"
    source = "".join(
        (execution / name).read_text() for name in ("content_platform.py", "executor.py")
    )
    used = set(re.findall(r'PlatformDecision\(None, "([a-z_]+)"\)', source))
    assert used == UNQUALIFIED_REASONS
    assert reason_code(
        "title content platform is unqualified: windows_depot_architecture_unqualified"
    ) == "content_platform_windows_depot_architecture_unqualified"
    assert reason_code("title content platform is unqualified: /private/x") == (
        "content_platform_unqualified"
    )


def test_new_install_ignores_linux_dlc_depots(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows", 21: {"oslist": "linux", "dlcappid": "20"}})})
    assert decide(tmp_path) == PlatformDecision(None, "new_install_without_linux_depot")


def test_nested_manifest_without_depots_checks_its_own_target(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    # SteamCMD's manifest names another installdir; the files are in target.
    manifest(target, 10, {}, installed_files=False)
    (target / "partial.bin").write_bytes(b"x")
    # Without a broker marker, nothing records which platform wrote the files.
    assert library_content_platform(tmp_path, 10, target=target, home=tmp_path / "no-home") == (
        PlatformDecision(None, "owned_content_platform_unknown")
    )
    # The broker's inode-bound marker supplies the platform of its own partial run.
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("windows", "owned_content_platform_recorded")
    (target / "partial.bin").unlink()
    assert library_content_platform(tmp_path, 10, target=target, home=tmp_path / "no-home") == (
        PlatformDecision("linux", "native_linux_available")
    )


def test_uninstalled_source_depot_architecture_shares_the_windows_gate(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 13: {"depotfromapp": "30"}}),
        30: ("windows", {13: {"oslist": "windows", "osarch": "64"}}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


@pytest.mark.parametrize("depot", [{"config": "windows"}, {"config": {"oslist": 7}}])
def test_malformed_depot_restrictions_fail_closed(tmp_path: Path, depot) -> None:
    from support.appinfo_fixture import _encode
    import struct

    tree = {"appinfo": {"common": {"oslist": "windows,linux"}, "depots": {"11": depot}}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    data = struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(data + header + kv + struct.pack("<I", 0))
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_unresolved_source_depot_blocks_windows_selection(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows", 13: {"depotfromapp": "30"}})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "source_depot_not_in_metadata")


@pytest.mark.parametrize(("recorded", "expected"), [
    ("absent", "linux"),  # markers from native-only brokers carry no platform
    ("linux", "linux"),
    # The new-install rule would pick native; the files are Windows depots.
    ("windows", "windows"),
    ("no-marker", None),
])
@pytest.mark.parametrize("nested_directory", [False, True])
def test_owned_target_without_manifests_uses_recorded_platform(
    tmp_path: Path, recorded, expected, nested_directory,
) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    target.mkdir(parents=True)
    (target / "partial.bin").write_bytes(b"x")
    if nested_directory:
        # A directory where the nested manifest belongs is not evidence.
        (target / "steamapps" / "appmanifest_10.acf").mkdir(parents=True)
    marker = tmp_path / "owned" / "10.json"
    if recorded != "no-marker":
        marker.parent.mkdir()
        identity = os.lstat(target)
        record = {"install_dir_name": "app_10", "dev": identity.st_dev, "ino": identity.st_ino}
        if recorded != "absent":
            record["platform"] = recorded
        marker.write_text(json.dumps(record))

    decision = library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    )

    assert decision.platform == expected
    if expected is None:
        assert decision.reason == "owned_content_platform_unknown"


def test_manifest_without_installed_depot_block_is_a_queued_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    path = tmp_path / "steamapps" / "appmanifest_10.acf"
    path.parent.mkdir(parents=True)
    path.write_text('"AppState"\n{\n\t"appid"\t\t"10"\n\t"installdir"\t\t"Game"\n}\n')
    assert decide(tmp_path) == PlatformDecision("linux", "native_linux_available")


def test_chained_depotfromapp_reference_is_refused(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("windows", {13: {"depotfromapp": "40"}}),
        40: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None, 13: None})
    assert decide(tmp_path) == PlatformDecision(None, "source_depot_not_in_metadata")


def test_staged_steamcmd_files_count_as_target_content(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    staged = target / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({
        "install_dir_name": "app_10", "dev": identity.st_dev, "ino": identity.st_ino,
        "platform": "windows",
    }))

    def platform() -> PlatformDecision:
        return library_content_platform(
            tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
        )

    # Empty staging directories are not content.
    assert platform() == PlatformDecision("linux", "native_linux_available")
    (staged / "chunk.bin").write_bytes(b"x")
    assert platform() == PlatformDecision("windows", "owned_content_platform_recorded")


def test_manifest_for_another_appid_is_not_evidence(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    path = manifest(target, 10, {11: None}, installed_files=False)
    (target / "game.bin").write_bytes(b"x")  # the recorded depot's files
    path.write_text(path.read_text().replace('"appid"\t\t"10"', '"appid"\t\t"20"'))
    assert library_content_platform(tmp_path, 10, target=target, home=tmp_path / "no-home") == (
        PlatformDecision(None, "installed_depots_unreadable")
    )


def test_string_table_tail_is_read_within_a_byte_budget(tmp_path: Path, monkeypatch) -> None:
    from steam_agent.execution import content_platform

    data = appinfo_bytes({10: ("linux", {11: "linux"})})
    path = tmp_path / "appinfo.vdf"
    # Valid table followed by a large unterminated tail.
    path.write_bytes(data + b"x" * 4096)
    monkeypatch.setattr(content_platform, "_MAX_STRING_TABLE_BYTES", 64)
    with pytest.raises(content_platform.AppInfoError):
        content_platform.read_app_platforms(path, {10})
    monkeypatch.setattr(content_platform, "_MAX_STRING_TABLE_BYTES", 1 << 20)
    assert 10 in content_platform.read_app_platforms(path, {10})


@pytest.mark.parametrize(("depots", "expected"), [
    ({11: "windows"}, PlatformDecision("windows", "owned_content_platform_recorded")),
    # Metadata refreshed after the interruption: the Windows gate still applies.
    ({11: "windows", 12: {"oslist": "windows", "osarch": "64"}},
     PlatformDecision(None, "windows_depot_architecture_unqualified")),
    ({11: "windows", 13: {"depotfromapp": "30"}},
     PlatformDecision(None, "source_depot_not_in_metadata")),
])
def test_marker_resumes_windows_partial_only_through_the_windows_gate(
    tmp_path: Path, depots, expected,
) -> None:
    write_appinfo(tmp_path, {10: ("windows", depots)})
    target = tmp_path / "steamapps" / "common" / "app_10"
    target.mkdir(parents=True)
    (target / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    # Neither manifest survives, so the new-install rule alone would refuse.
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == expected


def test_scalar_numeric_depot_entry_fails_closed(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode
    import struct

    tree = {"appinfo": {"common": {"oslist": "windows"}, "depots": {"11": {"config": {"oslist": "windows"}}, "12": "oops"}}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    data = struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(data + header + kv + struct.pack("<I", 0))
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_uninstalled_dlc_architecture_shares_the_windows_gate(tmp_path: Path) -> None:
    # The base app names DLC 20; only the DLC's own entry declares osarch.
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 21: {"dlcappid": "20"}}),
        20: ("windows", {21: {"oslist": "windows", "osarch": "64"}}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_marker_recovers_partial_under_queued_client_manifest(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {})  # the client's queued install: no depots yet
    target = tmp_path / "steamapps" / "common" / "Game"
    target.mkdir(parents=True)
    (target / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"

    def platform() -> PlatformDecision:
        return library_content_platform(
            tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
        )

    # Client-staged files without a broker marker stay unqualified.
    assert platform() == PlatformDecision(None, "owned_content_platform_unknown")
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "linux"}))
    assert platform() == PlatformDecision("linux", "owned_content_platform_recorded")


def test_dlc_depot_restriction_comes_from_either_description(tmp_path: Path) -> None:
    # The base entry only tags the DLC; the DLC entry restricts it to Linux.
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: "windows", 21: {"dlcappid": "20"}}),
        20: ("linux", {21: "linux"}),
    })
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_reference_architecture_counts_even_when_source_has_none(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 13: {"depotfromapp": "30", "osarch": "64"}}),
        30: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_intake_checks_the_client_manifest_directory_not_the_default(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {})  # queued client install in common/Game
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True, exist_ok=True)
    (game / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "linux"}))
    default = tmp_path / "steamapps" / "common" / "app_10"
    assert library_content_platform(
        tmp_path, 10, target=default, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("linux", "owned_content_platform_recorded")


@pytest.mark.parametrize("listed", [20, "20", "20, 21"])
def test_listofdlc_accepts_cached_integer_and_string_forms(tmp_path: Path, listed) -> None:
    from support.appinfo_fixture import _encode
    from steam_agent.execution.content_platform import read_app_platforms
    import struct

    tree = {"appinfo": {"common": {"oslist": "windows"}, "extended": {"listofdlc": listed},
                        "depots": {"11": {"config": {"oslist": "windows"}}}}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    path = tmp_path / "appinfo.vdf"
    path.write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    assert 20 in read_app_platforms(path, {10})[10].dlc_apps


def test_scalar_depot_table_fails_closed(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode
    import struct

    tree = {"appinfo": {"common": {"oslist": "windows"}, "depots": "oops"}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_windows_gate_refuses_chained_uninstalled_reference(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 13: {"depotfromapp": "30"}}),
        30: ("windows", {13: {"depotfromapp": "40"}}),
        40: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "source_depot_not_in_metadata")


def test_installed_dlc_depot_found_through_base_tag(tmp_path: Path) -> None:
    # The manifest omits dlcappid; the base app's tag names the DLC.
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: "windows", 21: {"dlcappid": "20"}}),
        20: ("linux", {21: "linux"}),
    })
    manifest(tmp_path, 10, {11: None, 21: None})
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_conflicting_source_definitions_for_one_installed_depot_refuse(tmp_path: Path) -> None:
    # Base and DLC references name different sources that disagree.
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 21: {"dlcappid": "20", "depotfromapp": "30"}}),
        20: ("windows,linux", {21: {"depotfromapp": "40"}}),
        30: ("windows", {21: "windows"}),
        40: ("linux", {21: "linux"}),
    })
    manifest(tmp_path, 10, {11: None, 21: 20})
    # Refused: the DLC entry redirects again, and the definitions disagree.
    assert decide(tmp_path).platform is None


def test_reference_architecture_with_windows_definition_is_gated(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 13: {"depotfromapp": "30", "osarch": "64"}}),
        30: ("windows", {13: None}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_scalar_extended_section_fails_closed(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode
    import struct

    tree = {"appinfo": {"common": {"oslist": "windows"}, "extended": "oops",
                        "depots": {"11": {"config": {"oslist": "windows"}}}}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_nested_depots_are_read_under_a_queued_client_manifest(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    client = manifest(tmp_path, 10, {})  # queued client install in common/Game
    target = tmp_path / "steamapps" / "common" / "Game"
    nested = manifest(target, 10, {11: None}, installed_files=False)  # SteamCMD already wrote Windows depots
    (target / "data.bin").write_bytes(b"x")
    os.utime(client, ns=(1_000_000_000, 1_000_000_000))
    os.utime(nested, ns=(2_000_000_000, 2_000_000_000))
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")
    # A SteamCMD manifest older than the queued install is a leftover.
    os.utime(client, ns=(3_000_000_000, 3_000_000_000))
    assert decide(tmp_path) == PlatformDecision(None, "owned_content_platform_unknown")


@pytest.mark.parametrize(("depots", "expected"), [
    ({11: "windows,macos"}, PlatformDecision(None, "installed_depots_mixed_platforms")),
    ({11: "windows,macos", 12: "windows"}, PlatformDecision("windows", "installed_windows_depots")),
])
def test_windows_macos_depot_is_windows_evidence_only_with_a_windows_depot(
    tmp_path: Path, depots, expected,
) -> None:
    write_appinfo(tmp_path, {10: ("windows,macos", depots)})
    manifest(tmp_path, 10, dict.fromkeys(depots))
    assert decide(tmp_path) == expected


def test_manifest_named_dlc_missing_from_cache_blocks_windows(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows", 21: "windows"})})
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision(None, "dlc_metadata_not_cached")


def test_duplicate_binary_keys_fail_closed(tmp_path: Path) -> None:
    import struct

    def key(name: str) -> bytes:
        return name.encode() + b"\0"

    config = b"\x00" + key("config") + b"\x01" + key("oslist") + key("windows") + b"\x08"
    depot = b"\x00" + key("11") + config + config + b"\x08"
    kv = (b"\x00" + key("appinfo") + b"\x00" + key("common") + b"\x01" + key("oslist")
          + key("windows") + b"\x08" + b"\x00" + key("depots") + depot + b"\x08" + b"\x08" + b"\x08")
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


@pytest.mark.parametrize(("depots", "expected"), [
    ({11: "linux,macos"}, PlatformDecision(None, "installed_depots_mixed_platforms")),
    ({11: "linux,macos", 12: "linux"}, PlatformDecision("linux", "installed_linux_depots")),
])
def test_linux_macos_depot_is_linux_evidence_only_with_a_linux_depot(
    tmp_path: Path, depots, expected,
) -> None:
    write_appinfo(tmp_path, {10: ("linux,macos", depots)})
    manifest(tmp_path, 10, dict.fromkeys(depots))
    assert decide(tmp_path) == expected


def test_unattributed_depot_is_found_in_a_listed_dlc(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    # Base app 10 lists DLC 20 only through listofdlc; depot 21 lives in 20.
    trees = {10: _tree(10, ("windows", {11: "windows"})), 20: _tree(20, ("windows", {21: "windows"}))}
    trees[10]["appinfo"]["extended"] = {"listofdlc": "20"}
    body = b""
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    for appid, tree in trees.items():
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None, 21: None})
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_unreadable_nested_manifest_under_queued_client_manifest_refuses(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {})
    target = tmp_path / "steamapps" / "common" / "Game"
    nested = manifest(target, 10, {11: None}, installed_files=False)
    nested.write_text('"AppState" { "appid" "10" "InstalledDepots" "oops" }')
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_steam_root_override_finds_cache_for_a_secondary_library(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "custom-root"
    library = tmp_path / "games"
    _client(root, {10: ("linux", {11: "linux"})}, [root, library])
    manifest(library, 10, {11: None})
    monkeypatch.setenv("STEAM_AGENT_STEAM_ROOT", str(root))
    assert library_content_platform(library, 10, home=tmp_path / "no-home") == (
        PlatformDecision("linux", "installed_linux_depots")
    )
    # An override naming a client that does not list the library is not evidence.
    _client(root, {10: ("linux", {11: "linux"})}, [root])
    assert library_content_platform(library, 10, home=tmp_path / "no-home") == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_marker_disagreeing_with_stale_nested_manifest_refuses(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    manifest(target, 10, {12: None}, installed_files=False)  # left by an older native run
    (target / "game.bin").write_bytes(b"x")  # the recorded depot's files
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "linux"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("linux", "installed_linux_depots")


def _client(root: Path, apps, libraries: list[Path]) -> None:
    write_appinfo(root, apps)
    entries = "".join(
        f'\t"{index}"\n\t{{\n\t\t"path"\t\t"{path}"\n\t}}\n' for index, path in enumerate(libraries)
    )
    (root / "steamapps").mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n{entries}}}\n')


def test_secondary_library_uses_the_client_that_lists_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    _client(native, {10: ("linux", {11: "linux"})}, [native])
    _client(flatpak, {10: ("windows", {11: "windows"})}, [flatpak, library])
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision("windows", "installed_windows_depots")
    )
    # With two clients and neither listing the library, no cache is evidence.
    _client(flatpak, {10: ("windows", {11: "windows"})}, [flatpak])
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_untagged_base_depot_entry_is_authoritative_over_listed_dlc(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    trees = {
        10: _tree(10, ("windows,linux", {11: "linux", 21: None})),
        20: _tree(20, ("windows", {21: "windows"})),
    }
    trees[10]["appinfo"]["extended"] = {"listofdlc": "20"}
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    body = b""
    for appid, tree in trees.items():
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None, 21: None})
    # The base app describes depot 21 without a DLC tag, so it is a base-game
    # depot; an unrelated listed DLC entry does not reclassify it.
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_deferred_platform_detail_keeps_its_reason_code() -> None:
    from steam_agent.execution.receipts import _platform_reason_code

    assert _platform_reason_code(
        "content platform unqualified: dlc_metadata_not_cached; deferred"
    ) == "content_platform_dlc_metadata_not_cached"
    assert _platform_reason_code("content platform unqualified: /home/x; deferred") == (
        "content_platform_unqualified"
    )


def test_owning_client_without_cache_refuses_rather_than_borrowing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    _client(native, {10: ("linux", {11: "linux"})}, [native])
    _client(flatpak, {10: ("windows", {11: "windows"})}, [flatpak, library])
    (flatpak / "appcache" / "appinfo.vdf").unlink()
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_looping_library_entry_does_not_break_lookup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    root = home / ".local" / "share" / "Steam"
    _client(root, {10: ("linux", {11: "linux"})}, [loop, library])
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision("linux", "installed_linux_depots")
    )


def test_mixed_case_restriction_keys_are_honored(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode
    import struct

    tree = {"appinfo": {"common": {"oslist": "windows"}, "depots": {
        "11": {"config": {"OSList": "windows"}},
        "12": {"Config": {"oslist": "windows", "OSArch": "64"}},
    }}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_post_shutdown_platform_abort_keeps_a_typed_reason(tmp_path: Path) -> None:
    from steam_agent.execution.receipts import serialize_result

    payload = {
        "operation_id": 1, "outcome": "aborted",
        "detail": "content platform changed before content start: install_directory_changed",
    }
    result = serialize_result(
        payload, exit_code=1, command="run", state_dir=tmp_path, machine_id="m", failure=None,
    )
    assert result["attempt"]["reason_code"] == "content_platform_install_directory_changed"


def test_sole_client_that_does_not_list_the_library_is_not_evidence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    root = home / ".local" / "share" / "Steam"
    _client(root, {10: ("windows", {11: "windows"})}, [root])
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_symlinked_game_directory_counts_as_target_content(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {})
    game = tmp_path / "steamapps" / "common" / "Game"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "data.bin").write_bytes(b"x")
    game.mkdir(parents=True, exist_ok=True)
    (game / "assets").symlink_to(elsewhere, target_is_directory=True)
    assert decide(tmp_path) == PlatformDecision(None, "owned_content_platform_unknown")


def test_unresolvable_root_override_is_a_structured_refusal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STEAM_AGENT_STEAM_ROOT", "~no-such-user-steam-cli-test/Steam")
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    assert library_content_platform(library, 10, home=tmp_path / "no-home") == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_manifest_keys_are_case_insensitive(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace('"InstalledDepots"', '"installeddepots"'))
    target = tmp_path / "steamapps" / "common" / "Game"
    manifest(target, 10, {12: None}, installed_files=False)  # stale native SteamCMD manifest
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_source_app_dlc_is_loaded_for_the_windows_gate(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    trees = {
        10: _tree(10, ("windows", {11: "windows", 13: {"depotfromapp": "30"}})),
        30: _tree(30, ("windows", {13: "windows"})),
        31: _tree(31, ("windows", {32: "windows"})),
    }
    trees[30]["appinfo"]["extended"] = {"listofdlc": "31"}
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    body = b""
    for appid, tree in trees.items():
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_deferred_restore_failure_keeps_platform_change_reason(tmp_path: Path) -> None:
    from steam_agent.execution.receipts import serialize_result

    payload = {
        "operation_id": 1, "outcome": "deferred",
        "detail": "content platform changed before content start: install_directory_changed;"
                  " client restore failed; state left for retry",
    }
    result = serialize_result(
        payload, exit_code=1, command="run", state_dir=tmp_path, machine_id="m", failure=None,
    )
    assert result["attempt"]["reason_code"] == "content_platform_install_directory_changed"


def test_installed_dlc_depot_with_uncached_dlc_refuses_either_platform(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: None, 21: {"dlcappid": "20"}})})
    manifest(tmp_path, 10, {11: None, 21: None})
    assert decide(tmp_path) == PlatformDecision(None, "dlc_metadata_not_cached")


def test_relative_library_entry_resolves_against_the_client_root(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    root = home / ".local" / "share" / "Steam"
    library = root.parent / "games"
    manifest(library, 10, {11: None})
    _client(root, {10: ("linux", {11: "linux"})}, [root, Path("../games")])
    monkeypatch.chdir(tmp_path)
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision("linux", "installed_linux_depots")
    )


def test_manifest_and_base_tag_naming_different_dlc_are_both_consulted(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 21: {"dlcappid": "30"}}),
        20: ("windows,linux", {}),
        30: ("windows", {21: "windows"}),
    })
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_shared_only_install_with_unused_windows_depot_is_ambiguous(tmp_path: Path) -> None:
    # A Proton install would include depot 12; its absence means this may be macOS.
    write_appinfo(tmp_path, {10: ("windows,macos", {11: None, 12: "windows"})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")


def test_client_root_without_its_cache_does_not_borrow_one(tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "other"
    library = tmp_path / "primary"
    (library / "appcache").mkdir(parents=True)  # a client root, cache missing
    _client(other, {10: ("windows", {11: "windows"})}, [other, library])
    manifest(library, 10, {11: None})
    monkeypatch.setenv("STEAM_AGENT_STEAM_ROOT", str(other))
    assert library_content_platform(library, 10, home=tmp_path / "no-home") == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_depot_only_a_listed_dlc_could_describe_needs_that_dlc_cached(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    tree = _tree(10, ("windows,linux", {11: "linux"}))  # depot 21 undescribed
    tree["appinfo"]["extended"] = {"listofdlc": "20"}  # DLC 20 is not cached
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None, 21: None})
    assert decide(tmp_path) == PlatformDecision(None, "dlc_metadata_not_cached")


def test_duplicate_normalized_depot_ids_fail_closed(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows", "011": {"oslist": "windows", "osarch": "64"}})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_mixed_case_library_index_is_recognized(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    root = home / ".local" / "share" / "Steam"
    _client(root, {10: ("linux", {11: "linux"})}, [root, library])
    index = root / "steamapps" / "libraryfolders.vdf"
    index.write_text(index.read_text().replace('"libraryfolders"', '"LibraryFolders"').replace('"path"', '"Path"'))
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision("linux", "installed_linux_depots")
    )


def test_installdir_in_a_comment_does_not_shadow_the_field(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import manifest_install_dir

    path = tmp_path / "appmanifest_10.acf"
    path.write_text('"AppState"\n{\n\t// "installdir" "Old"\n\t"appid" "10"\n\t"installdir" "Current"\n}\n')
    assert manifest_install_dir(path) == "Current"


def test_resume_never_switches_platform_over_marked_partial_content(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {12: None})  # the client now records native depots
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True, exist_ok=True)
    (game / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


def test_duplicate_app_records_fail_closed(tmp_path: Path) -> None:
    import struct
    from support.appinfo_fixture import appinfo_bytes

    single = appinfo_bytes({10: ("windows", {11: "windows"})}, version=28)
    record = single[8:-4]  # one app entry without header or terminator
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        single[:8] + record + record + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_malformed_unrelated_dlc_record_does_not_block_native(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    base = _tree(10, ("windows,linux", {11: "linux"}))
    base["appinfo"]["extended"] = {"listofdlc": "20"}
    broken = {"appinfo": {"common": {"oslist": "windows"}, "depots": "oops"}}
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    body = b""
    for appid, tree in ((10, base), (20, broken)):
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_installdir_with_control_character_is_a_structured_refusal(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    path = manifest(tmp_path, 10, {})
    path.write_text(path.read_text().replace('"Game"', '"Ga\\u0000me"'.encode().decode("unicode_escape")))
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_rewritten_queued_client_manifest_does_not_bypass_marker(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    client = manifest(tmp_path, 10, {})  # queued, rewritten by Steam later
    game = tmp_path / "steamapps" / "common" / "Game"
    manifest(game, 10, {12: None}, installed_files=False)  # stale native SteamCMD manifest
    (game / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))
    os.utime(client, ns=(3_000_000_000, 3_000_000_000))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


def test_windows_gate_merges_base_and_dlc_descriptions_of_one_depot(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 21: {"oslist": "linux", "osarch": "64", "dlcappid": "20"}}),
        20: ("windows", {21: "windows"}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


@pytest.mark.parametrize("entry", ['{ }', '{ "manifest" "oops" }'])
def test_installed_depot_needs_a_manifest_id(tmp_path: Path, entry: str) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace('{\n\t\t\t"manifest"\t\t"1"\n\t\t}', entry))
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_shared_source_architecture_counts_before_merging(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 13: {"depotfromapp": "30", "oslist": "linux"}}),
        30: ("windows", {13: {"oslist": "windows,linux", "osarch": "64"}}),
    })
    manifest(tmp_path, 10, {11: None})
    # Refused either way: the descriptions conflict, and one is shared 64-bit.
    assert decide(tmp_path).platform is None


def test_damaged_platform_on_an_owning_marker_is_unknown(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    manifest(target, 10, {12: None}, installed_files=False)
    (target / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "beos"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


def test_staged_download_keeps_the_marker_binding_over_a_newer_client(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    client = manifest(tmp_path, 10, {12: None})  # Steam later switched it to native
    game = tmp_path / "steamapps" / "common" / "Game"
    staged = game / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))
    os.utime(client, ns=(3_000_000_000, 3_000_000_000))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


@pytest.mark.parametrize("gid", ["0", "18446744073709551616"])
def test_out_of_range_depot_manifest_id_is_unreadable(tmp_path: Path, gid: str) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace('"manifest"\t\t"1"', f'"manifest"\t\t"{gid}"'))
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_no_marker_means_no_directory_walk(tmp_path: Path, monkeypatch) -> None:
    from steam_agent.execution import content_platform

    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None})
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True, exist_ok=True)
    (game / "game.exe").write_bytes(b"MZ")
    monkeypatch.setattr(content_platform, "_holds_content", lambda *_: pytest.fail("walked"))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=tmp_path / "owned" / "10.json",
        home=tmp_path / "no-home",
    ) == PlatformDecision("windows", "installed_windows_depots")


def test_half_written_manifest_keeps_a_tolerant_installdir(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import manifest_install_dir

    path = tmp_path / "appmanifest_10.acf"
    path.write_text('"AppState"\n{\n\t"appid" "10"\n\t"installdir" "Game"\n')  # no closing brace
    assert manifest_install_dir(path) == "Game"


def test_marker_recovery_gates_dlc_recorded_from_the_original_manifest(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows"}),
        20: ("windows", {21: {"oslist": "windows", "osarch": "64"}}),
    })
    target = tmp_path / "steamapps" / "common" / "app_10"
    target.mkdir(parents=True)
    (target / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    record = {"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows", "dlc_apps": [20]}
    marker.write_text(json.dumps(record))

    def platform() -> PlatformDecision:
        return library_content_platform(
            tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
        )

    assert platform() == PlatformDecision(None, "windows_depot_architecture_unqualified")
    record["dlc_apps"] = [30]  # recorded DLC no longer cached
    marker.write_text(json.dumps(record))
    assert platform() == PlatformDecision(None, "dlc_metadata_not_cached")


def test_windows_decision_carries_manifest_named_dlc(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"}), 20: ("windows", {21: "windows"})})
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path).dlc_apps == (20,)


def test_referenced_depot_uses_the_dlc_its_definition_tags(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("windows,linux", {13: {"dlcappid": "31"}}),
        31: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None, 13: None})
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_marker_recovery_keeps_the_recorded_dlc_for_the_next_marker(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"}), 20: ("windows", {21: "windows"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    target.mkdir(parents=True)
    (target / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({
        "dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows", "dlc_apps": [20],
    }))
    decision = library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    )
    assert decision == PlatformDecision("windows", "owned_content_platform_recorded")
    assert decision.dlc_apps == (20,)


def test_dlc_entry_redirecting_a_referenced_depot_is_refused(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: "linux", 13: {"depotfromapp": "30"}}),
        30: ("windows,linux", {13: {"dlcappid": "31"}}),
        31: ("windows", {13: {"depotfromapp": "40"}}),
        40: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None, 13: None})
    assert decide(tmp_path) == PlatformDecision(None, "source_depot_not_in_metadata")


def test_marker_dlc_omitted_by_the_manifest_still_gates_windows(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows"}),
        20: ("windows", {21: {"oslist": "windows", "osarch": "64"}}),
    })
    game = tmp_path / "steamapps" / "common" / "Game"
    manifest(tmp_path, 10, {11: None})
    game.mkdir(parents=True, exist_ok=True)
    (game / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({
        "dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows", "dlc_apps": [20],
    }))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_shared_data_depot_does_not_qualify_a_windows_only_new_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "linux,windows", 12: "windows"})})
    assert decide(tmp_path) == PlatformDecision(None, "new_install_without_linux_depot")


def test_trailing_bytes_in_an_app_record_fail_closed(tmp_path: Path) -> None:
    import struct
    from support.appinfo_fixture import appinfo_bytes

    data = bytearray(appinfo_bytes({10: ("windows", {11: "windows"})}, version=28))
    # Grow the record by one byte that the KeyValues terminator never reaches.
    (length,) = struct.unpack_from("<I", data, 12)
    struct.pack_into("<I", data, 12, length + 1)
    data[-4:-4] = b"\x00"
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(bytes(data))
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_depotless_nested_manifest_and_marker_allow_an_empty_native_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    manifest(target, 10, {}, installed_files=False)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("linux", "native_linux_available")


def test_missing_linux_windows_depot_makes_shared_install_ambiguous(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,macos", {11: None, 12: "linux,windows"})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")
    manifest(tmp_path, 10, {11: None, 12: None})
    assert decide(tmp_path) == PlatformDecision("windows", "platform_neutral_depots_windows_app")


def test_leftover_nested_manifest_alone_is_not_an_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    manifest(target, 10, {11: None}, installed_files=False)
    assert library_content_platform(tmp_path, 10, target=target, home=tmp_path / "no-home") == (
        PlatformDecision(None, "new_install_without_linux_depot")
    )


def test_windows_override_needs_the_app_to_list_windows(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: (None, {11: "windows"})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_platform_unknown")


def test_marker_recovery_rechecks_that_the_app_lists_windows(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("linux", {11: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    target.mkdir(parents=True)
    (target / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "app_platform_unknown")


def test_missing_unrestricted_base_depot_rules_out_both_platforms(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: None, 12: None})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")


def test_staging_beside_a_leftover_nested_manifest_is_not_an_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    manifest(target, 10, {11: None}, installed_files=False)
    staged = target / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    assert library_content_platform(tmp_path, 10, target=target, home=tmp_path / "no-home") == (
        # Not an install, and the staged bytes have no marker vouching for them.
        PlatformDecision(None, "owned_content_platform_unknown")
    )


def test_backslash_installdir_keeps_its_raw_unsafe_value(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import manifest_install_dir

    path = tmp_path / "appmanifest_10.acf"
    path.write_text('"AppState"\n{\n\t"appid" "10"\n\t"installdir" "Foo\\Bar"\n}\n')
    assert manifest_install_dir(path) == "Foo\\Bar"


def test_leftover_client_manifest_over_an_empty_directory_is_a_new_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    assert decide(tmp_path) == PlatformDecision(None, "new_install_without_linux_depot")
    game.mkdir(parents=True, exist_ok=True)
    (game / "game.exe").write_bytes(b"MZ")
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_ignored_nested_manifest_still_routes_staging_through_the_marker(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    manifest(target, 10, {11: None}, installed_files=False)
    staged = target / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    decision = library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    )
    assert decision == PlatformDecision("windows", "owned_content_platform_recorded")


@pytest.mark.parametrize(("missing", "expected"), [
    # A depot the preferred (native) platform would receive is missing.
    ("linux", PlatformDecision(None, "platform_neutral_install_ambiguous")),
    ("linux,windows", PlatformDecision(None, "platform_neutral_install_ambiguous")),
    (None, PlatformDecision(None, "platform_neutral_install_ambiguous")),
    # The platform's only depots are architecture-specific and none is installed.
    ({"oslist": "linux", "osarch": "32"}, PlatformDecision(None, "platform_neutral_install_ambiguous")),
    ({"oslist": "linux", "osarch": "64"}, PlatformDecision(None, "platform_neutral_install_ambiguous")),
    # A native install never receives this, so its absence is expected.
    ("windows", PlatformDecision("linux", "platform_neutral_depots")),
])
def test_shared_only_install_needs_every_depot_of_the_preferred_platform(
    tmp_path: Path, missing, expected,
) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: None, 12: missing})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == expected


@pytest.mark.parametrize("leftover", ["empty", "dangling", "external"])
def test_placeholders_are_not_installed_files(tmp_path: Path, leftover: str) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True)
    if leftover == "empty":
        (game / "game.exe").write_bytes(b"")
    elif leftover == "dangling":
        (game / "game.exe").symlink_to(tmp_path / "gone")
    else:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "data.bin").write_bytes(b"x")
        (game / "data").symlink_to(elsewhere, target_is_directory=True)
    assert decide(tmp_path) == PlatformDecision(None, "new_install_without_linux_depot")


def test_newer_nested_manifest_beside_staging_only_routes_to_the_marker(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    client = manifest(tmp_path, 10, {})
    game = tmp_path / "steamapps" / "common" / "Game"
    nested = manifest(game, 10, {11: None}, installed_files=False)
    staged = game / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    os.utime(client, ns=(1_000_000_000, 1_000_000_000))
    os.utime(nested, ns=(2_000_000_000, 2_000_000_000))
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("windows", "owned_content_platform_recorded")


def test_symlink_cycle_does_not_hang_the_installed_files_check(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True)
    (game / "loop").symlink_to(game, target_is_directory=True)
    assert decide(tmp_path) == PlatformDecision(None, "new_install_without_linux_depot")


def test_mixed_case_backslash_installdir_keeps_its_raw_value(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import manifest_install_dir

    path = tmp_path / "appmanifest_10.acf"
    path.write_text('"AppState"\n{\n\t"appid" "10"\n\t"InstallDir" "Foo\\Bar"\n}\n')
    assert manifest_install_dir(path) == "Foo\\Bar"


def test_missing_macos_only_depot_does_not_block_a_shared_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux,macos", {11: None, 12: "macos"})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "platform_neutral_depots")


def test_staged_proton_update_resumes_through_its_marker(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    staged = game / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"

    def platform() -> PlatformDecision:
        return library_content_platform(
            tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
        )

    assert platform() == PlatformDecision(None, "new_install_without_linux_depot")
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert platform() == PlatformDecision("windows", "installed_windows_depots")


def test_commented_backslash_installdir_is_ignored(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import manifest_install_dir

    path = tmp_path / "appmanifest_10.acf"
    path.write_text('"AppState"\n{\n\t// "InstallDir" "Old\\Name"\n\t"appid" "10"\n\t"installdir" "Current"\n}\n')
    assert manifest_install_dir(path) == "Current"


def test_newer_client_manifest_does_not_release_a_marker(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    client = manifest(tmp_path, 10, {11: None})  # Steam rewrote the Windows depot records
    game = tmp_path / "steamapps" / "common" / "Game"
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "linux"}))
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))
    os.utime(client, ns=(3_000_000_000, 3_000_000_000))
    # The native run's files may still be in place; newer records prove nothing.
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


def test_selection_needs_the_app_to_list_its_platform(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: (None, {11: "linux"})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_platform_unknown")
    write_appinfo(tmp_path, {10: ("windows", {11: "linux"})})
    assert decide(tmp_path) == PlatformDecision(None, "app_platform_unknown")


def test_unreadable_staging_is_not_a_resumable_download(tmp_path: Path, monkeypatch) -> None:
    from steam_agent.execution import content_platform

    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))

    def unreadable(_: Path, __: int) -> bool:
        raise PermissionError("staging")

    monkeypatch.setattr(content_platform, "has_staged_download", unreadable)
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "installed_depots_unreadable")


def test_referenced_base_depot_counts_unless_installed_as_shared(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("linux", {13: "linux"}),
    })
    path = manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_early_terminator_fails_closed(tmp_path: Path) -> None:
    import struct
    from support.appinfo_fixture import appinfo_bytes

    data = appinfo_bytes({10: ("windows", {11: "windows"})}, version=28)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        data[:8] + struct.pack("<I", 0) + data[8:]  # terminator before the record
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


@pytest.mark.parametrize("common", ["oops", {"oslist": 7}])
def test_malformed_common_section_fails_closed(tmp_path: Path, common) -> None:
    from support.appinfo_fixture import _encode
    import struct

    tree = {"appinfo": {"common": common, "depots": {"11": {"config": {"oslist": "linux"}}}}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_queued_empty_install_without_oslist_uses_the_new_install_rule(tmp_path: Path) -> None:
    # Without an app oslist, later updates would refuse, so the install does too.
    write_appinfo(tmp_path, {10: (None, {11: "linux"})})
    assert decide(tmp_path) == PlatformDecision(None, "new_install_without_linux_depot")
    manifest(tmp_path, 10, {})
    assert decide(tmp_path) == PlatformDecision(None, "new_install_without_linux_depot")


def test_empty_staged_placeholder_is_not_a_resumable_download(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    temp = game / "steamapps" / "temp"
    temp.mkdir(parents=True)
    (temp / "placeholder").write_bytes(b"")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "new_install_without_linux_depot")


def test_uninstalled_reference_to_windows_redistributable_allows_native(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "platform_neutral_depots")


def test_linked_staging_directory_is_not_a_resumable_download(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    (game / "steamapps").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "chunk").write_bytes(b"x")
    (game / "steamapps" / "downloading").symlink_to(elsewhere, target_is_directory=True)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ).platform is None


def test_marker_only_resume_needs_payload_bytes(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    temp = target / "steamapps" / "temp"
    temp.mkdir(parents=True)
    (temp / "placeholder").write_bytes(b"")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


def test_architecture_on_a_reference_is_gated_before_merging(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 13: {"depotfromapp": "30", "osarch": "64"}}),
        30: ("windows", {13: "linux"}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path).platform is None


def test_depot_absent_from_base_checks_every_listed_dlc(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    trees = {
        10: _tree(10, ("windows,linux", {11: "linux"})),
        20: _tree(20, ("linux", {21: "linux"})),
        40: _tree(40, ("windows", {21: "windows"})),
    }
    trees[10]["appinfo"]["extended"] = {"listofdlc": "20,40"}
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    body = b""
    for appid, tree in trees.items():
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None, 21: 20})
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_source_definition_dlc_is_loaded_for_the_shared_only_check(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("windows", {13: {"dlcappid": "31"}}),
        31: ("windows", {13: "windows"}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "platform_neutral_depots")


@pytest.mark.parametrize("source", ["40", "50"])
def test_shared_depot_must_match_and_resolve_its_source(tmp_path: Path, source: str) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("linux", {13: "linux"}),
        40: ("linux", {13: "linux"}),
    })
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", f'\n\t"SharedDepots"\n\t{{\n\t\t"13"\t\t"{source}"\n\t}}\n}}\n',
    ))
    # 40 conflicts with the base reference; 50 is not cached.
    assert decide(tmp_path) == PlatformDecision(None, "source_depot_not_in_metadata")


def test_marker_and_manifest_dlc_are_gated_together(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows", 21: "windows"}),
        20: ("windows", {25: "windows"}),
        30: ("windows", {25: "linux"}),
    })
    manifest(tmp_path, 10, {11: None, 21: 20})
    game = tmp_path / "steamapps" / "common" / "Game"
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({
        "dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows", "dlc_apps": [30],
    }))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_shared_depot_with_conflicting_source_restriction_refuses(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"oslist": "linux", "depotfromapp": "30"}}),
        30: ("windows", {13: "windows"}),
    })
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_linked_steamapps_directory_is_not_local_staging(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "downloading").mkdir(parents=True)
    (elsewhere / "downloading" / "chunk").write_bytes(b"x")
    (game / "steamapps").symlink_to(elsewhere, target_is_directory=True)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ).platform is None


def test_duplicate_shared_depot_ids_are_unreadable(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}), 30: ("linux", {13: "linux"})})
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"40"\n\t\t"013"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_shared_depot_source_dlc_is_loaded_before_validation(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None}),
        30: ("linux", {13: {"dlcappid": "31"}}),
        31: ("linux", {13: "linux"}),
    })
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_unreadable_library_index_leaves_ownership_unknown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    _client(native, {10: ("linux", {11: "linux"})}, [native, library])
    _client(flatpak, {10: ("windows", {11: "windows"})}, [flatpak, library])
    (flatpak / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders" { "0" ')
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_linked_nested_manifest_is_not_evidence(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    elsewhere = tmp_path / "elsewhere"
    real = manifest(elsewhere, 10, {11: None}, installed_files=False)
    target = tmp_path / "steamapps" / "common" / "app_10"
    (target / "steamapps").mkdir(parents=True)
    (target / "steamapps" / "appmanifest_10.acf").symlink_to(real)
    (target / "game.bin").write_bytes(b"x")
    decision = library_content_platform(tmp_path, 10, target=target, home=tmp_path / "no-home")
    assert decision.platform is None


def test_windows_only_shared_depot_is_windows_evidence(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("windows", {13: "windows"}),
    })
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path) == PlatformDecision("windows", "installed_windows_depots")


def test_malformed_numbered_library_entry_leaves_ownership_unknown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    _client(native, {10: ("linux", {11: "linux"})}, [native, library])
    _client(flatpak, {10: ("windows", {11: "windows"})}, [flatpak])
    index = flatpak / "steamapps" / "libraryfolders.vdf"
    index.write_text(index.read_text().replace('"path"', '"label"'))
    assert library_content_platform(library, 10, home=home) == (
        PlatformDecision(None, "app_metadata_unavailable")
    )


def test_target_replaced_by_a_file_reads_as_no_nested_manifest(tmp_path: Path) -> None:
    from steam_agent.execution.content_plane import locate_manifest

    target = tmp_path / "Game"
    target.write_bytes(b"not a directory")
    assert locate_manifest(install_dir=target, appid=10) is None


def test_shared_depots_do_not_qualify_unrecorded_files_of_a_queued_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"}), 30: ("windows", {13: "windows"})})
    path = manifest(tmp_path, 10, {})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    game = tmp_path / "steamapps" / "common" / "Game"
    game.mkdir(parents=True)
    (game / "leftover.bin").write_bytes(b"x")
    assert decide(tmp_path) == PlatformDecision(None, "owned_content_platform_unknown")


@pytest.mark.parametrize("record", [
    '{"dev": 0, "ino": 0, "platform": "windows", "platform": "linux"}',
    '{"dev": 0, "ino": 0, "Platform": "windows"}',
])
def test_ambiguous_marker_keys_are_damage(tmp_path: Path, record: str) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    target.mkdir(parents=True)
    (target / "partial.bin").write_bytes(b"x")
    identity = os.lstat(target)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    marker.write_text(record.replace('"dev": 0, "ino": 0', f'"dev": {identity.st_dev}, "ino": {identity.st_ino}'))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


def test_native_marker_recovery_rechecks_linux_support(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    staged = target / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    identity = os.lstat(target)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino}))  # legacy: native
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "new_install_without_linux_depot")


def test_malformed_unrelated_dlc_does_not_block_a_native_update(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    base = _tree(10, ("windows,linux", {11: "linux", 21: {"dlcappid": "20"}}))
    broken = {"appinfo": {"common": {"oslist": "windows"}, "depots": "oops"}}
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    body = b""
    for appid, tree in ((10, base), (20, broken)):
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_shared_depot_keeps_the_base_dlc_tag(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: "linux", 13: {"depotfromapp": "30", "dlcappid": "20"}}),
        20: ("windows", {13: "windows"}),
        30: ("windows,linux", {13: None}),
    })
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_shared_only_windows_evidence_still_needs_a_complete_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 12: None, 13: {"depotfromapp": "30"}}),
        30: ("windows", {13: "windows"}),
    })
    path = manifest(tmp_path, 10, {11: None})  # base depot 12 is missing
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")


def test_native_install_skips_unneeded_source_records(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    base = _tree(10, ("windows,linux", {11: "linux", 13: {"depotfromapp": "30", "oslist": "windows"}}))
    broken = {"appinfo": {"common": {"oslist": "windows"}, "depots": "oops"}}
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    body = b""
    for appid, tree in ((10, base), (30, broken)):
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


@pytest.mark.parametrize("record", ["null", '{"platform": "windows"}', '{"dev": "1", "ino": 2}'])
def test_malformed_marker_shape_is_damage(tmp_path: Path, record: str) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {12: None})  # the client now records native depots
    game = tmp_path / "steamapps" / "common" / "Game"
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    marker.write_text(record)
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


@pytest.mark.parametrize("extra", [{"optional": "1"}, {"manifests": None}])
def test_depots_a_default_install_never_receives_do_not_block_it(tmp_path: Path, extra) -> None:
    # e.g. an optional high-resolution pack, or a depot not yet released.
    write_appinfo(tmp_path, {10: ("windows", {11: None, 12: extra})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("windows", "platform_neutral_depots_windows_app")


def test_duplicates_in_sections_the_classifier_never_reads_are_tolerated(tmp_path: Path) -> None:
    import struct

    def key(name: str) -> bytes:
        return name.encode() + b"\0"

    def string(name: str, value: str) -> bytes:
        return b"\x01" + key(name) + key(value)

    registry = b"\x00" + key("install") + b"\x00" + key("registry") + string("a", "1") + string("a", "2") + b"\x08\x08"
    common = b"\x00" + key("common") + string("oslist", "linux") + b"\x08"
    depots = (b"\x00" + key("depots") + b"\x00" + key("11") + b"\x00" + key("config")
              + string("oslist", "linux") + b"\x08\x08\x08")
    kv = b"\x00" + key("appinfo") + common + depots + registry + b"\x08\x08"
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_unreleased_definition_of_a_reference_is_not_required(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("linux", {13: {"oslist": "linux", "manifests": None}}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "platform_neutral_depots")


def test_native_retry_proceeds_over_empty_leftovers(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    temp = target / "steamapps" / "temp"
    temp.mkdir(parents=True)
    (temp / "placeholder").write_bytes(b"")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "linux"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("linux", "owned_content_platform_recorded")


def test_marker_binds_by_inode_after_a_rename(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import marker_binds

    old = tmp_path / "Old"
    old.mkdir()
    identity = os.lstat(old)
    marker = tmp_path / "10.json"
    marker.write_text(json.dumps({"install_dir_name": "Old", "dev": identity.st_dev, "ino": identity.st_ino}))
    renamed = tmp_path / "New"
    old.rename(renamed)
    assert marker_binds(marker, renamed)
    marker.write_text("{broken")
    assert marker_binds(marker, renamed)  # damage is kept, never discarded
    assert not marker_binds(tmp_path / "missing.json", renamed)


def test_deeply_nested_marker_is_damage(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import marker_binds

    target = tmp_path / "Game"
    target.mkdir()
    marker = tmp_path / "10.json"
    marker.write_text("[" * 100000)
    assert marker_binds(marker, target)  # kept, never trusted


def test_record_without_appinfo_wrapper_is_unreadable(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode
    import struct

    tree = {"common": {"oslist": "linux"}, "depots": {"11": {"config": {"oslist": "linux"}}}}
    kv = _encode(tree, None)
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_inaccessible_library_alias_leaves_ownership_unknown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "games").symlink_to(library, target_is_directory=True)
    _client(native, {10: ("linux", {11: "linux"})}, [native, library])
    _client(flatpak, {10: ("windows", {11: "windows"})}, [flatpak, locked / "games"])
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK | os.X_OK):
            pytest.skip("permissions are not enforced for this user")
        # The Flatpak client may list this library through the locked alias.
        assert library_content_platform(library, 10, home=home) == (
            PlatformDecision(None, "app_metadata_unavailable")
        )
    finally:
        locked.chmod(0o755)


def test_windows_decision_carries_shared_depot_sources(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"}), 30: ("windows", {13: "windows"})})
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path).dlc_apps == (30,)


def test_one_installed_architecture_satisfies_the_architecture_set(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {
        11: None,
        12: {"oslist": "linux", "osarch": "32"},
        13: {"oslist": "linux", "osarch": "64"},
    })})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")
    manifest(tmp_path, 10, {11: None, 13: None})
    assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")


def test_intake_refuses_a_backslash_installdir_like_execution(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace('"Game"', '"Ga\\\\me"'))
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_architecture_set_uses_effective_definitions(tmp_path: Path) -> None:
    # The 64-bit depot declares osarch only in its source; the installed
    # shared copy satisfies the set, and the missing 32-bit one is fine.
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 12: {"oslist": "linux", "osarch": "32"}, 13: {"depotfromapp": "30"}}),
        30: ("linux", {13: {"oslist": "linux", "osarch": "64"}}),
    })
    path = manifest(tmp_path, 10, {11: None})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    assert decide(tmp_path).platform == "linux"


def test_installed_optional_architecture_depot_does_not_satisfy_the_set(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {
        11: None,
        12: {"oslist": "linux", "osarch": "64"},
        13: {"osarch": "32", "optional": "1"},  # shared, optional, installed
    })})
    manifest(tmp_path, 10, {11: None, 13: None})
    assert decide(tmp_path).platform is None


def test_linked_bookkeeping_directory_is_refused_before_any_write(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None})
    game = tmp_path / "steamapps" / "common" / "Game"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (game / "steamapps").symlink_to(elsewhere, target_is_directory=True)
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_unreadable")


def test_windows_marker_resume_needs_installed_or_staged_bytes(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    target = tmp_path / "steamapps" / "common" / "app_10"
    bookkeeping = target / "steamapps"
    bookkeeping.mkdir(parents=True)
    (bookkeeping / "libraryfolder.vdf").write_bytes(b"x")  # leftover bookkeeping only
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(target)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=target, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


def test_marker_for_another_populated_directory_is_not_overwritten(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    old = tmp_path / "steamapps" / "common" / "OldGame"
    old.mkdir(parents=True)
    (old / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(old)
    marker.write_text(json.dumps({
        "install_dir_name": "OldGame", "dev": identity.st_dev, "ino": identity.st_ino,
        "platform": "windows",
    }))
    manifest(tmp_path, 10, {12: None})  # the client now points at common/Game
    assert library_content_platform(
        tmp_path, 10, target=tmp_path / "steamapps" / "common" / "Game",
        owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_elsewhere")
    (old / "partial.bin").unlink()  # once that directory is empty, it no longer blocks
    assert library_content_platform(
        tmp_path, 10, target=tmp_path / "steamapps" / "common" / "Game",
        owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("linux", "installed_linux_depots")


def test_an_architecture_group_must_be_complete(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {
        11: None,
        12: {"osarch": "64"},                      # shared 64-bit content
        13: {"oslist": "linux", "osarch": "64"},   # Linux-only 64-bit binaries
    })})
    manifest(tmp_path, 10, {11: None, 12: None})  # a Proton install
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")
    manifest(tmp_path, 10, {11: None, 12: None, 13: None})
    assert decide(tmp_path).platform == "linux"


def test_another_titles_staging_is_not_a_resumable_download(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows", {11: "windows"})})
    manifest(tmp_path, 10, {11: None}, installed_files=False)
    game = tmp_path / "steamapps" / "common" / "Game"
    staged = game / "steamapps" / "downloading" / "999"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "new_install_without_linux_depot")


def test_english_language_depots_are_part_of_the_default_install(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {
        11: None,
        12: {"oslist": "linux", "language": "english"},
        13: {"oslist": "linux", "language": "german"},
    })})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "platform_neutral_install_ambiguous")
    manifest(tmp_path, 10, {11: None, 12: None})
    assert decide(tmp_path).platform == "linux"


@pytest.mark.parametrize("linked", ["downloading", "appid"])
def test_staging_through_a_linked_component_is_not_local(tmp_path: Path, linked: str) -> None:
    from steam_agent.execution.content_platform import has_staged_download

    target = tmp_path / "Game"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "10").mkdir(parents=True)
    (elsewhere / "10" / "chunk").write_bytes(b"x")
    (target / "steamapps").mkdir(parents=True)
    if linked == "downloading":
        (target / "steamapps" / "downloading").symlink_to(elsewhere, target_is_directory=True)
    else:
        (target / "steamapps" / "downloading").mkdir()
        (target / "steamapps" / "downloading" / "10").symlink_to(elsewhere / "10", target_is_directory=True)
    assert not has_staged_download(target, 10)


def test_corrupt_key_bytes_fail_closed(tmp_path: Path) -> None:
    import struct

    def key(name: bytes) -> bytes:
        return name + b"\0"

    config = b"\x00" + key(b"config") + b"\x01" + key(b"os\xfflist") + key(b"windows") + b"\x08"
    depots = b"\x00" + key(b"depots") + b"\x00" + key(b"11") + config + b"\x08\x08"
    common = b"\x00" + key(b"common") + b"\x01" + key(b"oslist") + key(b"windows,linux") + b"\x08"
    kv = b"\x00" + key(b"appinfo") + common + depots + b"\x08\x08"
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")


def test_conflicting_architectures_between_reference_and_definition_refuse(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30", "osarch": "32"}}),
        30: ("linux", {13: {"oslist": "linux", "osarch": "64"}}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "installed_depots_mixed_platforms")


def test_inherited_language_restriction_is_not_required(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: None, 13: {"depotfromapp": "30"}}),
        30: ("linux", {13: {"oslist": "linux", "language": "german"}}),
    })
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("linux", "platform_neutral_depots")


def test_former_directory_with_only_a_manifest_does_not_block(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    old = tmp_path / "steamapps" / "common" / "OldGame"
    manifest(old, 10, {11: None}, installed_files=False)  # only SteamCMD's manifest
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(old)
    marker.write_text(json.dumps({
        "install_dir_name": "OldGame", "dev": identity.st_dev, "ino": identity.st_ino,
        "platform": "windows",
    }))
    manifest(tmp_path, 10, {12: None})
    assert library_content_platform(
        tmp_path, 10, target=tmp_path / "steamapps" / "common" / "Game",
        owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("linux", "installed_linux_depots")


def test_inaccessible_entry_before_a_definite_match_still_proves_ownership(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = tmp_path / "games"
    manifest(library, 10, {11: None})
    root = home / ".local" / "share" / "Steam"
    locked = tmp_path / "locked"
    (locked / "other").mkdir(parents=True)
    _client(root, {10: ("linux", {11: "linux"})}, [root, locked / "other", library])
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK | os.X_OK):
            pytest.skip("permissions are not enforced for this user")
        assert library_content_platform(library, 10, home=home) == (
            PlatformDecision("linux", "installed_linux_depots")
        )
    finally:
        locked.chmod(0o755)


def test_damaged_marker_identity_for_a_populated_directory_refuses(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    old = tmp_path / "steamapps" / "common" / "OldGame"
    old.mkdir(parents=True)
    (old / "partial.bin").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"install_dir_name": "OldGame", "platform": "windows"}))
    manifest(tmp_path, 10, {12: None})
    assert library_content_platform(
        tmp_path, 10, target=tmp_path / "steamapps" / "common" / "Game",
        owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "owned_content_platform_unknown")


@pytest.mark.parametrize("restriction", [
    {"realm": "steamchina"}, {"steamdeck": "1"}, {"lowviolence": "1"}, {"highqualityaudio": "1"},
])
def test_depots_outside_a_global_desktop_install_are_not_required(tmp_path: Path, restriction) -> None:
    # e.g. Wallpaper Engine's Steam China depot, or a low-violence build.
    write_appinfo(tmp_path, {10: ("windows", {11: None, 12: restriction})})
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision("windows", "platform_neutral_depots_windows_app")


def test_public_only_dlc_stub_counts_as_missing_metadata(tmp_path: Path) -> None:
    from support.appinfo_fixture import _encode, _tree
    import struct

    base = _tree(10, ("windows", {11: "windows"}))
    base["appinfo"]["extended"] = {"listofdlc": "20"}
    stub = {"appinfo": {"appid": 20, "public_only": 1}}
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    body = b""
    for appid, tree in ((10, base), (20, stub)):
        kv = _encode(tree, None)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + body + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "dlc_metadata_not_cached")


def test_marker_recovery_gates_current_shared_depot_sources(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows", {11: "windows"}),
        30: ("windows", {13: {"oslist": "windows", "osarch": "64"}}),
    })
    path = manifest(tmp_path, 10, {})
    path.write_text(path.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    game = tmp_path / "steamapps" / "common" / "Game"
    staged = game / "steamapps" / "downloading" / "10"
    staged.mkdir(parents=True)
    (staged / "chunk").write_bytes(b"x")
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(game)
    marker.write_text(json.dumps({"dev": identity.st_dev, "ino": identity.st_ino, "platform": "windows"}))
    assert library_content_platform(
        tmp_path, 10, target=game, owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_marker_for_an_emptied_directory_allows_a_new_target(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    old = tmp_path / "steamapps" / "common" / "OldGame"
    old.mkdir(parents=True)
    marker = tmp_path / "owned" / "10.json"
    marker.parent.mkdir()
    identity = os.lstat(old)
    marker.write_text(json.dumps({
        "install_dir_name": "OldGame", "dev": identity.st_dev, "ino": identity.st_ino,
        "platform": "windows",
    }))
    # A new install into a directory that does not exist yet.
    assert library_content_platform(
        tmp_path, 10, target=tmp_path / "steamapps" / "common" / "app_10",
        owned_marker=marker, home=tmp_path / "no-home",
    ) == PlatformDecision("linux", "native_linux_available")


def test_quoted_double_slash_does_not_hide_a_backslash_installdir(tmp_path: Path) -> None:
    from steam_agent.execution.content_platform import manifest_install_dir

    path = tmp_path / "appmanifest_10.acf"
    path.write_text(
        '"AppState" { "appid" "10" "homepage" "https://example.invalid" '
        '"installdir" "Foo\\\\Bar" }\n'
    )
    assert manifest_install_dir(path) == "Foo\\\\Bar"


def test_queued_manifest_with_newer_nested_windows_gates_client_shared_sources(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {
        10: ("windows,linux", {11: "windows", 12: "linux"}),
        30: ("windows", {13: {"oslist": "windows", "osarch": "64"}}),
    })
    client = manifest(tmp_path, 10, {})
    client.write_text(client.read_text().replace(
        "\n}\n", '\n\t"SharedDepots"\n\t{\n\t\t"13"\t\t"30"\n\t}\n}\n',
    ))
    game = tmp_path / "steamapps" / "common" / "Game"
    nested = manifest(game, 10, {11: None}, installed_files=False)
    (game / "game.exe").write_bytes(b"MZ")
    os.utime(client, ns=(1_000_000_000, 1_000_000_000))
    os.utime(nested, ns=(2_000_000_000, 2_000_000_000))
    assert decide(tmp_path) == PlatformDecision(None, "windows_depot_architecture_unqualified")


def test_client_decision_is_not_blocked_by_an_unreadable_subdirectory(tmp_path: Path) -> None:
    write_appinfo(tmp_path, {10: ("windows,linux", {11: "windows", 12: "linux"})})
    manifest(tmp_path, 10, {12: None})
    game = tmp_path / "steamapps" / "common" / "Game"
    manifest(game, 10, {12: None}, installed_files=False)
    locked = game / "aaa-locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert decide(tmp_path) == PlatformDecision("linux", "installed_linux_depots")
    finally:
        locked.chmod(0o755)


def test_color_typed_flag_is_not_an_integer(tmp_path: Path) -> None:
    import struct

    def key(name: str) -> bytes:
        return name.encode() + b"\0"

    common = b"\x00" + key("common") + b"\x01" + key("oslist") + key("windows,linux") + b"\x08"
    optional_color = b"\x06" + key("optional") + struct.pack("<i", 1)
    manifests = b"\x00" + key("manifests") + b"\x00" + key("public") + b"\x01" + key("gid") + key("1") + b"\x08\x08"
    depots = (b"\x00" + key("depots")
              + b"\x00" + key("11") + manifests + b"\x08"
              + b"\x00" + key("12") + optional_color + manifests + b"\x08" + b"\x08")
    kv = b"\x00" + key("appinfo") + common + depots + b"\x08\x08"
    header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").write_bytes(
        struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10, len(header) + len(kv))
        + header + kv + struct.pack("<I", 0)
    )
    manifest(tmp_path, 10, {11: None})
    assert decide(tmp_path) == PlatformDecision(None, "app_metadata_unreadable")
