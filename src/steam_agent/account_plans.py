"""Inert account proposals and purchase handoffs from retained cache evidence.

Nothing here authorizes or performs an account mutation. Desired wishlist
priority is caller intent; only a separately synchronized snapshot is Steam
evidence. Purchase cards never establish licenses or checkout totals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, Mapping, TypedDict

from steam_agent.deal_query import build_deal_query_from_snapshot

if TYPE_CHECKING:
    from steam_agent.deal_evidence import StoreClass
    from steam_agent.storage import OwnedSnapshot, WishlistDealSnapshot, WishlistSnapshot


ACCOUNT_SNAPSHOT_FRESHNESS = timedelta(hours=24)


class PlannerResult(TypedDict):
    context: dict[str, object]
    completeness: dict[str, object]
    data: dict[str, object]


class BaselineConflict(ValueError):
    """The requested baseline is not the currently promoted wishlist."""


@dataclass(frozen=True, slots=True)
class WishlistChange:
    appid: int
    action: Literal["add", "remove", "reprioritize"]
    priority: int | None = None
    baseline_sync_id: int | None = None

    def __post_init__(self) -> None:
        _appid(self.appid)
        if self.action not in {"add", "remove", "reprioritize"}:
            raise ValueError("unsupported wishlist proposal action")
        if self.action == "reprioritize":
            if (
                not isinstance(self.priority, int) or isinstance(self.priority, bool)
                or not 0 <= self.priority <= 0xFFFFFFFF
            ):
                raise ValueError("reprioritize requires a nonnegative integer priority")
        elif self.priority is not None:
            raise ValueError("priority applies only to reprioritize")
        if self.baseline_sync_id is not None and (
            not isinstance(self.baseline_sync_id, int)
            or isinstance(self.baseline_sync_id, bool) or self.baseline_sync_id <= 0
        ):
            raise ValueError("baseline sync ID must be a positive integer")


def _appid(appid: int) -> None:
    if not isinstance(appid, int) or isinstance(appid, bool) or not 1 <= appid <= 0xFFFFFFFF:
        raise ValueError("AppID must be an unsigned nonzero 32-bit integer")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cached timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _now(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def human_reference(appid: int) -> dict[str, object]:
    _appid(appid)
    return {
        "url": f"https://store.steampowered.com/app/{appid}/",
        "access_mode": "open_for_human", "automation_supported": False,
        "purpose": "inspect the official Steam product before a human change",
    }


def _snapshot_state(snapshot: WishlistSnapshot | OwnedSnapshot | None, now: datetime):
    complete = snapshot.latest_complete if snapshot is not None else None
    latest = snapshot.latest if snapshot is not None else None
    observed_at = complete.completed_at or complete.started_at if complete else None
    age = now - _time(observed_at) if observed_at is not None else None
    authoritative = bool(
        complete is not None and latest is not None and latest.id == complete.id
        and latest.status == "complete" and latest.promoted
        and age is not None and timedelta(0) <= age <= ACCOUNT_SNAPSHOT_FRESHNESS
    )
    return complete, observed_at, authoritative


def wishlist_change_plan(
    snapshot: WishlistSnapshot | None, *, account_alias: str, change: WishlistChange,
    generated_at: datetime,
) -> PlannerResult:
    now = _now(generated_at)
    complete, observed_at, authoritative = _snapshot_state(snapshot, now)
    if change.baseline_sync_id is not None and (
        complete is None or complete.id != change.baseline_sync_id
    ):
        raise BaselineConflict("wishlist baseline no longer matches the promoted snapshot")
    item = next((game for game in snapshot.games if game.appid == change.appid), None) if snapshot else None
    retained_membership = item is not None if complete is not None else None
    expected_membership = retained_membership if authoritative else None
    status = "proposed"
    reason = None
    if not authoritative:
        status = "blocked"
        reason = "wishlist_baseline_unavailable" if complete is None else "wishlist_baseline_stale_or_superseded"
    elif change.action == "reprioritize" and item is None:
        status, reason = "blocked", "reprioritize_requires_observed_membership"
    elif (change.action == "add" and item is not None) or (
        change.action == "remove" and item is None
    ) or (change.action == "reprioritize" and item is not None and item.priority == change.priority):
        status = "already_satisfied_at_baseline"
    source = snapshot.latest_complete_provenance if snapshot else None
    return {
        "context": {"account_alias": account_alias, "scopes": ["wishlist"], "cache_only": True, "identifiers_included": False},
        "completeness": {
            "status": "complete" if authoritative else "unavailable" if complete is None else "partial",
            "missing_capabilities": ["wishlist.read"] if complete is None else [],
            "stale_capabilities": ["wishlist.read"] if complete is not None and not authoritative else [],
            "warnings": [],
        },
        "data": {
            "schema": "wishlist-change-proposal/0.1", "appid": change.appid,
            "status": status, "reason_code": reason, "execution_supported": False,
            "baseline": {
                "sync_id": complete.id if complete else None, "observed_at": observed_at,
                "freshness_policy": "promoted_latest_snapshot_within_24h", "authoritative": authoritative,
                "expected_membership": expected_membership, "retained_membership": retained_membership,
                "observed_priority": item.priority if item is not None else None,
                "evidence_ids": [item.evidence_id] if item is not None else [],
                "provider": source.provider if source else None,
                "support_level": source.support_level if source else None,
                "item_list_retrieved_at": source.item_list_retrieved_at if source else None,
                "item_count_retrieved_at": source.item_count_retrieved_at if source else None,
            },
            "desired_change": {"action": change.action, "priority": change.priority, "source": "local_caller_intent"},
            "completion": {"state": "not_observed", "requires": "human_change_then_separate_wishlist_sync"},
            "references": [human_reference(change.appid), {
                "url": "https://store.steampowered.com/my/wishlist/",
                "access_mode": "open_for_human", "automation_supported": False,
                "purpose": "review and change the wishlist in the human's Steam account",
            }],
        },
    }


def purchase_decision_plan(
    snapshot: WishlistDealSnapshot | None, *, owned: OwnedSnapshot | None,
    account_alias: str, appid: int, country: str, store_class: StoreClass,
    generated_at: datetime, compatibility: Mapping[str, object] | None = None,
) -> PlannerResult:
    _appid(appid)
    now = _now(generated_at)
    if country.upper() != "US":
        raise ValueError("purchase cards currently support only US/USD cached evidence")
    if store_class not in {"official", "keyshop", "unknown"}:
        raise ValueError("store class is invalid")
    query = build_deal_query_from_snapshot(
        snapshot, account_alias=account_alias, country="US", store_class=store_class,
        generated_at=now,
    ) if snapshot is not None else None
    items = query["data"]["items"] if query is not None else []  # type: ignore[index]
    item = next((candidate for candidate in items if candidate["appid"] == appid), None)
    offer = item["deal"]["current_offer"] if item is not None else None
    if offer is None and item is not None:
        offers = item["evidence"]["offers"]
        offer = offers[0] if offers else None
    comparable = bool(
        offer and offer["fresh"] and offer["product"]["mapping"] == "exact"
        and offer["product"]["comparability"] == "exact_product"
        and offer["price"]["country"] == "US" and offer["price"]["currency"] == "USD"
        and offer["store_class"] == store_class and _time(offer["observed_at"]) <= now
    )
    complete, observed_at, owned_fresh = _snapshot_state(owned, now)
    visible = any(game.appid == appid for game in owned.games) if owned else False
    ownership = {
        "visible_owned": visible if visible or owned_fresh else None,
        "license_owned": "unknown", "observed_at": observed_at, "fresh": owned_fresh,
        "sync_id": complete.id if complete else None,
        "limitation": "absence from visible-owned inventory does not establish absence of a license",
    }
    return {
        "context": {"account_alias": account_alias, "scopes": ["wishlist", "deals", "owned", "compatibility"], "cache_only": True, "country": "US", "currency": "USD", "store_class": store_class, "identifiers_included": False},
        "completeness": {
            "status": "partial" if snapshot is not None else "unavailable",
            "missing_capabilities": sorted([
                *([] if comparable else ["prices.exact_product.read"]),
                *([] if complete is not None else ["owned.visible.read"]),
                *([] if compatibility is not None else ["compatibility.read"]),
                "checkout.total.read", "license.inventory.read",
            ]),
            "stale_capabilities": sorted({
                *(["owned.visible.read"] if complete is not None and not owned_fresh else []),
                *(query["completeness"]["stale_capabilities"] if query else []),  # type: ignore[index]
            }),
            "warnings": [],
        },
        "data": {
            "schema": "purchase-decision-card/0.1", "appid": appid,
            "execution_supported": False, "handoff": "human_review_in_store",
            "product": {
                "steam_appid": appid, "kind": "app", "edition": "unknown",
                "dlc_base_game_requirement": "unknown",
                "provider_identity": offer["product"] if offer else None,
            },
            "quoted_offer": offer,
            "quote_selection": "ranked_current_offer" if item and item["deal"]["current_offer"] else "retained_unselected_evidence" if offer else "unavailable",
            "comparable_price": offer["price"] if comparable else None,
            "price_state": "fresh_exact_product_quote" if comparable else "unknown",
            "seller": offer.get("seller_id") if offer else None,
            "checkout_total": None, "checkout_total_state": "unknown",
            "ownership": ownership, "compatibility": dict(compatibility) if compatibility else None,
            "deal_snapshot": query["data"]["snapshots"] if query else None,  # type: ignore[index]
            "deal_completeness": query["completeness"] if query else None,
            "prerequisites": {
                "edition_and_dlc": "human_confirmation_required", "license": "unknown",
                "compatibility": "cached_assessment_only" if compatibility else "unknown",
                "tax_fees_discounts_and_checkout_total": "unknown",
            },
            "references": [human_reference(appid)],
            "limitations": [
                "a quoted offer is not an account-specific checkout total",
                "exact AppID mapping alone does not establish edition or DLC comparability",
                "no cart, checkout, wallet, credential or Steam mutation is performed",
                "price evidence is limited to the retained wishlist deal cache and US/USD",
            ],
        },
    }
