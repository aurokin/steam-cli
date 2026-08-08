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
    )
    backup = journal_dir / "appmanifest_1902490.acf.backup"
    assert backup.read_text(encoding="utf-8") == "old"


def test_reconcile_completed_swap_clears_journal(tmp_path: Path) -> None:
    library = _library(tmp_path)
    target = _target_with_manifest(tmp_path)
    journal_dir = tmp_path / "journal"
    adopt_manifest(
        source=locate_manifest(install_dir=target, appid=1902490),
        library=library,
        appid=1902490,
        install_dir_name="Desk Job",
        journal_dir=journal_dir,
    )
    assert (
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
        == "completed"
    )
    assert not (journal_dir / "adoption-1902490.json").exists()


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
    )
    adopted.write_text('"AppState" { torn', encoding="utf-8")  # simulate crash

    assert (
        reconcile_adoption(library=library, appid=1902490, journal_dir=journal_dir)
        == "restored"
    )
    assert prior.read_text(encoding="utf-8") == "old"


def test_reconcile_without_journal_is_clean(tmp_path: Path) -> None:
    library = _library(tmp_path)
    assert (
        reconcile_adoption(
            library=library, appid=42, journal_dir=tmp_path / "journal"
        )
        == "clean"
    )
