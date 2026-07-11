from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from jsonschema import validate
import pytest

import steam_agent.cli as cli
from steam_agent.storage import (
    PriceDemandSubject,
    PriceFactObservation,
    Storage,
    WishlistObservation,
)


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
STEAM_ID = "76561198000000000"


def invoke(
    data_dir: Path, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object], str]:
    code = cli.main(["--data-dir", str(data_dir), *arguments])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def wishlist(
    storage: Storage, *, observations: tuple[WishlistObservation, ...]
) -> tuple[int, int, tuple[PriceDemandSubject, ...]]:
    account = storage.configure_steam_account(
        alias="primary", steam_id64=STEAM_ID, configured_at=NOW
    )
    storage.record_wishlist_data_consent(
        account_id=account.id,
        disclosure_version="test",
        accepted_at=NOW,
        backups_acknowledged=True,
    )
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="wishlist.read",
        account_id=account.id,
        started_at=NOW,
    )
    storage.complete_wishlist_snapshot(
        run.id,
        observations,
        item_list_retrieved_at=NOW,
        item_count_retrieved_at=NOW,
        item_list_reported_count=len(observations),
        item_count_reported_count=len(observations),
        completed_at=NOW,
    )
    ordered = sorted(
        observations, key=lambda item: (item.priority, item.date_added, item.appid)
    )
    demand = tuple(
        PriceDemandSubject(item.appid, index, item.priority, item.date_added)
        for index, item in enumerate(ordered)
    )
    return account.id, run.id, demand


def offer(
    appid: int,
    *,
    provider: str,
    amount_minor: int,
    observed_at: datetime = NOW,
) -> PriceFactObservation:
    return PriceFactObservation(
        appid=appid,
        ordinal=0,
        fact_kind="offer",
        provider_product_id=f"steam/app/{appid}",
        amount_minor=amount_minor,
        currency="USD",
        regular_amount_minor=1000,
        discount_percent=None,
        store_class="official" if provider == "gg-deals" else "unknown",
        comparability=(
            "exact_product" if provider == "gg-deals" else "normalized_game"
        ),
        low_scope=None,
        effective_at=None,
        observed_at=observed_at,
        provider_url=(
            "https://gg.deals/game/synthetic/"
            if provider == "gg-deals"
            else "https://www.cheapshark.com/redirect?dealID=synthetic"
        ),
    )


def configure_gg_metadata(storage: Storage, database: Path) -> None:
    reference = cli._provider_credential_ref(
        database, cli._CREDENTIAL_PROVIDERS["gg-deals"]
    )
    storage.upsert_credential_reference(
        provider=reference.provider,
        kind=reference.kind,
        profile_id=reference.profile_id,
        backend="os",
        configured_at=NOW,
    )


