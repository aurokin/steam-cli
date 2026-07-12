import json

from steam_agent import cli
from steam_agent.storage import Storage


def invoke(tmp_path, capsys, *args: str):
    code = cli.main(["--data-dir", str(tmp_path), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def configured(tmp_path) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at="2026-07-12T04:00:00Z",
            source_kind="test",
        )


def test_feedback_cli_round_trip_and_user_abandoned_name(tmp_path, capsys) -> None:
    configured(tmp_path)
    code, rated, stderr = invoke(
        tmp_path, capsys, "feedback", "rate", "10", "--value", "liked"
    )
    assert code == 0 and stderr == ""
    assert rated["data"]["item"]["rating"] == "liked"
    assert rated["data"]["changes"][0]["changed"] is True
    assert rated["data"]["changes"][0]["event_id"] is not None

    code, abandoned, _ = invoke(tmp_path, capsys, "feedback", "abandon", "10")
    assert code == 0
    assert abandoned["data"]["item"]["play_state"] == "user_abandoned"

    code, queried, _ = invoke(tmp_path, capsys, "feedback", "query")
    assert code == 0
    assert queried["context"] == {
        "account_alias": "primary",
        "identifiers_included": False,
    }
    encoded = json.dumps(queried)
    assert "765611" not in encoded
    assert str(tmp_path) not in encoded

    code, cleared_rating, _ = invoke(
        tmp_path, capsys, "feedback", "rate", "10", "--clear"
    )
    assert code == 0
    assert cleared_rating["data"]["changes"][0]["field"] == "rating"
    assert cleared_rating["data"]["changes"][0]["changed"] is True
    code, cleared_state, _ = invoke(
        tmp_path, capsys, "feedback", "clear-state", "10"
    )
    assert code == 0
    assert cleared_state["data"]["item"] is None
    code, repeated_clear, _ = invoke(
        tmp_path, capsys, "feedback", "clear-state", "10"
    )
    assert code == 0
    assert repeated_clear["data"]["changes"] == [
        {"field": "play_state", "changed": False, "event_id": None, "trait": None}
    ]


def test_estimate_clear_trait_and_rules_cli(tmp_path, capsys) -> None:
    configured(tmp_path)
    code, estimate, _ = invoke(
        tmp_path,
        capsys,
        "feedback",
        "estimate",
        "10",
        "--minimum-session-minutes",
        "30",
        "--remaining-minutes",
        "90",
    )
    assert code == 0
    assert estimate["data"]["item"]["estimates"]["remaining_minutes"] == 90
    code, cleared, _ = invoke(
        tmp_path,
        capsys,
        "feedback",
        "estimate",
        "10",
        "--clear-remaining-minutes",
    )
    assert code == 0
    assert cleared["data"]["item"]["estimates"]["remaining_minutes"] is None
    assert invoke(
        tmp_path,
        capsys,
        "feedback",
        "trait",
        "10",
        "--trait",
        "user:relaxing",
        "--value",
        "present",
    )[0] == 0
    code, trait_cleared, _ = invoke(
        tmp_path,
        capsys,
        "feedback",
        "trait",
        "10",
        "--trait",
        "user:relaxing",
        "--clear",
    )
    assert code == 0
    trait_change = trait_cleared["data"]["changes"][0]
    assert trait_change["event_id"] is not None
    assert trait_change == {
        "field": "trait",
        "changed": True,
        "event_id": trait_change["event_id"],
        "trait": "user:relaxing",
    }
    code, rule, _ = invoke(
        tmp_path,
        capsys,
        "preferences",
        "rule",
        "set",
        "--trait",
        "user:relaxing",
        "--kind",
        "prefer",
        "--strength",
        "soft",
        "--weight",
        "80",
    )
    assert code == 0 and rule["data"]["rule"]["weight"] == 80
    assert invoke(tmp_path, capsys, "preferences", "rule", "list")[1]["data"]["rules"]
    assert invoke(
        tmp_path,
        capsys,
        "preferences",
        "rule",
        "remove",
        "--trait",
        "user:relaxing",
    )[1]["data"]["removed"] is True


def test_invalid_trait_and_missing_account_are_typed(tmp_path, capsys) -> None:
    configured(tmp_path)
    code, invalid, _ = invoke(
        tmp_path,
        capsys,
        "feedback",
        "trait",
        "10",
        "--trait",
        "provider:trait",
        "--value",
        "present",
    )
    assert code == 1
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"
    code, missing, _ = invoke(
        tmp_path, capsys, "feedback", "query", "--account", "missing"
    )
    assert code == 1
    assert missing["error"]["code"] == "ACCOUNT_NOT_CONFIGURED"
