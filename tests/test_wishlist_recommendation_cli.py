from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import steam_agent.cli as cli
from steam_agent.feedback import FeedbackService
from steam_agent.steam_reviews import (
    SteamReviewHumanReference,
    SteamReviewRequestContext,
    SteamReviewSummary,
)
from steam_agent.storage import Storage, WishlistObservation
from steam_agent.wishlist_recommendation_query import _deal


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def deal_item(
    *,
    bucket: str = "unknown",
    grade: str = "unknown",
    attempts: list[dict[str, object]],
    facts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "appid": 10,
        "deal": {
            "bucket": bucket,
            "evidence_grade": grade,
            "current_offer": None,
            "historical_low": None,
            "attempted_providers": attempts,
        },
        "evidence": {"offers": facts or [], "historical_lows": []},
        "references": [
            {"url": "https://gg.deals/steam/app/10/"},
            {"url": "https://steamdb.info/app/10/"},
        ],
    }


def test_m3_ladder_not_found_and_retained_evidence_are_not_collapsed() -> None:
    partial = deal_item(
        attempts=[
            {"provider": "gg-deals", "status": "not_found"},
            {"provider": "cheapshark", "status": "unavailable"},
        ]
    )
    assert _deal(partial, "US", "official", {}) is None  # type: ignore[arg-type]

    terminal = deal_item(
        attempts=[
            {"provider": "gg-deals", "status": "not_found"},
            {"provider": "cheapshark", "status": "not_found"},
        ]
    )
    subject = SimpleNamespace(
        outcome="not_found", observed_at="2026-07-11T12:00:00Z"
    )
    not_found = _deal(  # type: ignore[arg-type]
        terminal, "US", "official", {(10, "cheapshark"): subject}
    )
    assert not_found is not None and not_found.state == "not_found"
    assert not_found.provider == "cheapshark"

    retained = [
        {
            "evidence_id": 7,
            "provider": "gg-deals",
            "fresh": False,
            "observed_at": "2026-07-10T00:00:00Z",
        }
    ]
    stale = _deal(  # type: ignore[arg-type]
        deal_item(attempts=[], facts=retained), "US", "official", {}
    )
    assert stale is not None and stale.state == "unknown"
    assert stale.freshness == "stale" and stale.evidence_ids == ("price:7",)
    noncomparable = _deal(  # type: ignore[arg-type]
        deal_item(
            bucket="noncomparable",
            grade="degraded",
            attempts=[],
            facts=[
                retained[0],
                {
                    **retained[0],
                    "evidence_id": 8,
                    "fresh": True,
                    "observed_at": "2026-07-11T11:00:00Z",
                },
            ],
        ),
        "US",
        "official",
        {},
    )
    assert noncomparable is not None and noncomparable.bucket == "noncomparable"
    assert noncomparable.evidence_grade == "degraded"
    assert noncomparable.freshness == "fresh"


class Client:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def fetch_summary(self, appid: int) -> SteamReviewSummary:
        self.calls.append(appid)
        return SteamReviewSummary(
            appid,
            8,
            8,
            2,
            10,
            SteamReviewRequestContext(),
            "steam_store_appreviews",
            SteamReviewHumanReference(
                appid, f"https://store.steampowered.com/app/{appid}/#app_reviews_hash"
            ),
        )


def invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def configured(tmp_path: Path) -> tuple[Path, int]:
    data_dir = tmp_path / "data"
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
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
            (WishlistObservation(10, 0, 100, NOW),),
            item_list_retrieved_at=NOW,
            item_count_retrieved_at=NOW,
            item_list_reported_count=1,
            item_count_reported_count=1,
            completed_at=NOW,
        )
    return data_dir, account.id


def test_unavailable_wishlist_is_not_empty_and_invalid_alias_is_typed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    base = [
        "--data-dir",
        str(data_dir),
        "recommendations",
        "wishlist",
        "--country",
        "US",
    ]
    unavailable = invoke(base + ["--account", "primary"], capsys)
    assert unavailable["completeness"]["status"] == "unavailable"
    assert unavailable["data"]["empty"] is False
    assert unavailable["data"]["degradation_reasons"] == []

    assert cli.main(base + ["--account", "!invalid"]) == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"


