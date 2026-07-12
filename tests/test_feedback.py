from datetime import datetime, timezone
import sqlite3

import pytest

from steam_agent.feedback import FeedbackService, validate_trait
from steam_agent.storage import Storage


NOW = datetime(2026, 7, 12, 4, 0, tzinfo=timezone.utc)


def account(storage: Storage, alias: str = "primary", suffix: int = 0) -> int:
    return storage.configure_steam_account(
        alias=alias,
        steam_id64=str(76561198000000000 + suffix),
        configured_at=NOW,
        source_kind="test",
    ).id


def test_feedback_projection_audit_idempotence_and_semantics(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = account(storage)
        service = FeedbackService(storage, clock=lambda: NOW)
        rating_change = service.rate(account_id, 10, "liked")
        assert rating_change.changed and rating_change.event_id is not None
        repeated = service.rate(account_id, 10, "liked")
        assert not repeated.changed and repeated.event_id is None
        service.play_state(account_id, 10, "user_abandoned")
        service.snooze(account_id, 10, until="2026-07-13T04:00:00Z", clear=False)
        service.estimate(
            account_id,
            10,
            minimum_session_minutes=30,
            remaining_minutes=120,
            clear_minimum_session_minutes=False,
            clear_remaining_minutes=False,
        )
        trait_change = service.trait(account_id, 10, "user:crafting", "absent")
        assert trait_change.changed and trait_change.event_id is not None
        repeated_trait = service.trait(account_id, 10, "user:crafting", "absent")
        assert not repeated_trait.changed and repeated_trait.event_id is None

        item = service.query(account_id)[0]
        assert item["game_id"].startswith("game:")
        assert item["rating"] == "liked"
        assert item["play_state"] == "user_abandoned"
        assert item["snooze"] == {
            "until": "2026-07-13T04:00:00Z",
            "state": "active",
        }
        assert item["estimates"]["remaining_minutes"] == 120
        assert item["traits"][0]["trait"] == "user:crafting"
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM explicit_feedback_events"
        ).fetchone()[0] == 6


def test_clear_fields_and_expired_snooze(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = account(storage)
        service = FeedbackService(storage, clock=lambda: NOW)
        service.snooze(account_id, 10, until="2026-07-11T04:00:00Z", clear=False)
        assert service.query(account_id)[0]["snooze"]["state"] == "expired"
        service.snooze(account_id, 10, until=None, clear=True)
        assert service.query(account_id) == ()


def test_cross_field_noop_never_claims_another_fields_event(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = account(storage)
        service = FeedbackService(storage, clock=lambda: NOW)
        rating = service.rate(account_id, 10, "liked")
        play_state = service.play_state(account_id, 10, "finished")

        repeated = service.rate(account_id, 10, "liked")

        assert rating.event_id != play_state.event_id
        assert repeated.changed is False
        assert repeated.event_id is None


def test_clear_all_scalar_fields_and_trait_prunes_current_rows(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = account(storage)
        service = FeedbackService(storage, clock=lambda: NOW)
        service.rate(account_id, 10, "neutral")
        service.play_state(account_id, 10, "active")
        service.trait(account_id, 10, "user:crafting", "unknown")

        assert service.rate(account_id, 10, None, clear=True).changed
        assert service.play_state(account_id, 10, None, clear=True).changed
        cleared_trait = service.trait(
            account_id, 10, "user:crafting", None, clear=True
        )
        repeated_trait = service.trait(
            account_id, 10, "user:crafting", None, clear=True
        )

        assert cleared_trait.changed and cleared_trait.event_id is not None
        assert not repeated_trait.changed and repeated_trait.event_id is None
        assert service.query(account_id) == ()
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM explicit_feedback_current"
        ).fetchone()[0] == 0
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM explicit_trait_current"
        ).fetchone()[0] == 0
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM explicit_feedback_events"
        ).fetchone()[0] == 6


def test_multi_estimate_change_is_atomic_when_second_insert_fails(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = account(storage)
        service = FeedbackService(storage, clock=lambda: NOW)
        storage._connection.execute(
            """
            CREATE TRIGGER fail_remaining_estimate
            BEFORE INSERT ON explicit_feedback_events
            WHEN NEW.event_kind = 'remaining_minutes'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic second change failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            service.estimate(
                account_id,
                10,
                minimum_session_minutes=30,
                remaining_minutes=90,
                clear_minimum_session_minutes=False,
                clear_remaining_minutes=False,
            )

        assert service.query(account_id) == ()
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM explicit_feedback_events"
        ).fetchone()[0] == 0


def test_both_estimates_clear_atomically_with_one_timestamp(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = account(storage)
        service = FeedbackService(storage, clock=lambda: NOW)
        service.estimate(
            account_id,
            10,
            minimum_session_minutes=30,
            remaining_minutes=90,
            clear_minimum_session_minutes=False,
            clear_remaining_minutes=False,
        )

        changes = service.estimate(
            account_id,
            10,
            minimum_session_minutes=None,
            remaining_minutes=None,
            clear_minimum_session_minutes=True,
            clear_remaining_minutes=True,
        )

        assert [change.field for change in changes] == [
            "minimum_session_minutes",
            "remaining_minutes",
        ]
        assert all(change.changed and change.event_id is not None for change in changes)
        assert service.query(account_id) == ()
        timestamps = {
            row[0]
            for row in storage._connection.execute(
                """
                SELECT recorded_at FROM explicit_feedback_events
                WHERE id IN (?, ?)
                """,
                tuple(change.event_id for change in changes),
            )
        }
        assert timestamps == {"2026-07-12T04:00:00Z"}


def test_preference_rules_are_sorted_idempotent_and_removable(tmp_path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account_id = account(storage)
        service = FeedbackService(storage, clock=lambda: NOW)
        event = service.set_rule(
            account_id,
            trait="user:relaxing",
            kind="prefer",
            strength="soft",
            weight=80,
        )
        assert service.set_rule(
            account_id,
            trait="user:relaxing",
            kind="prefer",
            strength="soft",
            weight=80,
        ) == event
        assert service.list_rules(account_id)[0]["weight"] == 80
        assert service.remove_rule(account_id, trait="user:relaxing")
        assert not service.remove_rule(account_id, trait="user:relaxing")
        assert service.list_rules(account_id) == ()
        assert storage._connection.execute(
            "SELECT COUNT(*) FROM preference_rule_events"
        ).fetchone()[0] == 2


def test_account_isolation_deletion_counts_and_price_deletion_preserves_feedback(
    tmp_path,
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        first = account(storage)
        second = account(storage, "other", 1)
        first_service = FeedbackService(storage, clock=lambda: NOW)
        first_service.rate(first, 10, "liked")
        first_service.trait(first, 10, "user:coop", "present")
        first_service.set_rule(
            first,
            trait="user:coop",
            kind="prefer",
            strength="soft",
            weight=100,
        )
        first_service.rate(second, 20, "disliked")

        storage.delete_price_data(provider="cheapshark", account_id=first)
        assert len(first_service.query(first)) == 1
        deletion = storage.delete_steam_account_data(first)
        assert deletion.feedback_events_removed == 2
        assert deletion.feedback_current_removed == 1
        assert deletion.feedback_traits_removed == 1
        assert deletion.preference_rule_events_removed == 1
        assert deletion.preference_rules_removed == 1
        assert len(first_service.query(second)) == 1


@pytest.mark.parametrize(
    "trait",
    ["crafting", "steam:crafting", "user:", "user:UPPER", "user:has space", "user:a/../b"],
)
def test_trait_namespace_is_closed(trait: str) -> None:
    with pytest.raises(ValueError):
        validate_trait(trait)
