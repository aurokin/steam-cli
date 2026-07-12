"""Validated local explicit-feedback application boundary."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import re
from typing import Callable

from steam_agent.storage import ExplicitFeedback, PreferenceRule, Storage


MAX_UNSIGNED_32 = (1 << 32) - 1
TRAIT_PATTERN = re.compile(r"user:[a-z0-9](?:[a-z0-9._-]{0,57}[a-z0-9])?\Z")
Clock = Callable[[], datetime]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def validate_appid(appid: int) -> int:
    if isinstance(appid, bool) or not 1 <= appid <= MAX_UNSIGNED_32:
        raise ValueError("appid is invalid")
    return appid


def validate_trait(trait: str) -> str:
    if not isinstance(trait, str) or TRAIT_PATTERN.fullmatch(trait) is None:
        raise ValueError("trait must be a bounded user: slug")
    return trait


def validate_minutes(value: int) -> int:
    if isinstance(value, bool) or not 0 <= value <= MAX_UNSIGNED_32:
        raise ValueError("minute estimate is invalid")
    return value


def parse_utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


class FeedbackService:
    def __init__(self, storage: Storage, *, clock: Clock = now_utc) -> None:
        self.storage = storage
        self.clock = clock

    def rate(self, account_id: int, appid: int, value: str) -> int:
        validate_appid(appid)
        if value not in {"liked", "disliked", "neutral"}:
            raise ValueError("rating is invalid")
        return self._set(account_id, appid, "rating", value)

    def play_state(self, account_id: int, appid: int, value: str) -> int:
        validate_appid(appid)
        if value not in {"finished", "user_abandoned", "active"}:
            raise ValueError("play state is invalid")
        return self._set(account_id, appid, "play_state", value)

    def snooze(
        self, account_id: int, appid: int, *, until: str | None, clear: bool
    ) -> int:
        validate_appid(appid)
        if clear == (until is not None):
            raise ValueError("provide exactly one of until or clear")
        normalized = None
        if until is not None:
            normalized = parse_utc_timestamp(until).isoformat().replace("+00:00", "Z")
        return self._set(account_id, appid, "snooze", normalized)

    def estimate(
        self,
        account_id: int,
        appid: int,
        *,
        minimum_session_minutes: int | None,
        remaining_minutes: int | None,
        clear_minimum_session_minutes: bool,
        clear_remaining_minutes: bool,
    ) -> tuple[int, ...]:
        validate_appid(appid)
        if minimum_session_minutes is not None:
            validate_minutes(minimum_session_minutes)
        if remaining_minutes is not None:
            validate_minutes(remaining_minutes)
        if minimum_session_minutes is not None and clear_minimum_session_minutes:
            raise ValueError("minimum session estimate cannot be set and cleared")
        if remaining_minutes is not None and clear_remaining_minutes:
            raise ValueError("remaining estimate cannot be set and cleared")
        changes = (
            minimum_session_minutes is not None,
            remaining_minutes is not None,
            clear_minimum_session_minutes,
            clear_remaining_minutes,
        )
        if not any(changes):
            raise ValueError("at least one estimate change is required")
        event_ids: list[int] = []
        if minimum_session_minutes is not None or clear_minimum_session_minutes:
            event_ids.append(
                self._set(
                    account_id,
                    appid,
                    "minimum_session_minutes",
                    minimum_session_minutes,
                )
            )
        if remaining_minutes is not None or clear_remaining_minutes:
            event_ids.append(
                self._set(account_id, appid, "remaining_minutes", remaining_minutes)
            )
        return tuple(event_ids)

    def trait(self, account_id: int, appid: int, trait: str, value: str) -> int:
        validate_appid(appid)
        validate_trait(trait)
        if value not in {"present", "absent", "unknown"}:
            raise ValueError("trait value is invalid")
        return self.storage.set_explicit_trait(
            account_id=account_id,
            appid=appid,
            trait=trait,
            value=value,
            recorded_at=self.clock(),
        )

    def query(
        self, account_id: int, *, appid: int | None = None
    ) -> tuple[dict[str, object], ...]:
        if appid is not None:
            validate_appid(appid)
        now = self.clock().astimezone(timezone.utc)
        return tuple(_feedback_dict(item, now=now) for item in self.storage.list_explicit_feedback(account_id, appid=appid))

    def set_rule(
        self,
        account_id: int,
        *,
        trait: str,
        kind: str,
        strength: str,
        weight: int,
    ) -> int:
        validate_trait(trait)
        if kind not in {"prefer", "avoid", "require"}:
            raise ValueError("preference rule kind is invalid")
        if strength not in {"soft", "hard"}:
            raise ValueError("preference rule strength is invalid")
        if isinstance(weight, bool) or not 0 <= weight <= 100:
            raise ValueError("preference rule weight is invalid")
        return self.storage.set_preference_rule(
            account_id=account_id,
            trait=trait,
            kind=kind,
            strength=strength,
            weight=weight,
            recorded_at=self.clock(),
        )

    def remove_rule(self, account_id: int, *, trait: str) -> bool:
        validate_trait(trait)
        return self.storage.remove_preference_rule(
            account_id=account_id, trait=trait, recorded_at=self.clock()
        )

    def list_rules(self, account_id: int) -> tuple[dict[str, object], ...]:
        return tuple(_rule_dict(item) for item in self.storage.list_preference_rules(account_id))

    def _set(
        self, account_id: int, appid: int, field_name: str, value: str | int | None
    ) -> int:
        return self.storage.set_explicit_feedback_field(
            account_id=account_id,
            appid=appid,
            field_name=field_name,
            value=value,
            recorded_at=self.clock(),
        )


def _feedback_dict(item: ExplicitFeedback, *, now: datetime) -> dict[str, object]:
    snooze_state: str | None = None
    if item.snoozed_until is not None:
        snooze_state = (
            "active" if parse_utc_timestamp(item.snoozed_until) > now else "expired"
        )
    return {
        "appid": item.appid,
        "game_id": item.game_id,
        "rating": item.rating,
        "play_state": item.play_state,
        "snooze": {"until": item.snoozed_until, "state": snooze_state},
        "estimates": {
            "minimum_session_minutes": item.minimum_session_minutes,
            "remaining_minutes": item.remaining_minutes,
            "source": "explicit_user",
        },
        "traits": [asdict(trait) for trait in item.traits],
        "updated_at": item.updated_at,
        "last_event_id": item.last_event_id,
        "source": "explicit_user",
    }


def _rule_dict(item: PreferenceRule) -> dict[str, object]:
    return {
        "trait": item.trait,
        "kind": item.kind,
        "strength": item.strength,
        "weight": item.weight,
        "updated_at": item.updated_at,
        "last_event_id": item.last_event_id,
        "source": "explicit_user",
    }


__all__ = ["FeedbackService", "parse_utc_timestamp", "validate_trait"]
