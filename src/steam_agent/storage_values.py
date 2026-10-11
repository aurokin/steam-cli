"""Pure normalization and validation helpers shared by storage projections."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: str | datetime) -> str:
    if isinstance(value, str):
        candidate = value
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: str | datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(
        value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _group_fact_key(fact: str) -> tuple[str, str]:
    if fact == "players:min":
        return "players_min", ""
    if fact == "players:max":
        return "players_max", ""
    for prefix, kind in (("trait:", "trait"), ("policy:", "policy")):
        if isinstance(fact, str) and fact.startswith(prefix):
            slug = fact[len(prefix) :]
            _user_trait(slug)
            return kind, slug
    raise ValueError("group app fact is invalid")


def _group_app_fact(fact: str, value: int | str) -> tuple[str, str, str, int | None]:
    kind, slug = _group_fact_key(fact)
    if kind in {"players_min", "players_max"}:
        if value == "unknown":
            return kind, slug, "unknown", None
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= (1 << 32) - 1
        ):
            raise ValueError("player-limit assertion is invalid")
        return kind, slug, "known", value
    if value not in {"present", "absent", "unknown"}:
        raise ValueError("trait/policy assertion state is invalid")
    return kind, slug, str(value), None


def _group_fact_name(kind: str, slug: str) -> str:
    return {
        "players_min": "players:min",
        "players_max": "players:max",
        "trait": f"trait:{slug}",
        "policy": f"policy:{slug}",
    }[kind]


def _account_alias(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("account alias must be text")
    if not 1 <= len(value) <= 64:
        raise ValueError("account alias must be between 1 and 64 characters")
    if not value[0].isascii() or not value[0].isalpha():
        raise ValueError("account alias must begin with an ASCII letter")
    if any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in value
    ):
        raise ValueError(
            "account alias may contain only ASCII letters, digits, _ and -"
        )
    return value


def _user_trait(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("user:"):
        raise ValueError("trait must use the user namespace")
    slug = value[5:]
    if not 1 <= len(slug) <= 59:
        raise ValueError("user trait is invalid")
    if not slug[0].isascii() or not slug[0].islower() and not slug[0].isdigit():
        raise ValueError("user trait is invalid")
    if not slug[-1].isascii() or not slug[-1].islower() and not slug[-1].isdigit():
        raise ValueError("user trait is invalid")
    if any(
        not (
            character.isascii()
            and (character.islower() or character.isdigit() or character in "._-")
        )
        for character in slug
    ):
        raise ValueError("user trait is invalid")
    return value


def _catalog_appids(values: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= (1 << 32) - 1
        for value in values
    ):
        raise ValueError("catalog AppIDs must be positive unsigned 32-bit integers")
    normalized = tuple(sorted(set(values)))
    if len(normalized) != len(values):
        raise ValueError("catalog demanded AppIDs must be unique")
    return normalized
