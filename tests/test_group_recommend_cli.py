from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.groups import MemberRef
from steam_agent.storage import (
    GROUP_PROFILE_DISCLOSURE_VERSION,
    Machine,
    OwnedObservation,
    Storage,
)


NOW = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)
ALPHA = MemberRef("synthetic", "alpha")
BETA = MemberRef("synthetic", "beta")


def test_candidate_and_seed_declared_reads_keep_independent_bounds() -> None:
    class FakeStorage:
        def __init__(self) -> None:
            self.calls: list[tuple[int, ...]] = []

        def read_declared_app_snapshot(self, **kwargs: object):
            appids = kwargs["appids"]
            assert isinstance(appids, tuple)
            self.calls.append(appids)
            return {
                "items": tuple({"appid": appid, "facts": None} for appid in appids),
                "latest_demand": tuple(
                    {"appid": appid, "sync_run_id": None} for appid in appids
                ),
            }

    storage = FakeStorage()
    candidates = tuple(range(1, 10_001))
    result, stale = cli._group_declared_payloads(  # noqa: SLF001
        storage,  # type: ignore[arg-type]
        account_id=1,
        machine_id="local",
        country="US",
        language="english",
        candidate_appids=candidates,
        seed_appids=(1, 10_001),
        now=NOW,
    )

    assert [len(call) for call in storage.calls] == [10_000, 1]
    assert len(result) == 10_001
    assert stale == frozenset()


def test_preference_seed_bound_is_rejected_before_storage(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "Storage", lambda *_args, **_kwargs: pytest.fail("storage was opened")
    )
    arguments = [
        "group",
        "recommend",
        "--scope",
        "appids",
        "--limit",
        "1",
        "--appid",
        "400",
        "--member",
        "synthetic:alpha",
        "--member",
        "synthetic:beta",
        "--context-account",
        "primary",
        "--context-machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--mode",
        "online_coop",
        "--objective",
        "preference-fit",
        "--like",
        "synthetic:beta=400",
    ]
    for appid in range(1, 66):
        arguments.extend(("--like", f"synthetic:alpha={appid}"))

    code, value, _ = invoke(tmp_path, capsys, *arguments)

    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_empty_bounded_recommendation_scope_returns_empty_success(
    tmp_path: Path, capsys: object
) -> None:
    setup(tmp_path)

    code, value, _ = invoke(
        tmp_path,
        capsys,
        "group",
        "recommend",
        "--scope",
        "wishlist",
        "--limit",
        "10",
        "--member",
        "synthetic:alpha",
        "--member",
        "synthetic:beta",
        "--context-account",
        "primary",
        "--context-machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--mode",
        "online_coop",
        "--objective",
        "min-copies",
    )

    assert code == 0
    assert value["data"]["candidate_count"] == 0
    assert value["data"]["results"] == []


def invoke(tmp_path: Path, capsys: object, *arguments: str):
    code = cli.main(["--data-dir", str(tmp_path), *arguments])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def payload(appid: int, *, genre: int, category: int = 38) -> dict[str, object]:
    return {
        "schema_id": "declared-app-facts/0.2",
        "appid": appid,
        "context": {"country": "US", "language": "english"},
        "platforms": {
            "state": "declared",
            "windows": True,
            "macos": False,
            "linux": True,
        },
        "requirements": [
            {
                "platform": name,
                "state": "undeclared",
                "minimum": None,
                "recommended": None,
            }
            for name in ("linux", "macos", "windows")
        ],
        "languages": {"state": "undeclared", "items": [], "unrecognized_count": 0},
        "categories": {
            "state": "declared",
            "known_slugs": ["online_co_op"] if category == 38 else ["online_pvp"],
            "unknown_ids": [],
            "source": "steam_store_appdetails",
            "numeric_ids": [category],
        },
        "genres": {
            "state": "declared",
            "source": "steam_store_appdetails",
            "items": [{"id": genre, "localized_label": f"Genre {genre}"}],
        },
        "coming_soon": {"state": "absent", "localized_date_display": "Available"},
        "controller_support": None,
        "external_account_notice": {"state": "unknown", "text": None},
        "drm_notice": {"state": "unknown", "text": None},
        "source": {
            "provider": "steam_store",
            "support_level": "provisional",
            "source_locator": "steam_store_appdetails",
            "human_reference_url": f"https://store.steampowered.com/app/{appid}/?cc=US&l=english",
            "access_mode": "manual_only",
            "automation_supported": False,
        },
    }