def test_deals_query_missing_account_unsynced_and_empty_truth(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, missing, stderr = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert code == 0 and stderr == ""
    assert missing["schema_version"] == "0.1"
    assert missing["command"] == "deals.query"
    assert missing["completeness"]["status"] == "unavailable"  # type: ignore[index]
    assert missing["completeness"]["missing_capabilities"] == [  # type: ignore[index]
        "account.identity"
    ]
    assert missing["data"]["empty"] is False  # type: ignore[index]

    with Storage(database) as storage:
        account = storage.configure_steam_account(
            alias="primary", steam_id64=STEAM_ID, configured_at=NOW
        )
    code, unsynced, _ = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert code == 0
    assert unsynced["completeness"]["status"] == "unavailable"  # type: ignore[index]
    assert unsynced["data"]["empty"] is False  # type: ignore[index]

    with Storage(database) as storage:
        storage.record_wishlist_data_consent(
            account_id=account.id,
            disclosure_version="test",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account.id,
            started_at=NOW,
        )
        storage.complete_wishlist_snapshot(
            run.id,
            (),
            item_list_retrieved_at=NOW,
            item_count_retrieved_at=NOW,
            item_list_reported_count=0,
            item_count_reported_count=0,
            completed_at=NOW,
        )
    code, empty, _ = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert code == 0
    assert empty["completeness"]["status"] == "complete"  # type: ignore[index]
    assert empty["data"]["empty"] is True  # type: ignore[index]


def test_deals_query_temp_db_fallback_unevaluated_schema_table_and_determinism(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "private-data"
    database = data_dir / "steam-agent.sqlite3"
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    with Storage(database) as storage:
        account_id, wishlist_run, demand = wishlist(
            storage,
            observations=(
                WishlistObservation(10, 0, 100, NOW),
                WishlistObservation(20, 1, 200, NOW),
            ),
        )
        configure_gg_metadata(storage, database)
        gg = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW,
        )
        storage.complete_price_sync(
            gg.id,
            outcomes={10: "observed"},
            facts=(offer(10, provider="gg-deals", amount_minor=500),),
            completed_at=NOW,
            status="partial",
        )
        cheap = storage.begin_price_sync(
            provider="cheapshark",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW,
        )
        storage.complete_price_sync(
            cheap.id,
            outcomes={10: "not_found", 20: "observed"},
            facts=(offer(20, provider="cheapshark", amount_minor=400),),
            completed_at=NOW,
            status="complete",
        )

    arguments = [
        "deals",
        "query",
        "--scope",
        "wishlist",
        "--account",
        "primary",
        "--country",
        "US",
        "--store-class",
        "unknown",
    ]
    code, first, stderr = invoke(data_dir, arguments, capsys)
    code_again, second, _ = invoke(data_dir, arguments, capsys)

    assert code == code_again == 0 and stderr == ""
    assert first == second
    assert first["context"]["store_class"] == "unknown"  # type: ignore[index]
    assert first["completeness"]["status"] == "complete"  # type: ignore[index]
    codes = {
        warning["code"]
        for warning in first["completeness"]["warnings"]  # type: ignore[index]
    }
    assert {"DEGRADED_FALLBACK", "PRICE_EVIDENCE_NOT_EVALUATED"} <= codes
    items = first["data"]["items"]  # type: ignore[index]
    assert [item["appid"] for item in items] == [20, 10]
    fallback_item = next(item for item in items if item["appid"] == 20)
    assert fallback_item["deal"]["fallback_rung"] == 1
    assert fallback_item["deal"]["current_offer"]["price"]["amount_minor"] == 400
    validate(
        first,
        json.loads(
            (Path(__file__).parents[1] / "schemas" / "response.schema.json").read_text()
        ),
    )
    serialized = json.dumps(first)
    assert STEAM_ID not in serialized
    assert str(data_dir) not in serialized
    assert "api_key" not in serialized.lower()

    code = cli.main(["--data-dir", str(data_dir), *arguments, "--format", "table"])
    table = capsys.readouterr()
    assert code == 0 and table.err == ""
    assert "COMPLETENESS\tcomplete" in table.out
    assert "APPID\tBUCKET\tGRADE\tCURRENT_MINOR\tLOW_MINOR\tFALLBACK_RUNG" in table.out
    assert STEAM_ID not in table.out and str(data_dir) not in table.out


def test_deals_query_preserves_last_good_across_failure_then_marks_stale(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    with Storage(database) as storage:
        account_id, wishlist_run, demand = wishlist(
            storage, observations=(WishlistObservation(10, 0, 100, NOW),)
        )
        configure_gg_metadata(storage, database)
        good = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW,
        )
        storage.complete_price_sync(
            good.id,
            outcomes={10: "observed"},
            facts=(offer(10, provider="gg-deals", amount_minor=500),),
            completed_at=NOW,
            status="complete",
        )
        failed = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW + timedelta(hours=1),
        )
        storage.finish_price_sync(
            failed.id,
            completed_at=NOW + timedelta(hours=1),
            error_code="PROVIDER_UNAVAILABLE",
        )

    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(hours=1))
    _, failed_result, _ = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert failed_result["completeness"]["status"] == "partial"  # type: ignore[index]
    assert (
        failed_result["data"]["items"][0]["evidence"]["offers"][0][  # type: ignore[index]
            "price"
        ]["amount_minor"]
        == 500
    )

    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(hours=7))
    _, stale, _ = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert stale["completeness"]["status"] == "partial"  # type: ignore[index]
    assert (
        "prices.wishlist.read"
        in stale["completeness"][  # type: ignore[index]
            "stale_capabilities"
        ]
    )


