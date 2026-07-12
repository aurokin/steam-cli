from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from steam_agent.groups import MemberRef
from steam_agent.storage import (
    GROUP_PROFILE_DISCLOSURE_VERSION,
    AccountConflict,
    Storage,
    StorageError,
)


T0 = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 12, 13, tzinfo=timezone.utc)
PRIMARY = MemberRef("account", "primary")
GUEST = MemberRef("synthetic", "guest")
OWNER = MemberRef("synthetic", "owner")


def account(storage: Storage, alias: str = "primary"):
    return storage.configure_steam_account(
        alias=alias,
        steam_id64="76561198000000000",
        configured_at=T0,
    )


def synthetic(storage: Storage, alias: str = "guest"):
    return storage.create_synthetic_group_profile(
        alias,
        disclosure_version=GROUP_PROFILE_DISCLOSURE_VERSION,
        backups_acknowledged=True,
        created_at=T0,
    )


def acknowledge(storage: Storage, ref: MemberRef = PRIMARY):
    return storage.acknowledge_group_profile_storage(
        ref,
        disclosure_version=GROUP_PROFILE_DISCLOSURE_VERSION,
        backups_acknowledged=True,
        accepted_at=T0,
    )


def test_synthetic_creation_requires_current_disclosure_and_backup_ack(
    tmp_path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        with pytest.raises(ValueError, match="version"):
            storage.create_synthetic_group_profile(
                "guest",
                disclosure_version="old",
                backups_acknowledged=True,
                created_at=T0,
            )
        with pytest.raises(ValueError, match="backup"):
            storage.create_synthetic_group_profile(
                "guest",
                disclosure_version=GROUP_PROFILE_DISCLOSURE_VERSION,
                backups_acknowledged=False,
                created_at=T0,
            )
        assert storage.list_synthetic_group_profiles() == ()


def test_synthetic_profile_crud_is_case_insensitive_and_identifier_safe(
    tmp_path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        created = synthetic(storage, "Guest_User")
        repeated = storage.create_synthetic_group_profile(
            "guest_user",
            disclosure_version=GROUP_PROFILE_DISCLOSURE_VERSION,
            backups_acknowledged=True,
            created_at=T1,
        )
        assert created == repeated
        assert created.ref == MemberRef("synthetic", "GUEST_USER")
        assert created.disclosure_version == GROUP_PROFILE_DISCLOSURE_VERSION
        assert created.backups_acknowledged is True
        assert storage.get_synthetic_group_profile("guest_user") == created
        assert storage.list_synthetic_group_profiles() == (created,)
        serialized = asdict(created)
        assert "provider" not in serialized
        assert "steam" not in str(serialized).casefold()

        deleted = storage.delete_synthetic_group_profile("GUEST_USER")
        assert deleted.profile_removed and deleted.consent_removed
        assert storage.delete_synthetic_group_profile("guest_user").profile_removed is False


def test_aliases_cannot_collide_across_account_and_synthetic_kinds(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account(storage)
        with pytest.raises(AccountConflict, match="configured account"):
            synthetic(storage, "PRIMARY")
        synthetic(storage, "guest")
        with pytest.raises(AccountConflict, match="synthetic profile"):
            storage.configure_steam_account(
                alias="GUEST",
                steam_id64="76561198000000001",
                configured_at=T1,
            )


def test_configured_account_ref_is_lazy_and_group_clear_preserves_account(
    tmp_path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        configured = account(storage)
        lazy = storage.get_group_profile(PRIMARY)
        assert lazy is not None and lazy.id is None
        assert lazy.disclosure_version is None

        persisted = acknowledge(storage)
        assert persisted.id is not None
        storage.set_group_app_assertion(
            PRIMARY,
            appid=10,
            fact="policy:user:voice_chat_ok",
            value="present",
            updated_at=T1,
        )
        cleared = storage.clear_account_group_data("PRIMARY")
        assert cleared.profile_removed and cleared.assertions_removed == 1
        assert storage.get_account("primary") == configured
        again = storage.get_group_profile(PRIMARY)
        assert again is not None and again.id is None
        assert storage.read_group_app_assertions(PRIMARY) == ()


def test_group_mutations_require_profile_disclosure(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account(storage)
        with pytest.raises(StorageError, match="disclosure"):
            storage.set_group_app_assertion(
                PRIMARY,
                appid=10,
                fact="players:max",
                value=4,
                updated_at=T0,
            )
        # The failed transaction does not leave an undisclosed lazy row behind.
        profile = storage.get_group_profile(PRIMARY)
        assert profile is not None and profile.id is None


def test_synthetic_ownership_set_read_clear_and_account_override_rejection(
    tmp_path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        synthetic(storage)
        assertion = storage.set_group_ownership(
            GUEST, appid=10, state="owned", updated_at=T0
        )
        assert storage.read_group_ownership(GUEST) == (assertion,)
        updated = storage.set_group_ownership(
            GUEST, appid=10, state="unknown", updated_at=T1
        )
        assert storage.read_group_ownership(GUEST, appid=10) == (updated,)
        assert storage.clear_group_ownership(GUEST, appid=10)
        assert not storage.clear_group_ownership(GUEST, appid=10)
        with pytest.raises(ValueError, match="Steam evidence"):
            storage.set_group_ownership(
                PRIMARY, appid=10, state="owned", updated_at=T0
            )
        with pytest.raises(ValueError, match="state"):
            storage.set_group_ownership(
                GUEST, appid=10, state="maybe", updated_at=T0
            )


def test_family_edges_require_explicit_distinct_source_and_are_typed(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        synthetic(storage, "guest")
        synthetic(storage, "owner")
        with pytest.raises(ValueError, match="distinct"):
            storage.set_group_family(
                GUEST,
                source=GUEST,
                appid=10,
                state="available",
                updated_at=T0,
            )
        edge = storage.set_group_family(
            GUEST,
            source=OWNER,
            appid=10,
            state="unknown",
            updated_at=T0,
        )
        assert storage.read_group_family(GUEST) == (edge,)
        assert storage.read_group_family(GUEST, appid=11) == ()
        assert storage.clear_group_family(GUEST, source=OWNER, appid=10)


def test_family_edge_requires_current_disclosure_from_source_profile(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        synthetic(storage)
        account(storage)
        with pytest.raises(StorageError, match="disclosure"):
            storage.set_group_family(
                GUEST,
                source=PRIMARY,
                appid=10,
                state="available",
                updated_at=T0,
            )
        assert storage.read_group_family(GUEST) == ()

        acknowledge(storage)
        edge = storage.set_group_family(
            GUEST,
            source=PRIMARY,
            appid=10,
            state="available",
            updated_at=T1,
        )
        assert storage.read_group_family(GUEST) == (edge,)


@pytest.mark.parametrize(
    ("fact", "value", "state", "stored"),
    (
        ("players:min", 2, "known", 2),
        ("players:max", "unknown", "unknown", None),
        ("trait:user:deck_builder", "present", "present", None),
        ("policy:user:voice_chat_ok", "absent", "absent", None),
    ),
)
def test_typed_app_assertions_round_trip(
    tmp_path, fact: str, value: int | str, state: str, stored: int | None
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        synthetic(storage)
        result = storage.set_group_app_assertion(
            GUEST, appid=10, fact=fact, value=value, updated_at=T0
        )
        assert result.fact == fact
        assert result.state == state
        assert result.value == stored
        assert storage.read_group_app_assertions(GUEST, appid=10) == (result,)
        assert storage.clear_group_app_assertion(GUEST, appid=10, fact=fact)
        assert storage.read_group_app_assertions(GUEST) == ()


@pytest.mark.parametrize(
    ("fact", "value"),
    (
        ("players:min", 0),
        ("players:max", "present"),
        ("trait:not-user", "present"),
        ("trait:user:ok", "known"),
        ("policy:user:ok", 3),
        ("mechanic:user:ok", "present"),
    ),
)
def test_app_assertions_reject_untyped_facts_and_values(
    tmp_path, fact: str, value: int | str
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        synthetic(storage)
        with pytest.raises(ValueError):
            storage.set_group_app_assertion(
                GUEST, appid=10, fact=fact, value=value, updated_at=T0
            )


def test_player_limit_assertions_reject_an_inverted_pair(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        synthetic(storage)
        storage.set_group_app_assertion(
            GUEST, appid=10, fact="players:min", value=4, updated_at=T0
        )
        with pytest.raises(ValueError, match="inverted"):
            storage.set_group_app_assertion(
                GUEST, appid=10, fact="players:max", value=2, updated_at=T1
            )
        assert [item.fact for item in storage.read_group_app_assertions(GUEST)] == [
            "players:min"
        ]


def test_synthetic_delete_cascades_assertions_and_relationship_edges(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        synthetic(storage, "guest")
        synthetic(storage, "owner")
        storage.set_group_ownership(GUEST, appid=10, state="owned", updated_at=T0)
        storage.set_group_family(
            GUEST,
            source=OWNER,
            appid=10,
            state="available",
            updated_at=T0,
        )
        storage.set_group_app_assertion(
            GUEST,
            appid=10,
            fact="trait:user:cozy",
            value="present",
            updated_at=T0,
        )
        deleted = storage.delete_synthetic_group_profile("guest")
        assert deleted.ownership_removed == 1
        assert deleted.family_removed == 1
        assert deleted.assertions_removed == 1
        assert storage.read_group_family(OWNER) == ()
        counts = storage._connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM group_ownership_current),
                 (SELECT COUNT(*) FROM group_family_current),
                 (SELECT COUNT(*) FROM group_app_assertion_current)"""
        ).fetchone()
        assert tuple(counts) == (0, 0, 0)


def test_account_deletion_removes_only_its_group_row_and_edges(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        configured = account(storage)
        acknowledge(storage)
        synthetic(storage, "guest")
        storage.set_group_ownership(GUEST, appid=10, state="owned", updated_at=T0)
        storage.set_group_family(
            PRIMARY,
            source=GUEST,
            appid=10,
            state="available",
            updated_at=T0,
        )
        storage.set_group_app_assertion(
            GUEST,
            appid=10,
            fact="players:max",
            value=4,
            updated_at=T0,
        )
        storage.set_group_app_assertion(
            PRIMARY,
            appid=20,
            fact="policy:user:voice_chat_ok",
            value="present",
            updated_at=T0,
        )

        storage.delete_steam_account_data(configured.id)
        assert storage.get_group_profile(PRIMARY) is None
        assert storage.get_group_profile(GUEST) is not None
        assert storage.read_group_ownership(GUEST)[0].state == "owned"
        assert storage.read_group_app_assertions(GUEST)[0].value == 4
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM group_family_current"
        ).fetchone()[0] == 0
        assert storage._connection.execute(
            "SELECT 1 FROM steam_apps WHERE appid = 20"
        ).fetchone() is None


def test_all_account_deletion_preserves_unrelated_synthetic_facts(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account(storage)
        acknowledge(storage)
        synthetic(storage, "guest")
        storage.set_group_family(
            GUEST,
            source=PRIMARY,
            appid=10,
            state="unknown",
            updated_at=T0,
        )
        stored = storage.set_group_app_assertion(
            GUEST,
            appid=10,
            fact="trait:user:cozy",
            value="present",
            updated_at=T0,
        )

        storage.delete_all_steam_account_data()
        assert storage.get_group_profile(PRIMARY) is None
        assert storage.get_group_profile(GUEST) is not None
        assert storage.read_group_family(GUEST) == ()
        assert storage.read_group_app_assertions(GUEST) == (stored,)


def test_secure_delete_is_enabled_and_no_derived_group_results_are_persisted(
    tmp_path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        assert storage._connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
        tables = {
            row[0]
            for row in storage._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "group_profiles",
            "group_profile_consents",
            "group_ownership_current",
            "group_family_current",
            "group_app_assertion_current",
        } <= tables
        assert not any("result" in name or "ranking" in name for name in tables if name.startswith("group_"))
