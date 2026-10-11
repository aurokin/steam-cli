"""Account plans retain baseline truth and never perform proposed changes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.account_plans import (
    BaselineConflict, WishlistChange, purchase_decision_plan, wishlist_change_plan,
)
from steam_agent.storage import (
    Machine, OwnedSnapshot, PriceSnapshot, Storage, StoredPriceFact, StoredPriceSubject,
    SyncRun, WishlistDealSnapshot, WishlistGame, WishlistObservation, WishlistSnapshot,
    steam_application_stable_id,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
STAMP = "2026-10-10T12:00:00Z"


def snapshot(*, members: tuple[int, ...] = (480,)) -> WishlistSnapshot:
    run = SyncRun(1, "steam_web_api", "wishlist.read", None, 99, STAMP, STAMP, "complete", True, len(members), None, None)
    return WishlistSnapshot(
        tuple(WishlistGame(99, appid, 0, 100, STAMP, appid, 1) for appid in members),
        run, run, None, tuple((appid, steam_application_stable_id(appid)) for appid in members),
    )


def plan(value: WishlistSnapshot | None, *, action="remove", priority=None, baseline=None, now=NOW):
    return wishlist_change_plan(
        value, account_alias="primary", change=WishlistChange(480, action, priority, baseline), generated_at=now,
    )


def test_wishlist_local_priority_remains_intent_until_separate_promoted_refresh() -> None:
    original = snapshot()
    proposal = plan(original, action="reprioritize", priority=7)
    assert proposal["data"]["status"] == "proposed"
    assert proposal["data"]["desired_change"] == {"action": "reprioritize", "priority": 7, "source": "local_caller_intent"}
    assert proposal["data"]["baseline"]["observed_priority"] == 0
    assert original.games[0].priority == 0
    assert proposal["data"]["completion"]["state"] == "not_observed"
    refreshed = replace(
        original, games=(replace(original.games[0], priority=7, promoted_sync_run_id=2),),
        latest=replace(original.latest, id=2), latest_complete=replace(original.latest_complete, id=2),
    )
    observed = plan(refreshed, action="reprioritize", priority=7, baseline=2)
    assert observed["data"]["status"] == "already_satisfied_at_baseline"
    assert observed["data"]["baseline"]["observed_priority"] == 7
    with pytest.raises(BaselineConflict):
        plan(refreshed, baseline=1)


@pytest.mark.parametrize("case", ["stale", "future", "failed_attempt", "missing"])
def test_wishlist_uncertain_baseline_never_becomes_definitive_membership(case: str) -> None:
    value = snapshot()
    now = NOW
    if case == "stale":
        now += timedelta(days=2)
    elif case == "future":
        now -= timedelta(seconds=1)
    elif case == "failed_attempt":
        value = replace(value, latest=replace(value.latest, id=2, status="failed", promoted=False))
    else:
        value = None
    result = plan(value, now=now)["data"]
    assert result["status"] == "blocked"
    assert result["baseline"]["expected_membership"] is None
    assert result["baseline"]["retained_membership"] is (None if case == "missing" else True)
    assert result["execution_supported"] is False


def test_wishlist_known_empty_and_absent_reprioritization_are_distinct() -> None:
    known_empty = snapshot(members=())
    assert plan(known_empty, action="add")["data"]["status"] == "proposed"
    assert plan(known_empty)["data"]["status"] == "already_satisfied_at_baseline"
    reprioritize = plan(known_empty, action="reprioritize", priority=1)["data"]
    assert reprioritize["status"] == "blocked"
    assert reprioritize["baseline"]["expected_membership"] is False
    assert reprioritize["reason_code"] == "reprioritize_requires_observed_membership"


@pytest.mark.parametrize("change", [
    ("reprioritize", None), ("reprioritize", -1), ("add", 1), ("remove", 1),
])
def test_wishlist_priority_arguments_are_unambiguous(change) -> None:
    with pytest.raises(ValueError):
        WishlistChange(480, *change)


def priced_snapshot(*, comparability="exact_product", fresh_until="2026-10-10T18:00:00Z") -> WishlistDealSnapshot:
    wishlist = snapshot()
    fact = StoredPriceFact(
        account_id=99, country="US", provider="gg-deals", appid=480, ordinal=0,
        fact_kind="offer", provider_product_id="synthetic-product-480", product_mapping="exact",
        amount_minor=500, currency="USD", regular_amount_minor=1000, discount_percent=50,
        store_class="official", comparability=comparability, low_scope=None, effective_at=None,
        observed_at=STAMP, fresh_until=fresh_until, hard_expires_at="2026-10-18T12:00:00Z",
        provider_url="https://gg.deals/game/synthetic/", access_mode="manual_only",
        automation_supported=0, evidence_id=42, promoted_sync_run_id=2, seller_id="synthetic-seller",
    )
    prices = PriceSnapshot(
        (fact,), (StoredPriceSubject(99, "US", "gg-deals", 480, "observed", STAMP, fresh_until, "2026-10-18T12:00:00Z", 2),),
        (), 0, 0, 0, False, False, (), (), (),
    )
    return WishlistDealSnapshot(wishlist, prices, wishlist.stable_game_ids_by_appid)


@pytest.mark.parametrize("comparability", ["exact_product", "normalized_game", "unknown"])
def test_purchase_exact_quote_is_separate_from_game_identity_and_checkout(comparability: str) -> None:
    result = purchase_decision_plan(
        priced_snapshot(comparability=comparability), owned=None, account_alias="primary", appid=480,
        country="US", store_class="official", generated_at=NOW,
    )["data"]
    assert result["quoted_offer"]["price"]["amount_minor"] == 500
    assert result["quoted_offer"]["reference"]["access_mode"] == "manual_only"
    assert result["comparable_price"] == (
        {"amount_minor": 500, "country": "US", "currency": "USD"} if comparability == "exact_product" else None
    )
    assert result["checkout_total"] is None
    assert result["ownership"]["license_owned"] == "unknown"
    assert result["product"]["edition"] == "unknown"
    assert result["product"]["dlc_base_game_requirement"] == "unknown"
    assert result["execution_supported"] is False
    assert result["references"][0]["access_mode"] == "open_for_human"


def test_purchase_retains_stale_quote_without_claiming_comparable_current_price() -> None:
    result = purchase_decision_plan(
        priced_snapshot(), owned=None, account_alias="primary",
        appid=480, country="US", store_class="official", generated_at=NOW + timedelta(hours=7),
    )
    assert result["data"]["quoted_offer"]["fresh"] is False
    assert result["data"]["comparable_price"] is None
    assert result["data"]["price_state"] == "unknown"
    assert "prices.wishlist.read" in result["completeness"]["stale_capabilities"]


@pytest.mark.parametrize("context", ["other_store_class", "future_quote"])
def test_exact_product_price_still_requires_matching_store_and_quote_time(context: str) -> None:
    value = priced_snapshot()
    if context == "other_store_class":
        value = replace(value, prices=replace(value.prices, facts=(replace(value.prices.facts[0], store_class="keyshop"),)))
    else:
        value = replace(value, prices=replace(value.prices, facts=(replace(value.prices.facts[0], observed_at="2026-10-10T13:00:00Z"),)))
    card = purchase_decision_plan(
        value, owned=None, account_alias="primary", appid=480, country="US",
        store_class="official", generated_at=NOW,
    )["data"]
    assert card["quoted_offer"] is not None
    assert card["comparable_price"] is None
    assert card["price_state"] == "unknown"


def test_purchase_rejects_reference_that_would_enable_automation() -> None:
    value = priced_snapshot()
    value = replace(value, prices=replace(value.prices, facts=(replace(value.prices.facts[0], automation_supported=1),)))
    with pytest.raises(ValueError, match="cannot support automation"):
        purchase_decision_plan(
            value, owned=None, account_alias="primary", appid=480, country="US",
            store_class="official", generated_at=NOW,
        )


def test_visible_owned_absence_does_not_establish_absent_license() -> None:
    value = snapshot(members=())
    owned = OwnedSnapshot((), value.latest, value.latest_complete, None, ())
    result = purchase_decision_plan(
        None, owned=owned, account_alias="primary", appid=480, country="US", store_class="official", generated_at=NOW,
    )["data"]
    assert result["ownership"]["visible_owned"] is False
    assert result["ownership"]["license_owned"] == "unknown"
    assert result["compatibility"] is None


def setup_cache(directory: Path, *, priority: int = 0) -> int:
    with Storage(directory / "steam-agent.sqlite3") as storage:
        account = storage.configure_steam_account(alias="primary", steam_id64="76561198000000000", configured_at=NOW)
        storage.record_wishlist_data_consent(account_id=account.id, disclosure_version="fixture", accepted_at=NOW, backups_acknowledged=True)
        storage.upsert_machine(Machine("local", "Synthetic", "linux"), observed_at=NOW)
        run = storage.begin_sync(provider="steam_web_api", capability="wishlist.read", account_id=account.id, started_at=NOW)
        storage.complete_wishlist_snapshot(
            run.id, [WishlistObservation(480, priority, 100, NOW)],
            item_list_retrieved_at=NOW, item_count_retrieved_at=NOW,
            item_list_reported_count=1, item_count_reported_count=1, completed_at=NOW,
        )
        return run.id


def invoke(directory: Path, args: list[str], capsys):
    code = cli.main(["--data-dir", str(directory), *args])
    output = capsys.readouterr()
    assert output.err == ""
    return code, json.loads(output.out)


@pytest.mark.parametrize("command", ["wishlist", "deals"])
def test_cli_plans_are_cache_only_and_inert_with_official_human_references(
    tmp_path: Path, monkeypatch, capsys, command: str,
) -> None:
    baseline = setup_cache(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    for name in ("_resolve_credential", "_steam_web_api_client", "_steam_wishlist_client", "_gg_deals_client", "_cheapshark_client"):
        monkeypatch.setattr(cli, name, lambda *args, **kwargs: pytest.fail("provider or credential accessed"))
    args = [command, "plan", "--account", "primary", "--appid", "480"]
    args += ["--action", "reprioritize", "--priority", "9", "--baseline-sync-id", str(baseline)] if command == "wishlist" else ["--country", "US"]
    code, envelope = invoke(tmp_path, args, capsys)
    assert code == 0
    assert envelope["schema_version"] == "0.1"
    assert envelope["context"]["cache_only"] is True
    assert envelope["data"]["execution_supported"] is False
    if command == "deals":
        assert envelope["data"]["compatibility"] is not None
        assert envelope["data"]["checkout_total_state"] == "unknown"
    assert all(reference["url"].startswith("https://store.steampowered.com/") for reference in envelope["data"]["references"])
    assert all(reference["access_mode"] == "open_for_human" and reference["automation_supported"] is False for reference in envelope["data"]["references"])
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert storage.read_wishlist_snapshot(account.id).games[0].priority == 0
    serialized = json.dumps(envelope)
    assert "76561198000000000" not in serialized
    assert str(tmp_path) not in serialized


def test_cli_baseline_conflict_and_country_limits_have_explicit_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    baseline = setup_cache(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    code, envelope = invoke(tmp_path, ["wishlist", "plan", "--account", "primary", "--appid", "480", "--action", "remove", "--baseline-sync-id", str(baseline + 1)], capsys)
    assert code == 2
    assert envelope["error"]["code"] == "WISHLIST_BASELINE_CONFLICT"
    code, envelope = invoke(tmp_path, ["deals", "plan", "--account", "primary", "--appid", "480", "--country", "CA"], capsys)
    assert code == 2
    assert envelope["error"]["code"] == "UNSUPPORTED_COUNTRY"


@pytest.mark.parametrize("command", ["wishlist", "deals"])
def test_missing_account_plan_is_explicit_unknown_not_known_empty(tmp_path: Path, monkeypatch, capsys, command: str) -> None:
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    args = [command, "plan", "--account", "primary", "--appid", "480"]
    args += ["--action", "add"] if command == "wishlist" else ["--country", "US"]
    code, envelope = invoke(tmp_path, args, capsys)
    assert code == 0
    assert envelope["completeness"]["status"] == "unavailable"
    assert "account.identity" in envelope["completeness"]["missing_capabilities"]
    if command == "wishlist":
        assert envelope["data"]["baseline"]["expected_membership"] is None
        assert envelope["data"]["status"] == "blocked"
    else:
        assert envelope["data"]["ownership"]["visible_owned"] is None
        assert envelope["data"]["comparable_price"] is None