def test_deals_query_marks_retained_attempt_expired_after_subject_hard_expiry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    with Storage(database) as storage:
        account_id, wishlist_run, demand = wishlist(
            storage, observations=(WishlistObservation(10, 0, 100, NOW),)
        )
        configure_gg_metadata(storage, database)
        run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW,
        )
        storage.complete_price_sync(
            run.id,
            outcomes={10: "observed"},
            facts=(offer(10, provider="gg-deals", amount_minor=500),),
            completed_at=NOW + timedelta(minutes=5),
            status="complete",
        )

    # Price subjects expire seven days after their underlying observation. The
    # coarse attempt lineage is retained until seven days after completion, so
    # this exercises the short interval where demand remains but evidence does not.
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(days=7, seconds=1))
    code, result, stderr = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )

    assert code == 0 and stderr == ""
    assert result["completeness"]["status"] == "partial"  # type: ignore[index]
    assert (  # type: ignore[index]
        "prices.wishlist.read" in result["completeness"]["stale_capabilities"]
    )
    assert "STALE_PRICE_EVIDENCE" in {
        warning["code"]
        for warning in result["completeness"]["warnings"]  # type: ignore[index]
    }
    prices = result["data"]["snapshots"]["prices"]  # type: ignore[index]
    gg_state = next(
        provider
        for provider in prices["providers"]
        if provider["provider"] == "gg-deals"
    )
    assert gg_state["state_counts"]["expired"] == 1
    assert prices["fact_count"] == 0
    assert (  # type: ignore[index]
        result["data"]["items"][0]["deal"]["current_offer"] is None
    )


def test_removed_then_readded_wishlist_item_requires_new_price_sync(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    with Storage(database) as storage:
        account_id, wishlist_run, demand = wishlist(
            storage, observations=(WishlistObservation(10, 0, 100, NOW),)
        )
        configure_gg_metadata(storage, database)
        price_run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW,
        )
        storage.complete_price_sync(
            price_run.id,
            outcomes={10: "observed"},
            facts=(offer(10, provider="gg-deals", amount_minor=500),),
            completed_at=NOW,
            status="complete",
        )

        removed = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=NOW + timedelta(hours=1),
        )
        storage.complete_wishlist_snapshot(
            removed.id,
            (),
            item_list_retrieved_at=NOW + timedelta(hours=1),
            item_count_retrieved_at=NOW + timedelta(hours=1),
            item_list_reported_count=0,
            item_count_reported_count=0,
            completed_at=NOW + timedelta(hours=1),
        )

        readded_at = NOW + timedelta(hours=2)
        readded = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=readded_at,
        )
        storage.complete_wishlist_snapshot(
            readded.id,
            (WishlistObservation(10, 0, 100, readded_at),),
            item_list_retrieved_at=readded_at,
            item_count_retrieved_at=readded_at,
            item_list_reported_count=1,
            item_count_reported_count=1,
            completed_at=readded_at,
        )

    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(hours=2))
    code, result, stderr = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )

    assert code == 0 and stderr == ""
    assert result["completeness"]["status"] == "partial"  # type: ignore[index]
    assert result["completeness"]["missing_capabilities"] == [  # type: ignore[index]
        "prices.wishlist.read"
    ]
    assert result["completeness"]["stale_capabilities"] == []  # type: ignore[index]
    item = result["data"]["items"][0]  # type: ignore[index]
    assert item["evidence"]["offers"] == []
    assert [
        (attempt["provider"], attempt["error_code"])
        for attempt in item["deal"]["attempted_providers"]
    ] == [("gg-deals", "NOT_SYNCED"), ("cheapshark", "NOT_SYNCED")]


def test_deals_query_reflects_provider_and_account_deletion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    with Storage(database) as storage:
        account_id, wishlist_run, demand = wishlist(
            storage, observations=(WishlistObservation(10, 0, 100, NOW),)
        )
        configure_gg_metadata(storage, database)
        run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW,
        )
        storage.complete_price_sync(
            run.id,
            outcomes={10: "observed"},
            facts=(offer(10, provider="gg-deals", amount_minor=500),),
            completed_at=NOW,
            status="complete",
        )
        storage.delete_price_data(provider="gg-deals", account_id=account_id)

    _, deleted_provider, _ = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert deleted_provider["completeness"]["status"] == "partial"  # type: ignore[index]
    assert deleted_provider["data"]["items"][0]["evidence"]["offers"] == []  # type: ignore[index]

    with Storage(database) as storage:
        storage.remove_account("primary")
    _, deleted_account, _ = invoke(
        data_dir,
        [
            "deals",
            "query",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert deleted_account["completeness"]["status"] == "unavailable"  # type: ignore[index]
    assert deleted_account["completeness"]["missing_capabilities"] == [  # type: ignore[index]
        "account.identity"
    ]
