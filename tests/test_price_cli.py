from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.credentials import (
    CredentialError,
    InMemoryCredentialStore,
    SecretValue,
)
from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    ManualReference,
    Money,
    OfferEvidence,
    ProductIdentity,
)
from steam_agent.gg_deals import GgDealsBatch, GgDealsError, RateLimitMetadata
from steam_agent.steam_wishlist import WishlistCount, WishlistItem, WishlistItems
from steam_agent.storage import Storage


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


class WishlistClient:
    def fetch_count(self, **_: object) -> WishlistCount:
        return WishlistCount("ready", 2)

    def fetch_items(self, **_: object) -> WishlistItems:
        return WishlistItems(
            "ready", (WishlistItem(20, 1, 200), WishlistItem(10, 0, 100))
        )


def evidence(provider: str, appid: int, amount: int) -> DealEvidenceSnapshot:
    product = ProductIdentity(f"product-{appid}", appid)
    reference = ManualReference(
        "https://gg.deals/game/synthetic/"
        if provider == "gg-deals"
        else "https://www.cheapshark.com/redirect?dealID=synthetic",
        "manual attributed offer",
    )
    offer = OfferEvidence(
        provider,
        product,
        Money(amount, "USD", "US"),
        None,
        None,
        "official" if provider == "gg-deals" else "unknown",
        "2026-07-11T12:00:00Z",
        reference,
        "normalized_game",
    )
    low = HistoricalLowSummary(
        provider,
        product,
        Money(max(0, amount - 100), "USD", "US"),
        "2026-07-11T12:00:00Z",
        None,
        "all_time_any_store",
        reference,
        "normalized_game",
    )
    return DealEvidenceSnapshot(
        provider, product, (offer,), (low,), "2026-07-11T12:00:00Z", ()
    )


class GgClient:
    def fetch_app_price_summaries(self, *, appids, api_key):
        assert api_key.reveal() == "gg-secret-canary-value"
        assert tuple(appids) == (10, 20)
        return GgDealsBatch(
            (10, 20),
            (evidence("gg-deals", 10, 0),),
            (20,),
            RateLimitMetadata(100, 99, 123),
        )


class GgUnavailableClient:
    def fetch_app_price_summaries(self, *, appids, api_key):
        raise GgDealsError("PROVIDER_UNAVAILABLE", retryable=True)


class CheapClient:
    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        assert appid == 20
        return evidence("cheapshark", appid, 500)


class CheapAnyClient:
    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        return evidence("cheapshark", appid, 600)


class UnreadableStore(InMemoryCredentialStore):
    def resolve(self, ref: object) -> SecretValue | None:
        raise CredentialError("CREDENTIAL_READ_FAILED")