def test_review_sync_requires_current_disclosure_and_retains_no_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path)
    common = ["--data-dir", str(data_dir), "sync", "reviews", "--scope", "wishlist"]
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    client = Client()
    monkeypatch.setattr(cli, "_steam_review_client", lambda: client)

    assert cli.main(common) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"
    assert client.calls == []

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.record_review_data_consent(
            account_id=1,
            disclosure_version="obsolete.m4",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
    assert cli.main(common) == 1
    obsolete = json.loads(capsys.readouterr().out)
    assert obsolete["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"
    assert client.calls == []

    synced = invoke(common + ["--acknowledge-local-storage"], capsys)
    assert synced["data"]["targeted_count"] == 1
    assert synced["data"]["review_text_retained"] is False
    assert client.calls == [10]
    encoded = json.dumps(synced)
    assert "review body canary" not in encoded
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        consent = storage.get_review_data_consent(1)
        assert consent is not None and consent.disclosure_version == "2026-07-11.m4"
    queried = invoke(
        [
            "--data-dir",
            str(data_dir),
            "recommendations",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    review = queried["data"]["ranked"][0]["review"]
    assert review["request_context"]["day_range"] == 365
    assert review["source_locator"] == "steam_store_appreviews"
    assert review["human_reference"] == {
        "appid": 10,
        "url": "https://store.steampowered.com/app/10/#app_reviews_hash",
        "purpose": "view_store_reviews",
        "access_mode": "manual_only",
        "automation_supported": False,
    }
    assert "/appreviews/" not in json.dumps(queried)
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage._connection.execute(  # noqa: SLF001
            "UPDATE review_current SET observed_at='2026-07-12T12:00:00Z'"
        )
        storage._connection.commit()  # noqa: SLF001
    future = invoke(
        [
            "--data-dir",
            str(data_dir),
            "recommendations",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert future["data"]["ranked"][0]["review"]["freshness"] == "unknown"
    assert "review" in future["data"]["ranked"][0]["missing"]
    assert any(
        warning["code"] == "REVIEW_CLOCK_REGRESSION"
        for warning in future["completeness"]["warnings"]
    )
    deleted = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "steam-store-reviews",
            "--account",
            "primary",
            "--yes",
        ],
        capsys,
    )
    assert deleted["data"]["current_removed"] == 1
    after_delete = invoke(
        [
            "--data-dir",
            str(data_dir),
            "recommendations",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
        ],
        capsys,
    )
    assert after_delete["data"]["ranked"][0]["review"] is None
    assert after_delete["data"]["review_snapshot"]["state"] == "not_synced"


def test_wishlist_query_is_cache_only_and_direct_feedback_changes_fit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, account_id = configured(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        cli,
        "_steam_review_client",
        lambda: (_ for _ in ()).throw(AssertionError("query attempted network")),
    )
    command = [
        "--data-dir",
        str(data_dir),
        "recommendations",
        "wishlist",
        "--account",
        "primary",
        "--country",
        "US",
    ]
    unknown = invoke(command, capsys)
    assert unknown["data"]["purchase_recommendation_supported"] is False
    assert unknown["data"]["degradation_reasons"] == [
        "insufficient_preference_evidence"
    ]
    assert unknown["data"]["ranked"][0]["name"] is None
    assert unknown["data"]["ranked"][0]["compatibility"] == {"state": "unknown", "evidence_ids": []}

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        service = FeedbackService(storage, clock=lambda: NOW)
        rating = service.rate(account_id, 10, "liked")
        estimate = service.estimate(
            account_id,
            10,
            minimum_session_minutes=30,
            remaining_minutes=None,
            clear_minimum_session_minutes=False,
            clear_remaining_minutes=False,
        )
    liked = invoke(command, capsys)
    item = liked["data"]["ranked"][0]
    assert liked["data"]["purchase_recommendation_supported"] is True
    assert item["preference_fit"]["score"] == 100
    assert item["preference_fit"]["factors"][0]["evidence_ids"] == [
        f"feedback:{rating.event_id}"
    ]
    assert all(
        f"feedback:{change.event_id}"
        not in item["preference_fit"]["factors"][0]["evidence_ids"]
        for change in estimate
    )
    assert item["review"] is None
    assert item["deal_value"]["state"] == "unknown"
    assert liked["context"]["recipe"] == "wishlist-fit/0.1"
    assert "76561198000000000" not in json.dumps(liked)
    assert cli.main(command + ["--format", "table"]) == 0
    table = capsys.readouterr().out
    assert "APPID\tNAME\tELIGIBILITY\tPREFERENCE_FIT\tDEAL\tREVIEW_TOTAL" in table
    assert "76561198000000000" not in table