def setup(tmp_path: Path, *, owned: tuple[int, ...] = ()) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("local", "Machine", "linux", "x86_64"), observed_at=NOW
        )
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198999999999",
            configured_at=NOW,
        )
        storage.record_compatibility_data_consent(
            account_id=account.id,
            disclosure_version="m5-v1",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        if owned:
            storage.record_owned_data_consent(
                account_id=account.id,
                disclosure_version="owned-visible-v1",
                accepted_at=NOW,
                backups_acknowledged=True,
            )
            run = storage.begin_sync(
                provider="steam_web_api",
                capability="owned.visible.read",
                account_id=account.id,
                started_at=NOW,
            )
            storage.complete_owned_snapshot(
                run.id,
                tuple(
                    OwnedObservation(appid, 0, "visible_owned", NOW, None)
                    for appid in owned
                ),
                base_retrieved_at=NOW,
                expanded_retrieved_at=NOW,
                base_reported_count=len(owned),
                expanded_reported_count=len(owned),
                completed_at=NOW,
            )
        for alias in ("alpha", "beta"):
            storage.create_synthetic_group_profile(
                alias,
                disclosure_version=GROUP_PROFILE_DISCLOSURE_VERSION,
                backups_acknowledged=True,
                created_at=NOW,
            )


def seed(tmp_path: Path, facts: dict[int, dict[str, object]]) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert account is not None
        appids = sorted(facts)
        run, _, _ = storage.begin_declared_app_sync(
            account_id=account.id,
            machine_id="local",
            demanded_appids=appids,
            country="US",
            language="english",
            max_items=len(appids),
            skip_fresh_terminal=True,
            started_at=NOW,
            disclosure_version="m5-v1",
        )
        for appid in appids:
            storage.record_declared_app_result(
                run.id,
                account_id=account.id,
                appid=appid,
                state="ready",
                observed_at=NOW,
                facts=facts[appid],
            )
        storage.finish_declared_app_sync(run.id, completed_at=NOW)


def own(tmp_path: Path, ref: MemberRef, *appids: int) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        for appid in appids:
            storage.set_group_ownership(ref, appid=appid, state="owned", updated_at=NOW)


def common(*, objective: str, appids: tuple[int, ...]) -> list[str]:
    arguments = [
        "group",
        "recommend",
        "--scope",
        "appids",
        "--limit",
        "20",
        "--member",
        "synthetic:Alpha",
        "--member",
        "synthetic:Beta",
        "--context-account",
        "primary",
        "--context-machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--mode",
        "online_coop",
        "--objective",
        objective,
    ]
    for appid in appids:
        arguments.extend(("--appid", str(appid)))
    return arguments


def test_no_purchase_and_min_copies_use_shared_copy_certainty(tmp_path, capsys) -> None:
    setup(tmp_path)
    seed(tmp_path, {10: payload(10, genre=1), 20: payload(20, genre=2)})
    own(tmp_path, ALPHA, 10, 20)
    own(tmp_path, BETA, 10)

    code, value, stderr = invoke(
        tmp_path, capsys, *common(objective="no-purchase", appids=(10, 20))
    )
    assert code == 0 and stderr == ""
    assert [item["appid"] for item in value["data"]["results"]] == [10]
    assert value["data"]["results"][0]["copies"]["guarantee"] == "guaranteed"

    code, value, _ = invoke(
        tmp_path, capsys, *common(objective="min-copies", appids=(10, 20))
    )
    assert code == 0
    assert [item["appid"] for item in value["data"]["results"]] == [10, 20]
    assert value["data"]["results"][1]["copies"]["guarantee"] == "conditional"


def test_preference_fit_requires_member_qualified_seeds_and_ranks_similarity(
    tmp_path, capsys
) -> None:
    setup(tmp_path)
    seed(
        tmp_path,
        {
            10: payload(10, genre=1),
            20: payload(20, genre=2),
            100: payload(100, genre=1),
            200: payload(200, genre=1),
        },
    )
    own(tmp_path, ALPHA, 10, 20)
    own(tmp_path, BETA, 10, 20)
    args = common(objective="preference-fit", appids=(10, 20))
    args.extend(("--like", "synthetic:Alpha=100", "--like", "synthetic:Beta=200"))
    code, value, _ = invoke(tmp_path, capsys, *args)
    assert code == 0
    assert [item["appid"] for item in value["data"]["results"]] == [10, 20]
    first = value["data"]["results"][0]["preference"]
    assert first["state"] == "known"
    assert first["least_member_score_bps"] == 10_000
    assert [item["member_ordinal"] for item in first["members"]] == [0, 1]

    missing_member = common(objective="preference-fit", appids=(10,))
    missing_member.extend(("--like", "synthetic:Alpha=100"))
    code, value, _ = invoke(tmp_path, capsys, *missing_member)
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_seed_features_sort_unknown_after_known_within_certainty(
    tmp_path, capsys
) -> None:
    setup(tmp_path)
    seed(
        tmp_path,
        {
            10: payload(10, genre=1),
            20: payload(20, genre=2),
            100: payload(100, genre=1),
        },
    )
    own(tmp_path, ALPHA, 10, 20)
    own(tmp_path, BETA, 10, 20)
    args = common(objective="preference-fit", appids=(10, 20))
    args.extend(("--like", "synthetic:Alpha=100", "--like", "synthetic:Beta=999"))
    code, value, _ = invoke(tmp_path, capsys, *args)
    assert code == 0
    assert all(
        item["preference"]["state"] == "unknown" for item in value["data"]["results"]
    )
    assert [item["appid"] for item in value["data"]["results"]] == [10, 20]
    assert value["completeness"]["status"] == "partial"


def test_explicit_present_trait_excludes_but_missing_trait_remains_visible(
    tmp_path, capsys
) -> None:
    setup(tmp_path)
    seed(tmp_path, {10: payload(10, genre=1), 20: payload(20, genre=2)})
    own(tmp_path, ALPHA, 10, 20)
    own(tmp_path, BETA, 10, 20)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.set_group_app_assertion(
            ALPHA,
            appid=10,
            fact="trait:user:motion_heavy",
            value="present",
            updated_at=NOW,
        )
    args = common(objective="min-copies", appids=(10, 20))
    args.extend(("--exclude-trait", "user:motion_heavy"))
    code, value, _ = invoke(tmp_path, capsys, *args)
    assert code == 0
    assert value["data"]["explicit_trait_exclusions"] == 1
    assert [item["appid"] for item in value["data"]["results"]] == [20]
    assert value["data"]["results"][0]["trait_exclusion"]["state"] == "unknown"


def test_known_scope_is_context_bounded_and_does_not_admit_global_facts(
    tmp_path, capsys
) -> None:
    setup(tmp_path, owned=(10,))
    seed(tmp_path, {10: payload(10, genre=1), 999: payload(999, genre=9)})
    own(tmp_path, ALPHA, 10)
    own(tmp_path, BETA, 10)
    args = common(objective="min-copies", appids=())
    index = args.index("appids")
    args[index] = "known"
    code, value, _ = invoke(tmp_path, capsys, *args)
    assert code == 0
    assert value["data"]["candidate_count"] == 1
    assert [item["appid"] for item in value["data"]["results"]] == [10]


def test_group_recommend_is_cache_only_and_output_is_ordinal_redacted(
    tmp_path, capsys, monkeypatch
) -> None:
    setup(tmp_path)
    seed(tmp_path, {10: payload(10, genre=1)})
    own(tmp_path, ALPHA, 10)
    own(tmp_path, BETA, 10)

    def network_tripwire(*_args, **_kwargs):
        raise AssertionError("group recommend attempted network access")

    monkeypatch.setattr(cli.SteamDeclaredFactsClient, "fetch", network_tripwire)
    code, value, stderr = invoke(
        tmp_path, capsys, *common(objective="min-copies", appids=(10,))
    )
    assert code == 0 and stderr == ""
    assert value["context"]["network_used"] is False
    rendered = json.dumps(value).casefold()
    for private in ("alpha", "beta", "primary", "765611", "steam_web_api"):
        assert private not in rendered


@pytest.mark.parametrize(
    "mutation",
    (
        ("--limit", "0"),
        ("--limit", "10001"),
    ),
)
def test_recommend_bounds_are_rejected(tmp_path, capsys, mutation) -> None:
    setup(tmp_path)
    args = common(objective="min-copies", appids=(10,))
    position = args.index(mutation[0]) + 1
    args[position] = mutation[1]
    code, value, _ = invoke(tmp_path, capsys, *args)
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_recommend_member_evidence_contract_is_opt_in(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    setup(tmp_path, owned=(10,))
    seed(tmp_path, {10: payload(10, genre=1)})
    own(tmp_path, ALPHA, 10)
    own(tmp_path, BETA, 10)
    args = common(objective="min-copies", appids=(10,))
    args.extend(("--member", "account:primary"))

    code, default, stderr = invoke(tmp_path, capsys, *args)
    flagged_code, flagged, flagged_stderr = invoke(
        tmp_path, capsys, *args, "--include-member-evidence"
    )

    assert code == flagged_code == 0
    assert stderr == flagged_stderr == ""
    assert default["data"]["schema"] == "group-fit/0.1"
    assert "members" not in default["data"]
    assert flagged["data"]["schema"] == "group-fit/0.2"
    assert flagged["data"]["members"] == [
        {
            "member_ordinal": 0,
            "kind": "synthetic",
            "member_evidence": "asserted",
            "last_attempt_at": None,
        },
        {
            "member_ordinal": 1,
            "kind": "synthetic",
            "member_evidence": "asserted",
            "last_attempt_at": None,
        },
        {
            "member_ordinal": 2,
            "kind": "account",
            "member_evidence": "authoritative",
            "last_attempt_at": "2026-07-12T18:00:00Z",
        },
    ]
    assert {
        key: value
        for key, value in flagged["data"].items()
        if key not in {"schema", "members"}
    } == {key: value for key, value in default["data"].items() if key != "schema"}
    assert flagged["completeness"] == default["completeness"]


def test_host_topology_requires_explicit_host_at_cli_boundary(tmp_path, capsys) -> None:
    setup(tmp_path)
    args = common(objective="min-copies", appids=(10,))
    args[args.index("online_coop")] = "remote_play_together"
    code, value, _ = invoke(tmp_path, capsys, *args)
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"
