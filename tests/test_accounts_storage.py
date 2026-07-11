from __future__ import annotations

from importlib import resources
from pathlib import Path
import sqlite3

import pytest

from steam_agent.storage import AccountConflict, Storage


SID0 = "76561197960265728"
SID1 = "76561197960265729"
T0 = "2026-07-10T12:00:00Z"
T1 = "2026-07-10T12:01:00Z"


def test_account_migration_is_packaged_and_applied(tmp_path: Path) -> None:
    migration = resources.files("steam_agent").joinpath(
        "migrations", "002_accounts.sql"
    )
    assert migration.is_file()
    assert "CREATE TABLE accounts" in migration.read_text(encoding="utf-8")

    path = tmp_path / "accounts.sqlite3"
    with Storage(path):
        pass
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,),
            (11,), (12,), (13,), (14,), (15,)
        ]


def test_configure_get_list_and_remove_multiple_accounts(tmp_path: Path) -> None:
    with Storage(tmp_path / "accounts.sqlite3") as storage:
        primary = storage.configure_steam_account(
            alias="primary", steam_id64=SID0, configured_at=T0
        )
        secondary = storage.configure_steam_account(
            alias="secondary", steam_id64=SID1, configured_at=T0
        )

        assert primary.alias == "primary"
        assert primary.provider == "steam"
        assert primary.provider_account_id == SID0
        assert primary.source_kind == "local_steam_login_registry"
        assert SID0 not in repr(primary)
        assert storage.get_account("PRIMARY") == primary
        assert storage.list_accounts() == [primary, secondary]
        assert storage.remove_account("primary") is True
        assert storage.remove_account("primary") is False
        assert storage.get_account("primary") is None
        assert storage.list_accounts() == [secondary]


def test_configure_is_idempotent_and_preserves_creation_time(tmp_path: Path) -> None:
    with Storage(tmp_path / "accounts.sqlite3") as storage:
        first = storage.configure_steam_account(
            alias="primary", steam_id64=SID0, configured_at=T0
        )
        second = storage.configure_steam_account(
            alias="PRIMARY",
            steam_id64=SID0,
            configured_at=T1,
            source_kind="explicit_steam_id",
        )

        assert second.id == first.id
        assert second.alias == "primary"
        assert second.created_at == first.created_at == T0
        assert second.updated_at == T1
        assert second.source_kind == "explicit_steam_id"


def test_alias_cannot_be_rebound_to_another_identity(tmp_path: Path) -> None:
    with Storage(tmp_path / "accounts.sqlite3") as storage:
        storage.configure_steam_account(
            alias="primary", steam_id64=SID0, configured_at=T0
        )
        with pytest.raises(AccountConflict, match="alias") as captured:
            storage.configure_steam_account(
                alias="primary", steam_id64=SID1, configured_at=T1
            )
        assert SID0 not in str(captured.value)
        assert SID1 not in str(captured.value)
        assert storage.get_account("primary").provider_account_id == SID0  # type: ignore[union-attr]


def test_identity_cannot_be_implicitly_renamed(tmp_path: Path) -> None:
    with Storage(tmp_path / "accounts.sqlite3") as storage:
        storage.configure_steam_account(
            alias="primary", steam_id64=SID0, configured_at=T0
        )
        with pytest.raises(AccountConflict, match="another alias"):
            storage.configure_steam_account(
                alias="renamed", steam_id64=SID0, configured_at=T1
            )
        assert [account.alias for account in storage.list_accounts()] == ["primary"]


@pytest.mark.parametrize(
    "alias",
    ["", "1primary", "has space", "ümlaut", "a" * 65],
)
def test_account_alias_validation(alias: str, tmp_path: Path) -> None:
    with Storage(tmp_path / "accounts.sqlite3") as storage:
        with pytest.raises(ValueError, match="alias"):
            storage.configure_steam_account(
                alias=alias, steam_id64=SID0, configured_at=T0
            )


def test_account_identifier_and_timestamp_validation(tmp_path: Path) -> None:
    with Storage(tmp_path / "accounts.sqlite3") as storage:
        with pytest.raises(ValueError, match="SteamID64"):
            storage.configure_steam_account(
                alias="primary", steam_id64="secret-name", configured_at=T0
            )
        with pytest.raises(ValueError, match="timezone"):
            storage.configure_steam_account(
                alias="primary",
                steam_id64=SID0,
                configured_at="2026-07-10T12:00:00",
            )
