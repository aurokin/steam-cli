from datetime import datetime, timezone

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
        rating_event = service.rate(account_id, 10, "liked")
        assert service.rate(account_id, 10, "liked") == rating_event
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
        trait_event = service.trait(account_id, 10, "user:crafting", "absent")
        assert service.trait(account_id, 10, "user:crafting", "absent") == trait_event

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
