"""Execute every active M3 scenario's deterministic oracle in normal CI.

Unlike the M4 corpus, an M3 oracle addresses the ``deals query`` envelope, so
the executable proof runs the installed CLI over the materialized fixture.
Two independent properties are checked for each scenario:

* the document is reproducible -- two identical invocations over the same cache
  produce byte-identical JSON apart from ``generated_at``, and every declared
  assertion holds; and
* the ranking in the document is exactly ``rank_deals`` applied to the evidence
  the document itself publishes.  The retained facts are rebuilt into ranking
  DTOs here and re-ranked, so a bucket, evidence grade, or selected offer can
  never drift away from the accepted pure recipe.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

import pytest

import steam_agent.cli as cli
import steam_agent.storage as storage_module
from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    ManualReference,
    Money,
    OfferEvidence,
    ProductIdentity,
)
from steam_agent.deal_ranking import (
    DealCandidate,
    DealComparisonContext,
    ProviderAttempt,
    rank_deals,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import grade  # noqa: E402
from evals.runner.materialize import materialize  # noqa: E402

SCENARIO_PATHS = tuple(
    sorted((ROOT / "evals" / "scenarios" / "m3").glob("*.json"))
)


def _freeze_cli_clock(
    monkeypatch: pytest.MonkeyPatch, scenario: dict[str, Any]
) -> None:
    frozen = datetime.fromisoformat(
        scenario["frozen_time"].replace("Z", "+00:00")
    )

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    monkeypatch.setattr(storage_module, "datetime", FrozenDatetime)


def _run(scenario: dict[str, Any], data_dir: Path, capsys: Any) -> dict[str, Any]:
    requirement = scenario["tool_policy"]["required"][0]
    argv = requirement["command"].split()[1:] + list(requirement["arguments"])
    assert cli.main(["--data-dir", str(data_dir), *argv]) == 0
    return json.loads(capsys.readouterr().out)


def _reference(fact: dict[str, Any]) -> ManualReference:
    return ManualReference(fact["reference"]["url"], fact["reference"]["purpose"])


def _money(value: dict[str, Any]) -> Money:
    return Money(value["amount_minor"], value["currency"], value["country"])


def _snapshots(item: dict[str, Any]) -> tuple[DealEvidenceSnapshot, ...]:
    """Rebuild ranking snapshots from the fresh evidence the document lists."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for kind in ("offers", "historical_lows"):
        for fact in item["evidence"][kind]:
            if not fact["fresh"]:
                continue
            key = (fact["provider"], fact["product"]["provider_product_id"])
            groups.setdefault(key, []).append(fact)
    snapshots = []
    for (provider, product_id), facts in sorted(groups.items()):
        product = ProductIdentity(product_id, item["appid"])
        snapshots.append(
            DealEvidenceSnapshot(
                provider=provider,
                product=product,
                offers=tuple(
                    OfferEvidence(
                        provider=provider,
                        product=product,
                        price=_money(fact["price"]),
                        regular_price=(
                            None
                            if fact["regular_price"] is None
                            else _money(fact["regular_price"])
                        ),
                        discount_percent=fact["discount_percent"],
                        store_class=fact["store_class"],
                        observed_at=fact["observed_at"],
                        provider_url=_reference(fact),
                        comparability=fact["product"]["comparability"],
                        seller_id=fact["seller_id"],
                    )
                    for fact in facts
                    if fact["fact_kind"] == "offer"
                ),
                history_lows=tuple(
                    HistoricalLowSummary(
                        provider=provider,
                        product=product,
                        price=_money(fact["price"]),
                        observed_at=fact["observed_at"],
                        effective_at=fact["effective_at"],
                        scope=fact["scope"],
                        provider_url=_reference(fact),
                        comparability=fact["product"]["comparability"],
                    )
                    for fact in facts
                    if fact["fact_kind"] == "historical_low"
                ),
                observed_at=max(fact["observed_at"] for fact in facts),
                limitations=("eval fixture",),
            )
        )
    return tuple(snapshots)