def invoke(argv: list[str], capsys: object) -> tuple[int, dict[str, object], str]:
    code = cli.main(argv)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def test_cli_traces_wishlist_through_fallback_and_queryable_snapshot(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    store = InMemoryCredentialStore()
    steam_ref = cli._steam_credential_ref(database)
    gg_ref = cli._provider_credential_ref(
        database, cli._CREDENTIAL_PROVIDERS["gg-deals"]
    )
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
        for ref in (steam_ref, gg_ref):
            storage.upsert_credential_reference(
                provider=ref.provider,
                kind=ref.kind,
                profile_id=ref.profile_id,
                backend="os",
                configured_at=NOW,
            )
    store.put(steam_ref, SecretValue("steam-secret-canary-value"))
    store.put(gg_ref, SecretValue("gg-secret-canary-value"))
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: store
    )
    monkeypatch.setattr(cli, "_reserve_provider_request", lambda *args: True)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "_steam_wishlist_client", WishlistClient)
    monkeypatch.setattr(cli, "_gg_deals_client", lambda gate: GgClient())
    monkeypatch.setattr(cli, "_cheapshark_client", lambda gate: CheapClient())
    common = ["--data-dir", str(data_dir)]

    assert (
        invoke(common + ["sync", "wishlist", "--acknowledge-local-storage"], capsys)[0]
        == 0
    )
    code, result, stderr = invoke(
        common
        + [
            "sync",
            "prices",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
            "--provider",
            "auto",
        ],
        capsys,
    )

    assert code == 0 and stderr == ""
    assert result["completeness"]["status"] == "complete"  # type: ignore[index]
    assert result["data"]["evaluated_items"] == 2  # type: ignore[index]
    assert result["data"]["fallback_evaluated"] == 1  # type: ignore[index]
    serialized = json.dumps(result)
    assert "76561198000000000" not in serialized
    assert "secret-canary" not in serialized
    with Storage(database) as storage:
        snapshot = storage.read_price_snapshot(
            account_id=storage.get_account("primary").id,  # type: ignore[union-attr]
            country="US",
            now=NOW,
        )
        assert {
            (fact.provider, fact.appid, fact.fact_kind, fact.amount_minor)
            for fact in snapshot.facts
        } == {
            ("gg-deals", 10, "offer", 0),
            ("gg-deals", 10, "historical_low", 0),
            ("cheapshark", 20, "offer", 500),
            ("cheapshark", 20, "historical_low", 400),
        }
        assert all(
            fact.currency == "USD" and fact.country == "US" for fact in snapshot.facts
        )

    monkeypatch.setattr(cli, "_gg_deals_client", lambda gate: GgUnavailableClient())
    monkeypatch.setattr(cli, "_cheapshark_client", lambda gate: CheapAnyClient())
    code, degraded, stderr = invoke(
        common
        + [
            "sync",
            "prices",
            "--scope",
            "wishlist",
            "--account",
            "primary",
            "--country",
            "US",
            "--provider",
            "auto",
        ],
        capsys,
    )
    assert code == 0 and stderr == ""
    assert degraded["completeness"]["status"] == "complete"  # type: ignore[index]
    assert "DEGRADED_FALLBACK" in {
        warning["code"]
        for warning in degraded["completeness"]["warnings"]  # type: ignore[index]
    }

    code, deleted, _ = invoke(
        common
        + [
            "data",
            "delete",
            "--provider",
            "gg-deals",
            "--account",
            "primary",
            "--yes",
        ],
        capsys,
    )
    assert code == 0
    assert deleted["data"]["price_current_removed"] == 2  # type: ignore[index]
    assert store.resolve(gg_ref) is not None
    with Storage(database) as storage:
        account = storage.get_account("primary")
        assert account is not None
        remaining = storage.read_price_snapshot(
            account_id=account.id, country="US", now=NOW
        )
        assert {fact.provider for fact in remaining.facts} == {"cheapshark"}

    code, deleted_all, _ = invoke(
        common + ["data", "delete", "--provider", "gg-deals", "--all", "--yes"],
        capsys,
    )
    assert code == 0
    assert deleted_all["data"]["steam_account_data_preserved"] is True  # type: ignore[index]
    assert deleted_all["data"]["other_provider_data_preserved"] is True  # type: ignore[index]
    assert store.resolve(gg_ref) is None

    monkeypatch.setattr(cli, "_cheapshark_client", lambda gate: CheapAnyClient())
    code, degraded, _ = invoke(
        common
        + [
            "sync",
            "prices",
            "--scope",
            "wishlist",
            "--country",
            "US",
            "--provider",
            "auto",
            "--max-items",
            "1",
        ],
        capsys,
    )
    assert code == 0
    assert degraded["data"]["providers_attempted"] == ["cheapshark"]  # type: ignore[index]
    warnings = degraded["completeness"]["warnings"]  # type: ignore[index]
    assert any(item["code"] == "CREDENTIAL_NOT_FOUND" for item in warnings)


def test_non_us_country_is_rejected_before_provider_access(
    tmp_path: Path, capsys: object
) -> None:
    code, result, _ = invoke(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "sync",
            "prices",
            "--scope",
            "wishlist",
            "--country",
            "CA",
        ],
        capsys,
    )
    assert code == 2
    assert result["error"]["code"] == "UNSUPPORTED_COUNTRY"  # type: ignore[index]


def test_provider_delete_removes_unreadable_key_and_cached_data(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._provider_credential_ref(database, cli._CREDENTIAL_PROVIDERS["gg-deals"])
    with Storage(database) as storage:
        storage.upsert_credential_reference(
            provider=ref.provider,
            kind=ref.kind,
            profile_id=ref.profile_id,
            backend="os",
            configured_at=NOW,
        )
    store = UnreadableStore()
    store.put(ref, SecretValue("credential-long-enough"))
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: store
    )

    code, result, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "gg-deals",
            "--all",
            "--yes",
        ],
        capsys,
    )

    assert code == 0 and stderr == ""
    assert result["data"]["local_credential_removed"] is True  # type: ignore[index]
    assert not store.contains(ref)
    with Storage(database) as storage:
        assert (
            storage.get_credential_reference(
                provider=ref.provider, kind=ref.kind, profile_id=ref.profile_id
            )
            is None
        )
