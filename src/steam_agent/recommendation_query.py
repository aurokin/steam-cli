"""Map one atomic M4 storage snapshot into deterministic recommendations."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from steam_agent.activity import ACTIVITY_FRESH, ACHIEVEMENT_FRESH, HARD_RETENTION
from steam_agent.recommendations import (
    AchievementEvidence,
    ActivityEvidence,
    ConstraintOverride,
    ExplicitFeedback,
    ProfileRule,
    RecommendationCandidate,
    RecommendationContext,
    Requirement,
    TraitAssertion,
    rank_recommendations,
)
from steam_agent.storage import RecommendationSnapshot


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _freshness(observed_at: str | None, now: datetime, fresh_for: timedelta) -> str:
    if observed_at is None:
        return "unknown"
    age = now - _parse(observed_at)
    if age < timedelta(0):
        return "unknown"
    if age <= fresh_for:
        return "fresh"
    if age <= HARD_RETENTION:
        return "stale"
    return "expired"


def _recent_freshness(observed_at: str | None, now: datetime) -> str:
    if observed_at is None:
        return "unknown"
    age = now - _parse(observed_at)
    if age < timedelta(0):
        return "unknown"
    if age <= timedelta(hours=1):
        return "fresh"
    if age <= timedelta(hours=24):
        return "stale"
    return "expired"


def _achievement_state(row: dict[str, Any]) -> str:
    state = row["state"]
    if state == "ready":
        return "ready"
    if state == "profile_not_public":
        return "profile_private"
    if state == "achievements_not_supported":
        return "unsupported"
    if state in {"unevaluated", "running"}:
        return "unevaluated"
    error = row.get("error_code")
    if error == "AUTHENTICATION_FAILED":
        return "authentication_failed"
    if error in {"PROVIDER_RATE_LIMITED", "REQUEST_THROTTLED"}:
        return "rate_limited"
    if error in {"PROVIDER_RESPONSE_INVALID", "INVALID_RESPONSE"}:
        return "invalid_response"
    return "provider_failed"


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


def build_recommendation_query(
    snapshot: RecommendationSnapshot,
    *,
    recipe: str,
    now: datetime,
    time_minutes: int | None,
    requirements: tuple[Requirement, ...],
    unknown_policy: str,
    overrides: tuple[ConstraintOverride, ...],
    explain: bool,
) -> dict[str, Any]:
    """Build a JSON-ready query result without performing I/O."""

    now = now.astimezone(timezone.utc).replace(microsecond=0)
    installed_ids = {item.appid for item in snapshot.installed.games}
    installed_known = snapshot.installed.latest_complete is not None
    activity = {int(item["appid"]): dict(item) for item in snapshot.activity_items}
    achievements = {int(item["appid"]): dict(item) for item in snapshot.achievement_items}
    feedback = {item.appid: item for item in snapshot.feedback}
    classifications = {
        fact.appid: fact
        for fact in snapshot.catalog.facts
    }
    stable_ids = dict(snapshot.owned.stable_game_ids_by_appid)

    candidates: list[RecommendationCandidate] = []
    for owned in snapshot.owned.games:
        activity_row = activity.get(owned.appid)
        activity_value = None
        if activity_row is not None:
            recent = activity_row["recent_window_minutes"]
            recent_observed = activity_row["recent_observed_at"]
            if recent_observed is None or now - _parse(recent_observed) > timedelta(hours=24):
                recent = None
            activity_value = ActivityEvidence(
                freshness=_freshness(activity_row["observed_at"], now, ACTIVITY_FRESH),
                observed_at=_parse(activity_row["observed_at"]),
                lifetime_minutes=activity_row["playtime_forever_minutes"],
                recent_window_minutes=recent,
                last_played_at=(
                    None
                    if activity_row["last_played_unix"] is None
                    else datetime.fromtimestamp(activity_row["last_played_unix"], timezone.utc)
                ),
                evidence_ids=(f"activity:{activity_row['promoted_sync_run_id']}:{owned.appid}",),
                recent_freshness=_recent_freshness(recent_observed, now),
            )
        achievement_row = achievements.get(owned.appid)
        achievement_value = None
        if achievement_row is not None:
            player_observed_at = achievement_row.get("player_observed_at")
            player_sync_run_id = achievement_row.get("player_sync_run_id")
            has_last_good = player_observed_at is not None
            state = "ready" if has_last_good else _achievement_state(achievement_row)
            evidence_observed_at = (
                player_observed_at if has_last_good else achievement_row["observed_at"]
            )
            achievement_value = AchievementEvidence(
                state=state,
                freshness=_freshness(
                    evidence_observed_at, now, ACHIEVEMENT_FRESH
                ),
                observed_at=(
                    None
                    if evidence_observed_at is None
                    else _parse(evidence_observed_at)
                ),
                unlocked=int(achievement_row["unlocked"] or 0) if state == "ready" else None,
                total=int(achievement_row["total"] or 0) if state == "ready" else None,
                evidence_ids=(
                    f"achievement:{player_sync_run_id or achievement_row['sync_run_id']}:{owned.appid}",
                ),
            )
        stored_feedback = feedback.get(owned.appid)
        feedback_value = ExplicitFeedback()
        if stored_feedback is not None:
            by_field = dict(stored_feedback.field_event_ids)
            def field_ids(name: str) -> tuple[str, ...]:
                event_id = by_field.get(name)
                return () if event_id is None else (f"feedback-event:{event_id}",)
            feedback_value = ExplicitFeedback(
                rating=stored_feedback.rating,
                play_state=stored_feedback.play_state,
                snoozed_until=(
                    None if stored_feedback.snoozed_until is None else _parse(stored_feedback.snoozed_until)
                ),
                minimum_session_minutes=stored_feedback.minimum_session_minutes,
                remaining_minutes=stored_feedback.remaining_minutes,
                traits=tuple(
                    TraitAssertion(
                        trait.trait,
                        trait.value,
                        (f"feedback-event:{trait.last_event_id}",),
                    )
                    for trait in stored_feedback.traits
                ),
                rating_evidence_ids=field_ids("rating"),
                play_state_evidence_ids=field_ids("play_state"),
                snooze_evidence_ids=field_ids("snooze"),
                minimum_session_evidence_ids=field_ids("minimum_session_minutes"),
                remaining_evidence_ids=field_ids("remaining_minutes"),
            )
        classification_fact = classifications.get(owned.appid)
        classification = (
            "not_observed" if classification_fact is None else classification_fact.classification
        )
        classification_fresh = (
            classification_fact is not None
            and now - _parse(classification_fact.observed_at) <= timedelta(hours=24)
            and now >= _parse(classification_fact.observed_at)
        )
        candidates.append(
            RecommendationCandidate(
                appid=owned.appid,
                name=owned.name,
                owned=True,
                installed=(owned.appid in installed_ids) if installed_known else None,
                activity=activity_value,
                achievements=achievement_value,
                feedback=feedback_value,
                identity_evidence_ids=(f"identity:{stable_ids[owned.appid]}",),
                owned_evidence_ids=(f"owned:{owned.evidence_id}",),
                installed_evidence_ids=(
                    ()
                    if not installed_known
                    else (f"installed-snapshot:{snapshot.installed.latest_complete.id}",)
                ),
                is_game=(
                    True if classification_fresh and classification == "game" else False if classification_fresh and classification == "non_game" else None
                ),
                classification_evidence_ids=(
                    () if classification_fact is None else (f"catalog:{classification_fact.evidence_id}",)
                ),
            )
        )
    rules = tuple(
        ProfileRule(
            rule.trait,
            rule.kind,
            rule.strength,
            rule.weight,
            (f"preference-event:{rule.last_event_id}",),
        )
        for rule in snapshot.rules
    )
    context = RecommendationContext(
        recipe=recipe,
        now=now,
        time_minutes=time_minutes,
        requirements=requirements,
        unknown_policy=unknown_policy,
        overrides=overrides,
        explain=explain,
    )
    ranking = rank_recommendations(tuple(candidates), profile_rules=rules, context=context)
    result = _json_ready(asdict(ranking))
    result["context"]["now"] = _json_ready(context.now)
    result["counts"] = dict(result["counts"])
    return result


__all__ = ["build_recommendation_query"]
