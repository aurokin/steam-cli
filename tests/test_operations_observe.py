from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from steam_agent.operations_observe import (
    SCHEMA,
    UNSUPPORTED_CAPABILITIES,
    InstalledAttempt,
    PromotedInstalledFact,
    observe_local_operations,
)


NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def fact(appid: int, **changes: object) -> PromotedInstalledFact:
    values: dict[str, object] = {
        "appid": appid,
        "presence": "present",
        "observed_at": NOW - timedelta(minutes=5),
        "evidence_ids": (f"evidence:{appid}",),
        "promoted_sync_run_id": 10,
        "build_id": "12001",
        "size_on_disk_bytes": 4_000_000,
        "manifest_source_modified_at": NOW - timedelta(days=1),
    }
    values.update(changes)
    return PromotedInstalledFact(**values)  # type: ignore[arg-type]


def batch(*facts: PromotedInstalledFact, requested: tuple[int, ...] | None = None):
    return observe_local_operations(
        requested_appids=requested or tuple(item.appid for item in facts),
        installed_facts=facts,
        generated_at=NOW,
    )


def test_emits_safe_deterministic_schema_and_manifest_facts() -> None:
    result = batch(fact(20), fact(10)).to_dict()

    assert result["schema"] == SCHEMA == "local-operation-state/0.1"
    assert result["generated_at"] == "2026-07-15T18:00:00Z"
    assert [item["appid"] for item in result["items"]] == [10, 20]  # type: ignore[index]
    first = result["items"][0]  # type: ignore[index]
    assert first == {
        "appid": 10,
        "installed": {
            "state": "present",
            "freshness": "fresh",
            "observed_at": "2026-07-15T17:55:00Z",
            "evidence_ids": ["evidence:10"],
        },
        "build_id": {"state": "known", "value": "12001"},
        "size_on_disk_bytes": {"state": "known", "value": 4_000_000},
        "manifest_source_modified_at": {
            "state": "known",
            "value": "2026-07-14T18:00:00Z",
        },
    }
    assert result["unsupported_capabilities"] == {
        capability: {
            "availability": "unavailable",
            "reason": "adapter_not_implemented",
        }
        for capability in UNSUPPORTED_CAPABILITIES
    }
    assert {"bandwidth", "completion_time"} <= set(
        result["unsupported_capabilities"]
    )
    serialized = repr(result)
    assert "path" not in serialized.casefold()
    assert "stateflags" not in serialized.casefold()


def test_missing_manifest_fields_remain_explicitly_unknown() -> None:
    item = batch(
        fact(
            10,
            build_id=None,
            size_on_disk_bytes=None,
            manifest_source_modified_at=None,
        )
    ).to_dict()["items"][0]  # type: ignore[index]

    assert item["build_id"] == {
        "state": "unknown",
        "value": None,
        "unknown_reason": "not_observed",
    }
    assert item["size_on_disk_bytes"]["state"] == "unknown"
    assert item["manifest_source_modified_at"]["state"] == "unknown"


def test_future_or_unparseable_manifest_source_time_is_unknown() -> None:
    future = batch(
        fact(10, manifest_source_modified_at=NOW + timedelta(seconds=1))
    ).items[0]
    invalid = batch(fact(10, manifest_source_modified_at="not-a-time")).items[0]

    assert (
        future.manifest_source_modified_at
        == future.manifest_source_modified_at.__class__(
            "unknown", None, "source_time_in_future"
        )
    )
    assert invalid.manifest_source_modified_at.state == "unknown"
    assert invalid.manifest_source_modified_at.unknown_reason == "not_observed"


def test_absence_is_distinct_from_missing_or_unknown_input() -> None:
    absent = fact(
        10,
        presence="absent",
        build_id=None,
        size_on_disk_bytes=None,
        manifest_source_modified_at=None,
    )
    unknown = fact(
        20,
        presence="unknown",
        observed_at=None,
        evidence_ids=(),
        build_id=None,
        size_on_disk_bytes=None,
        manifest_source_modified_at=None,
        unknown_reason="projection_unavailable",
    )
    items = batch(absent, unknown, requested=(10, 20, 30)).to_dict()["items"]

    assert items[0]["installed"]["state"] == "absent"
    assert items[0]["build_id"]["unknown_reason"] == "not_installed"
    assert items[1]["installed"] == {
        "state": "unknown",
        "freshness": "unknown",
        "observed_at": None,
        "evidence_ids": [],
        "unknown_reason": "projection_unavailable",
        "freshness_reason": "observation_time_unparseable_or_missing",
    }
    assert items[2]["installed"]["unknown_reason"] == "no_promoted_fact"


