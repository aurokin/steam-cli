from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.storage import Machine, OwnedObservation, Storage


NOW = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)


def invoke(tmp_path: Path, capsys: object, *arguments: str):
    code = cli.main(["--data-dir", str(tmp_path), *arguments])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def configure(tmp_path: Path, *, with_owned: bool = False) -> None:
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
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="owned-visible-v1",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        if with_owned:
            run = storage.begin_sync(
                provider="steam_web_api",
                capability="owned.visible.read",
                account_id=account.id,
                started_at=NOW,
            )
            storage.complete_owned_snapshot(
                run.id,
                (OwnedObservation(400, 0, "visible_owned", NOW, "Private title"),),
                base_retrieved_at=NOW,
                expanded_retrieved_at=NOW,
                base_reported_count=1,
                expanded_reported_count=1,
                completed_at=NOW,
            )


def create_profile(tmp_path: Path, capsys: object, ref: str) -> None:
    code, _, _ = invoke(
        tmp_path,
        capsys,
        "profiles",
        "create",
        ref,
        "--acknowledge-group-storage",
        "--acknowledge-backups",
    )
    assert code == 0


def declared_payload(appid: int) -> dict[str, object]:
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
            "known_slugs": ["online_co_op"],
            "unknown_ids": [],
            "source": "steam_store_appdetails",
            "numeric_ids": [38],
        },
        "genres": {
            "state": "undeclared",
            "source": "steam_store_appdetails",
            "items": [],
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


def seed_declared(tmp_path: Path, appid: int) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert account is not None
        run, _, _ = storage.begin_declared_app_sync(
            account_id=account.id,
            machine_id="local",
            demanded_appids=[appid],
            country="US",
            language="english",
            max_items=10,
            skip_fresh_terminal=True,
            started_at=NOW,
            disclosure_version="m5-v1",
        )
        storage.record_declared_app_result(
            run.id,
            account_id=account.id,
            appid=appid,
            state="ready",
            observed_at=NOW,
            facts=declared_payload(appid),
        )
        storage.finish_declared_app_sync(run.id, completed_at=NOW)


def test_profile_mutations_require_disclosure_and_destructive_confirmation(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    code, value, _ = invoke(tmp_path, capsys, "profiles", "create", "synthetic:Guest")
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"

    create_profile(tmp_path, capsys, "synthetic:Guest")
    code, value, _ = invoke(tmp_path, capsys, "profiles", "get", "synthetic:Guest")
    assert code == 0
    assert value["data"]["profile"] == {
        "ordinal": 0,
        "kind": "synthetic",
        "storage_acknowledged": True,
    }
    assert (
        "backups may retain group data"
        in value["context"]["backup_retention_warning"].casefold()
    )
    assert "guest" not in json.dumps(value).casefold()

    code, value, _ = invoke(tmp_path, capsys, "profiles", "delete", "synthetic:Guest")
    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"
    code, value, _ = invoke(
        tmp_path, capsys, "profiles", "delete", "synthetic:Guest", "--yes"
    )
    assert code == 0
    assert value["data"]["deleted"]["profile_removed"] is True
    assert value["completeness"]["warnings"] == [
        {
            "code": "BACKUP_RETENTION",
            "message": (
                "Local group data was removed, but replicas, snapshots, and "
                "user-controlled backups may retain copies."
            ),
        }
    ]


def test_group_ownership_uses_visible_positive_and_synthetic_assertions_only(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path, with_owned=True)
    create_profile(tmp_path, capsys, "synthetic:Guest")
    code, _, _ = invoke(
        tmp_path, capsys, "ownership", "set", "synthetic:Guest", "400", "owned"
    )
    assert code == 0

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "group",
        "ownership",
        "400",
        "401",
        "--member",
        "account:primary",
        "--member",
        "synthetic:Guest",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 0 and stderr == ""
    results = value["data"]["results"]
    assert results[0]["ownership"]["union"] == "owned"
    assert results[0]["ownership"]["intersection"] == "owned"
    assert results[1]["ownership"]["union"] == "unknown"
    rendered = json.dumps(value).casefold()
    assert "primary" not in rendered
    assert "guest" not in rendered
    assert "765611" not in rendered
    assert "steam_web_api" not in rendered
    assert (
        "backups may retain group data"
        in value["context"]["backup_retention_warning"].casefold()
    )


def test_stale_account_owned_rows_remain_unknown_for_copy_guarantees(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path, with_owned=True)
    create_profile(tmp_path, capsys, "synthetic:Guest")
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage._connection.execute(  # noqa: SLF001
            """UPDATE sync_runs SET started_at=?, completed_at=?
               WHERE capability='owned.visible.read'""",
            ("2026-07-01T00:00:00Z", "2026-07-01T00:00:01Z"),
        )
        storage._connection.commit()  # noqa: SLF001

    code, value, _ = invoke(
        tmp_path,
        capsys,
        "group",
        "ownership",
        "400",
        "--member",
        "account:primary",
        "--member",
        "synthetic:Guest",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 0
    assert value["data"]["results"][0]["ownership"]["members"][0]["state"] == "unknown"
    assert value["completeness"]["status"] == "partial"
    assert value["completeness"]["stale_capabilities"] == ["owned.visible.read"]


def test_group_with_only_unsynced_accounts_is_unavailable(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.configure_steam_account(
            alias="other",
            steam_id64="76561198999999998",
            configured_at=NOW,
        )

    code, value, _ = invoke(
        tmp_path,
        capsys,
        "group",
        "ownership",
        "400",
        "--member",
        "account:primary",
        "--member",
        "account:other",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 0
    assert value["completeness"]["status"] == "unavailable"
    assert value["completeness"]["missing_capabilities"] == ["owned.visible.read"]


@pytest.mark.parametrize(
    "copy_sources",
    [
        ("synthetic:Alpha",),
        tuple(f"synthetic:source{index}" for index in range(63)),
    ],
)
def test_group_ownership_rejects_unbounded_or_member_copy_sources_before_storage(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
    copy_sources: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        cli, "Storage", lambda *_args, **_kwargs: pytest.fail("storage was opened")
    )
    arguments = [
        "group",
        "ownership",
        "400",
        "--member",
        "synthetic:Alpha",
        "--member",
        "synthetic:Beta",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
    ]
    for source in copy_sources:
        arguments.extend(("--copy-source", source))

    code, value, _ = invoke(tmp_path, capsys, *arguments)

    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"


def test_group_eligibility_combines_mode_copies_players_and_policy(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    create_profile(tmp_path, capsys, "synthetic:Alpha")
    create_profile(tmp_path, capsys, "synthetic:Beta")
    for ref in ("synthetic:Alpha", "synthetic:Beta"):
        assert invoke(tmp_path, capsys, "ownership", "set", ref, "400", "owned")[0] == 0
        assert (
            invoke(tmp_path, capsys, "fact", "set", ref, "400", "players:max", "4")[0]
            == 0
        )
        assert (
            invoke(
                tmp_path, capsys, "fact", "set", ref, "400", "policy:user:ok", "present"
            )[0]
            == 0
        )
    seed_declared(tmp_path, 400)

    code, value, stderr = invoke(
        tmp_path,
        capsys,
        "group",
        "eligibility",
        "400",
        "--member",
        "synthetic:Alpha",
        "--member",
        "synthetic:Beta",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--mode",
        "online_coop",
        "--policy",
        "user:ok",
    )
    assert code == 0 and stderr == ""
    result = value["data"]["results"][0]
    assert result["copies"]["guarantee"] == "guaranteed"
    assert result["eligibility"]["state"] == "pass"
    assert [gate["state"] for gate in result["eligibility"]["gates"]] == [
        "pass",
        "pass",
        "pass",
    ]
    rendered = json.dumps(value).casefold()
    assert "alpha" not in rendered and "beta" not in rendered


def test_group_eligibility_reports_missing_declared_evidence_in_completeness(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    create_profile(tmp_path, capsys, "synthetic:Alpha")
    create_profile(tmp_path, capsys, "synthetic:Beta")

    code, value, _ = invoke(
        tmp_path,
        capsys,
        "group",
        "eligibility",
        "400",
        "--member",
        "synthetic:Alpha",
        "--member",
        "synthetic:Beta",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--mode",
        "online_coop",
    )

    assert code == 0
    assert value["completeness"]["status"] == "unavailable"
    assert value["completeness"]["missing_capabilities"] == ["discovery.declared.read"]
    assert value["data"]["results"][0]["eligibility"]["gates"][0]["state"] == "unknown"


def test_group_eligibility_propagates_stale_declared_facts(
    tmp_path: Path, capsys: object
) -> None:
    configure(tmp_path)
    create_profile(tmp_path, capsys, "synthetic:Alpha")
    create_profile(tmp_path, capsys, "synthetic:Beta")
    seed_declared(tmp_path, 400)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage._connection.execute(  # noqa: SLF001
            "UPDATE declared_app_current SET observed_at='2026-07-04T00:00:00Z'"
        )
        storage._connection.commit()  # noqa: SLF001

    code, value, _ = invoke(
        tmp_path,
        capsys,
        "group",
        "eligibility",
        "400",
        "--member",
        "synthetic:Alpha",
        "--member",
        "synthetic:Beta",
        "--account",
        "primary",
        "--machine",
        "local",
        "--country",
        "US",
        "--language",
        "english",
        "--mode",
        "online_coop",
    )

    assert code == 0
    assert value["completeness"]["status"] == "partial"
    assert value["completeness"]["stale_capabilities"] == ["discovery.declared.read"]
