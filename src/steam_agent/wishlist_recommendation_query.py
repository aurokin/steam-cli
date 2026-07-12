"""Application mapping from one atomic cache snapshot to wishlist-fit/0.1."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from steam_agent.deal_query import build_deal_query_from_snapshot
from steam_agent.storage import WishlistRecommendationSnapshot
from steam_agent.wishlist_recommendations import (
    DealDimension,
    DealReference,
    DirectFeedback,
    GateOverride,
    ProfileRule,
    ReviewHumanReference,
    ReviewRequestContext,
    ReviewSummary,
    TraitAssertion,
    WishlistCandidate,
    WishlistFitContext,
    rank_wishlist,
)


def build_wishlist_recommendation_query(
    snapshot: WishlistRecommendationSnapshot,
    *,
    account_alias: str,
    country: str,
    store_class: str,
    unknown_policy: str,
    overrides: tuple[GateOverride, ...],
    generated_at: datetime,
    gg_credential_configured: bool,
) -> dict[str, object]:
    now = generated_at.astimezone(timezone.utc).replace(microsecond=0)
    deal_result = build_deal_query_from_snapshot(
        snapshot.deals,
        account_alias=account_alias,
        country=country,
        store_class=store_class,
        generated_at=now,
        gg_credential_configured=gg_credential_configured,
    )
    deal_items = {
        int(item["appid"]): item
        for item in deal_result["data"]["items"]  # type: ignore[index]
    }
    price_subjects = {
        (item.appid, item.provider): item for item in snapshot.deals.prices.subjects
    }
    feedback_by_appid = {item.appid: item for item in snapshot.feedback}
    reviews_by_appid = {int(item["appid"]): item for item in snapshot.reviews}
    candidates = tuple(
        WishlistCandidate(
            appid=game.appid,
            name=None,
            feedback=_feedback(feedback_by_appid.get(game.appid)),
            deal=_deal(
                deal_items.get(game.appid),
                country,
                store_class,
                price_subjects,
            ),
            review=_review(reviews_by_appid.get(game.appid), now),
            identity_evidence_ids=(f"wishlist:{game.evidence_id}",),
        )
        for game in snapshot.deals.wishlist.games
    )
    rules = tuple(
        ProfileRule(
            item.trait,
            item.kind,  # type: ignore[arg-type]
            item.strength,  # type: ignore[arg-type]
            item.weight,
            (f"preference-rule:{item.last_event_id}",),
        )
        for item in snapshot.rules
    )
    ranked = rank_wishlist(
        candidates,
        rules=rules,
        context=WishlistFitContext(
            country=country,
            currency="USD",
            store_class=store_class,  # type: ignore[arg-type]
            unknown_policy=unknown_policy,  # type: ignore[arg-type]
            generated_at=now,
        ),
        overrides=overrides,
    )
    data = _json(ranked)
    assert isinstance(data, dict)
    data.update(
        {
            "empty": not snapshot.deals.wishlist.games,
            "next_cursor": None,
            "deal_snapshot": deal_result["data"]["snapshots"],  # type: ignore[index]
            "deal_fallback": deal_result["data"]["fallback"],  # type: ignore[index]
            "review_snapshot": _review_snapshot(snapshot, now),
            "limitations": [
                "preference fit uses only direct feedback and explicit user traits",
                "aggregate reviews are report-only and never treated as user taste",
                "release and compatibility remain unknown",
                "queries are cache-only and do not open manual references",
            ],
        }
    )
    wishlist_completeness = deal_result["completeness"]
    status = str(wishlist_completeness["status"])  # type: ignore[index]
    missing = list(wishlist_completeness["missing_capabilities"])  # type: ignore[index]
    stale = list(wishlist_completeness["stale_capabilities"])  # type: ignore[index]
    warnings = list(wishlist_completeness["warnings"])  # type: ignore[index]
    review_snapshot = data["review_snapshot"]
    if review_snapshot["refresh_incomplete"]:  # type: ignore[index]
        warnings.append(
            {
                "code": "REVIEW_REFRESH_INCOMPLETE",
                "message": "Aggregate review refresh was incomplete; available last-good evidence remains attributed.",
            }
        )
    if review_snapshot["stale_current_count"]:  # type: ignore[index]
        warnings.append(
            {
                "code": "STALE_REVIEW_EVIDENCE",
                "message": "Some optional aggregate review evidence is stale.",
            }
        )
    if review_snapshot["future_current_count"]:  # type: ignore[index]
        warnings.append(
            {
                "code": "REVIEW_CLOCK_REGRESSION",
                "message": "Optional aggregate review evidence is future-dated and treated as unknown.",
            }
        )
    if ranked.status == "degraded":
        status = "partial" if status != "unavailable" else status
        warnings.extend(
            {"code": reason.upper(), "message": reason.replace("_", " ")}
            for reason in ranked.degradation_reasons
        )
    return {
        "context": {
            "account_alias": account_alias,
            "scopes": ["wishlist", "deals", "explicit_preferences", "reviews"],
            "country": country,
            "currency": "USD",
            "store_class": store_class,
            "unknown_policy": unknown_policy,
            "recipe": "wishlist-fit/0.1",
            "identifiers_included": False,
        },
        "completeness": {
            "status": status,
            "missing_capabilities": sorted(set(missing)),
            "stale_capabilities": sorted(set(stale)),
            "warnings": sorted(warnings, key=lambda item: str(item["code"])),
        },
        "data": data,
    }


def _feedback(value: Any) -> DirectFeedback:
    if value is None:
        return DirectFeedback()
    field_ids = dict(value.field_event_ids)
    rating_evidence = (
        ()
        if "rating" not in field_ids
        else (f"feedback:{field_ids['rating']}",)
    )
    snooze_evidence = (
        ()
        if "snooze" not in field_ids
        else (f"feedback:{field_ids['snooze']}",)
    )
    play_state_evidence = (
        ()
        if "play_state" not in field_ids
        else (f"feedback:{field_ids['play_state']}",)
    )
    traits = tuple(
        TraitAssertion(
            item.trait,
            item.value,
            (f"feedback:{item.last_event_id}",),
        )
        for item in value.traits
    )
    snooze = (
        None
        if value.snoozed_until is None
        else datetime.fromisoformat(value.snoozed_until.replace("Z", "+00:00"))
    )
    # user_abandoned is an explicit user-authored rejection, not behavioral inference.
    hard_exclude = value.play_state == "user_abandoned"
    return DirectFeedback(
        rating=value.rating,
        snoozed_until=snooze,
        hard_exclude=hard_exclude,
        traits=traits,
        rating_evidence_ids=rating_evidence,
        snooze_evidence_ids=snooze_evidence,
        hard_exclude_evidence_ids=play_state_evidence,
    )


def _deal(
    item: dict[str, Any] | None,
    country: str,
    store_class: str,
    subjects: dict[tuple[int, str], Any],
) -> DealDimension | None:
    if item is None:
        return None
    deal = item["deal"]
    current = deal["current_offer"]
    low = deal["historical_low"]
    selected = current or low
    references = tuple(
        DealReference(_reference_provider(ref["url"]), ref["url"])
        for ref in item["references"]
    )
    evidence_ids = tuple(
        sorted(
            {
                f"price:{fact['evidence_id']}"
                for kind in ("offers", "historical_lows")
                for fact in item["evidence"][kind]
            }
        )
    )
    attempts = deal["attempted_providers"]
    retained_facts = [
        fact
        for kind in ("offers", "historical_lows")
        for fact in item["evidence"][kind]
    ]
    ingest_attempts = {
        attempt["provider"]: attempt
        for attempt in attempts
        if attempt["provider"] in {"gg-deals", "cheapshark"}
    }
    ladder_not_found = all(
        ingest_attempts.get(provider, {}).get("status") == "not_found"
        for provider in ("gg-deals", "cheapshark")
    )
    not_found = ingest_attempts.get("cheapshark") if ladder_not_found else None
    if selected is None:
        if retained_facts and deal["bucket"] == "noncomparable":
            fact = retained_facts[0]
            return DealDimension(
                "deal-evidence/0.1",
                "ready",
                "noncomparable",
                "degraded",
                fact["provider"],
                store_class,
                country,
                "USD",
                "fresh" if fact["fresh"] else "stale",
                observed_at=datetime.fromisoformat(
                    fact["observed_at"].replace("Z", "+00:00")
                ),
                references=references,
                evidence_ids=evidence_ids,
            )
        if retained_facts and any(not fact["fresh"] for fact in retained_facts):
            fact = next(fact for fact in retained_facts if not fact["fresh"])
            return DealDimension(
                "deal-evidence/0.1",
                "unknown",
                "unknown",
                "unknown",
                fact["provider"],
                store_class,
                country,
                "USD",
                "stale",
                observed_at=datetime.fromisoformat(
                    fact["observed_at"].replace("Z", "+00:00")
                ),
                references=references,
                evidence_ids=evidence_ids,
            )
        if not_found is None:
            return None
        subject = subjects.get((int(item["appid"]), not_found["provider"]))
        if subject is None or subject.outcome != "not_found":
            raise ValueError("M3 not-found selection lacks attributed subject evidence")
        return DealDimension(
            "deal-evidence/0.1",
            "not_found",
            "unknown",
            "unknown",
            not_found["provider"],
            store_class,
            country,
            "USD",
            "fresh",
            observed_at=datetime.fromisoformat(subject.observed_at.replace("Z", "+00:00")),
            references=references,
            evidence_ids=evidence_ids,
        )
    observed = datetime.fromisoformat(selected["observed_at"].replace("Z", "+00:00"))
    provider = selected["provider"]
    return DealDimension(
        "deal-evidence/0.1",
        "ready",
        deal["bucket"],
        deal["evidence_grade"],
        provider,
        store_class,
        country,
        "USD",
        "fresh" if selected["fresh"] else "stale",
        current_amount_minor=None if current is None else current["price"]["amount_minor"],
        historical_low_amount_minor=None if low is None else low["price"]["amount_minor"],
        observed_at=observed,
        references=references,
        evidence_ids=evidence_ids,
    )


def _reference_provider(url: str) -> str:
    if url.startswith("https://gg.deals/"):
        return "gg-deals"
    if url.startswith("https://www.cheapshark.com/"):
        return "cheapshark"
    return "steamdb"


def _review(value: dict[str, Any] | None, now: datetime) -> ReviewSummary | None:
    if value is None:
        return None
    observed = datetime.fromisoformat(str(value["observed_at"]).replace("Z", "+00:00"))
    age = now - observed
    freshness = (
        "unknown"
        if age < timedelta(0)
        else "fresh"
        if age <= timedelta(hours=24)
        else "stale"
    )
    return ReviewSummary(
        "steam_store",
        int(value["total_positive"]),
        int(value["total_reviews"]),
        observed,
        freshness,  # type: ignore[arg-type]
        (
            (
                f"review:{value['appid']}:{value['observed_at']}"
                if value["promoted_sync_run_id"] is None
                else f"review-run:{value['promoted_sync_run_id']}"
            ),
        ),
        review_score=int(value["review_score"]),
        negative=int(value["total_negative"]),
        request_context=ReviewRequestContext(
            filter=value["request_filter"],
            language=value["language"],
            day_range=value["day_range"],
            review_type=value["review_type"],
            purchase_type=value["purchase_type"],
            num_per_page=value["num_per_page"],
            off_topic_activity_filtered=bool(value["off_topic_activity_filtered"]),
        ),
        source_locator=value["source_locator"],
        human_reference=ReviewHumanReference(
            int(value["appid"]), str(value["human_reference_url"])
        ),
    )


def _review_snapshot(
    snapshot: WishlistRecommendationSnapshot, now: datetime
) -> dict[str, object]:
    states: dict[str, int] = {}
    for row in snapshot.review_demand:
        state = str(row["state"])
        states[state] = states.get(state, 0) + 1
    stale_count = 0
    future_count = 0
    for row in snapshot.reviews:
        observed = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
        age = now - observed
        stale_count += int(age > timedelta(hours=24))
        future_count += int(age < timedelta(0))
    failed_or_unevaluated = sum(
        states.get(state, 0) for state in ("failed", "unevaluated", "running")
    )
    return {
        "state": "not_synced" if not snapshot.review_attempts else "observed",
        "last_attempt_status": (
            None if not snapshot.review_attempts else snapshot.review_attempts[-1].status
        ),
        "subject_states": {key: states[key] for key in sorted(states)},
        "current_count": len(snapshot.reviews),
        "stale_current_count": stale_count,
        "future_current_count": future_count,
        "refresh_incomplete": failed_or_unevaluated > 0,
        "using_last_good_count": sum(
            1
            for row in snapshot.review_demand
            if row["state"] in {"failed", "unevaluated", "running"}
            and any(item["appid"] == row["appid"] for item in snapshot.reviews)
        ),
        "optional_dimension": True,
    }


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return _json(asdict(value))
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


__all__ = ["build_wishlist_recommendation_query"]