@pytest.mark.parametrize(
    ("observed_at", "freshness", "reason"),
    [
        (NOW - timedelta(minutes=15), "fresh", None),
        (NOW - timedelta(minutes=15, seconds=1), "stale", None),
        (NOW + timedelta(seconds=1), "unknown", "observation_time_in_future"),
        ("not-a-time", "unknown", "observation_time_unparseable_or_missing"),
        ("2026-07-15T18:00:00", "unknown", "observation_time_unparseable_or_missing"),
    ],
)
def test_operational_freshness_boundary_is_truthful(
    observed_at: object, freshness: str, reason: str | None
) -> None:
    installed = batch(fact(10, observed_at=observed_at)).to_dict()["items"][0][
        "installed"
    ]

    assert installed["freshness"] == freshness
    assert installed.get("freshness_reason") == reason


@pytest.mark.parametrize("status", ["partial", "failed", "running"])
def test_newer_incomplete_attempt_downgrades_last_good_to_stale(status: str) -> None:
    result = observe_local_operations(
        requested_appids=(10,),
        installed_facts=(fact(10),),
        generated_at=NOW,
        latest_attempt=InstalledAttempt(
            status,
            NOW - timedelta(minutes=1),
            11,  # type: ignore[arg-type]
        ),
    ).to_dict()

    assert result["items"][0]["installed"]["freshness"] == "stale"
    assert (
        result["items"][0]["installed"]["freshness_reason"]
        == "newer_incomplete_attempt"
    )


def test_older_incomplete_and_newer_complete_attempts_do_not_downgrade() -> None:
    old = observe_local_operations(
        requested_appids=(10,),
        installed_facts=(fact(10),),
        generated_at=NOW,
        latest_attempt=InstalledAttempt("failed", NOW - timedelta(minutes=10), 9),
    )
    complete = observe_local_operations(
        requested_appids=(10,),
        installed_facts=(fact(10),),
        generated_at=NOW,
        latest_attempt=InstalledAttempt("complete", NOW - timedelta(minutes=1), 11),
    )

    assert old.items[0].installed.freshness == "fresh"
    assert complete.items[0].installed.freshness == "fresh"


@pytest.mark.parametrize(
    ("attempted_at", "reason"),
    [
        ("bad", "latest_attempt_time_unparseable_or_missing"),
        (NOW + timedelta(seconds=1), "latest_attempt_time_in_future"),
    ],
)
def test_unorderable_incomplete_attempt_makes_freshness_unknown(
    attempted_at: object, reason: str
) -> None:
    result = observe_local_operations(
        requested_appids=(10,),
        installed_facts=(fact(10),),
        generated_at=NOW,
        latest_attempt=InstalledAttempt("partial", attempted_at, 11),  # type: ignore[arg-type]
    )

    assert result.items[0].installed.freshness == "unknown"
    assert result.items[0].installed.freshness_reason == reason


def test_equal_time_attempt_uses_lineage_conservatively() -> None:
    same = observe_local_operations(
        requested_appids=(10,),
        installed_facts=(fact(10),),
        generated_at=NOW,
        latest_attempt=InstalledAttempt("partial", NOW - timedelta(minutes=5), 10),
    )
    ambiguous = observe_local_operations(
        requested_appids=(10,),
        installed_facts=(fact(10),),
        generated_at=NOW,
        latest_attempt=InstalledAttempt("partial", NOW - timedelta(minutes=5), "other"),
    )

    assert same.items[0].installed.freshness == "fresh"
    assert ambiguous.items[0].installed.freshness == "stale"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requested_appids": (10, 10), "installed_facts": ()},
        {"requested_appids": (10,), "installed_facts": (fact(20),)},
        {"requested_appids": (10,), "installed_facts": (fact(10), fact(10))},
    ],
)
def test_rejects_ambiguous_subject_sets(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        observe_local_operations(generated_at=NOW, **kwargs)  # type: ignore[arg-type]


def test_validates_installed_only_values_and_unknown_reason() -> None:
    with pytest.raises(ValueError, match="only present"):
        fact(10, presence="absent")
    with pytest.raises(ValueError, match="requires an explanation"):
        fact(
            10,
            presence="unknown",
            build_id=None,
            size_on_disk_bytes=None,
            manifest_source_modified_at=None,
        )
    with pytest.raises(ValueError, match="size_on_disk_bytes"):
        fact(10, size_on_disk_bytes=-1)
    with pytest.raises(ValueError, match="evidence lineage"):
        fact(10, evidence_ids=())
