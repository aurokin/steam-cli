from __future__ import annotations

import os
from pathlib import Path

import pytest

from steam_agent.local_accounts import (
    MAX_LOGINUSERS_BYTES,
    AmbiguousLocalAccounts,
    LocalAccountRegistryUnavailable,
    MalformedLocalAccountRegistry,
    NoLocalAccount,
    discover_local_accounts,
    select_primary_local_account,
    validate_steam_id64,
)


SID0 = "76561197960265728"
SID1 = "76561197960265729"


def write_registry(root: Path, users_body: str) -> Path:
    config = root / "config"
    config.mkdir(parents=True)
    registry = config / "loginusers.vdf"
    registry.write_text(f'"users"\n{{\n{users_body}\n}}\n', encoding="utf-8")
    return registry


def record(steam_id: str, most_recent: str | None, *, extra: str = "") -> str:
    recent = "" if most_recent is None else f'"MostRecent" "{most_recent}"'
    return f'''"{steam_id}"
    {{
        "AccountName" "must-not-cross-boundary"
        "PersonaName" "also-private"
        "RememberPassword" "1"
        {recent}
        {extra}
    }}'''


def test_discovery_allowlists_identifier_and_most_recent(tmp_path: Path) -> None:
    write_registry(tmp_path, record(SID0, "1"))

    discovery = discover_local_accounts(tmp_path)

    assert len(discovery.candidates) == 1
    assert discovery.candidates[0].steam_id64 == SID0
    assert discovery.candidates[0].most_recent is True
    assert not hasattr(discovery.candidates[0], "account_name")
    rendered = repr(discovery)
    assert SID0 not in rendered
    assert "must-not-cross-boundary" not in rendered


def test_discovery_keys_are_case_insensitive_and_unknown_fields_are_ignored(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "loginusers.vdf").write_text(
        f'''"UsErS"
        {{
            "{SID0}"
            {{
                "mOsTrEcEnT" "1"
                "FutureField" "ignored"
            }}
        }}''',
        encoding="utf-8",
    )

    candidate = select_primary_local_account(discover_local_accounts(tmp_path))
    assert candidate.steam_id64 == SID0
    assert candidate.most_recent is True


def test_unique_most_recent_wins_and_results_are_deterministic(tmp_path: Path) -> None:
    write_registry(
        tmp_path,
        record(SID1, "1") + "\n" + record(SID0, "0"),
    )

    discovery = discover_local_accounts(tmp_path)
    assert [item.steam_id64 for item in discovery.candidates] == [SID0, SID1]
    assert select_primary_local_account(discovery).steam_id64 == SID1


def test_sole_candidate_is_selected_without_most_recent(tmp_path: Path) -> None:
    write_registry(tmp_path, record(SID0, None))
    assert (
        select_primary_local_account(discover_local_accounts(tmp_path)).steam_id64
        == SID0
    )


@pytest.mark.parametrize(
    "body",
    [
        record(SID0, "0") + "\n" + record(SID1, "0"),
        record(SID0, "1") + "\n" + record(SID1, "1"),
    ],
)
def test_multiple_accounts_without_unique_recent_are_typed_ambiguous(
    tmp_path: Path, body: str
) -> None:
    write_registry(tmp_path, body)
    with pytest.raises(AmbiguousLocalAccounts) as captured:
        select_primary_local_account(discover_local_accounts(tmp_path))
    assert SID0 not in str(captured.value)
    assert SID1 not in str(captured.value)


def test_empty_registry_has_typed_no_account(tmp_path: Path) -> None:
    write_registry(tmp_path, "")
    with pytest.raises(NoLocalAccount):
        select_primary_local_account(discover_local_accounts(tmp_path))


@pytest.mark.parametrize(
    "value",
    ["", "abc", "-1", "0", "18446744073709551616", "１２３"],
)
def test_steam_id64_validation_rejects_non_uint64_values(value: str) -> None:
    with pytest.raises(ValueError, match="SteamID64"):
        validate_steam_id64(value)


def test_steam_id64_validation_canonicalizes_decimal_value() -> None:
    assert validate_steam_id64("0001") == "1"
    assert validate_steam_id64("18446744073709551615") == "18446744073709551615"


@pytest.mark.parametrize(
    "payload",
    [
        '"not-users" {}',
        '"users" { "not-an-id" { "MostRecent" "1" } }',
        '"users" { "1" { "MostRecent" "1" "mostrecent" "0" } }',
        '"users" {} "USERS" {}',
        '"users" { "1" {} "01" {} }',
        f'"users" {{ "{SID0}" "not-an-object" }}',
        f'"users" {{ "{SID0}" {{ "MostRecent" "yes" }} }}',
        '"users" {',
    ],
)
def test_malformed_registry_is_typed_and_does_not_echo_payload(
    tmp_path: Path, payload: str
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "loginusers.vdf").write_text(payload, encoding="utf-8")

    with pytest.raises(MalformedLocalAccountRegistry) as captured:
        discover_local_accounts(tmp_path)
    assert "not-an-id" not in str(captured.value)


def test_missing_registry_is_typed_unavailable(tmp_path: Path) -> None:
    with pytest.raises(LocalAccountRegistryUnavailable, match="not found"):
        discover_local_accounts(tmp_path)


def test_oversized_registry_is_rejected_before_parsing(tmp_path: Path) -> None:
    registry = write_registry(tmp_path, "")
    registry.write_bytes(b" " * (MAX_LOGINUSERS_BYTES + 1))
    with pytest.raises(MalformedLocalAccountRegistry, match="size"):
        discover_local_accounts(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is privileged on Windows")
def test_registry_symlink_is_not_followed(tmp_path: Path) -> None:
    target_root = tmp_path / "target"
    target = write_registry(target_root, record(SID0, "1"))
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "loginusers.vdf").symlink_to(target)

    with pytest.raises(LocalAccountRegistryUnavailable, match="symbolic link"):
        discover_local_accounts(root)


def test_registry_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "config" / "loginusers.vdf").mkdir(parents=True)
    with pytest.raises(LocalAccountRegistryUnavailable, match="regular file"):
        discover_local_accounts(tmp_path)