def _reranked(document: dict[str, Any]) -> tuple[Any, ...]:
    context = DealComparisonContext(
        country=document["context"]["country"],
        currency=document["context"]["currency"],
        store_class=document["context"]["store_class"],
        history_scope=document["context"]["history_scope"],
    )
    candidates = [
        DealCandidate(
            item["appid"],
            _snapshots(item),
            tuple(
                ProviderAttempt(
                    attempt["provider"],
                    attempt["fallback_rung"],
                    attempt["status"],
                    attempt.get("error_code"),
                )
                for attempt in item["deal"]["attempted_providers"]
            ),
        )
        for item in document["data"]["items"]
    ]
    ranking = rank_deals(candidates, context=context)
    return ranking.candidates


def _assert_document_ranking(document: dict[str, Any]) -> None:
    ranked = _reranked(document)
    assert ranked, "every M3 scenario retains at least one wishlist candidate"
    items = document["data"]["items"]
    assert [item["appid"] for item in items] == [
        candidate.steam_appid for candidate in ranked
    ], "document AppIDs differ from exact rank_deals order"
    for item, candidate in zip(items, ranked, strict=True):
        assert candidate.bucket == item["deal"]["bucket"]
        assert candidate.evidence_grade == item["deal"]["evidence_grade"]
        assert candidate.fallback_rung == item["deal"]["fallback_rung"]
        assert list(candidate.reasons) == item["deal"]["reasons"]
        offer = item["deal"]["current_offer"]
        if offer is None:
            assert candidate.current_offer is None
        else:
            assert candidate.current_offer is not None
            assert (
                candidate.current_offer.price.amount_minor
                == offer["price"]["amount_minor"]
            )
            assert candidate.current_offer.provider == offer["provider"]


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=lambda path: path.stem)
def test_active_m3_deterministic_oracle_is_executable(
    path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    assert scenario["status"] == "active"
    _freeze_cli_clock(monkeypatch, scenario)
    materialize(scenario, tmp_path)

    document = _run(scenario, tmp_path, capsys)
    repeat = _run(scenario, tmp_path, capsys)
    assert document["data"] == repeat["data"]
    assert document["completeness"] == repeat["completeness"]

    assertions = scenario["deterministic_oracle"]["assertions"]
    assert assertions, "an active deterministic oracle cannot be empty"
    result = grade.grade_oracle(document, {"assertions": assertions})
    assert result["passed"], result["failed"]


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=lambda path: path.stem)
def test_m3_document_ranking_is_rank_deals_over_its_own_evidence(
    path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    _freeze_cli_clock(monkeypatch, scenario)
    materialize(scenario, tmp_path)
    document = _run(scenario, tmp_path, capsys)

    _assert_document_ranking(document)


def test_m3_reranking_rejects_reversed_document_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = json.loads(
        (
            ROOT / "evals" / "scenarios" / "m3" / "m3-d01-best-official-deals.json"
        ).read_text(encoding="utf-8")
    )
    _freeze_cli_clock(monkeypatch, scenario)
    materialize(scenario, tmp_path)
    document = _run(scenario, tmp_path, capsys)
    assert len(document["data"]["items"]) > 1
    document["data"]["items"].reverse()

    with pytest.raises(AssertionError, match="exact rank_deals order"):
        _assert_document_ranking(document)


def test_m3_d01_oracle_rejects_one_wrong_offer_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = json.loads(
        (
            ROOT / "evals" / "scenarios" / "m3" / "m3-d01-best-official-deals.json"
        ).read_text(encoding="utf-8")
    )
    _freeze_cli_clock(monkeypatch, scenario)
    materialize(scenario, tmp_path)
    document = _run(scenario, tmp_path, capsys)
    document["data"]["items"][1]["deal"]["current_offer"]["provider"] = (
        "cheapshark"
    )

    result = grade.grade_oracle(document, scenario["deterministic_oracle"])

    assert not result["passed"]
    assert result["failed"] == [
        {
            "path": "$.data.items[*].deal.current_offer.provider",
            "operator": "ordered_equals",
            "expected": ["gg-deals", "gg-deals", "gg-deals"],
        }
    ]
