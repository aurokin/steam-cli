from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.activity import ACTIVITY_DISCLOSURE_VERSION
from steam_agent.credentials import (
    CredentialError,
    InMemoryCredentialStore,
    SecretValue,
)
from steam_agent.steam_web_api import VisibleOwnedGame, VisibleOwnedSnapshot
from steam_agent.storage import OwnedObservation, Storage


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "steam"


def invoke(argv: list[str], capsys: object) -> tuple[int, dict[str, object], str]:
    code = cli.main(argv)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


class Client:
    def __init__(self, *, inaccessible: bool = False) -> None:
        self.calls: list[tuple[bool, bool]] = []
        self.inaccessible = inaccessible

    def fetch_visible_owned_games(
        self,
        *,
        steamid: str,
        api_key: SecretValue,
        include_appinfo: bool,
        include_played_free_games: bool,
    ) -> VisibleOwnedSnapshot:
        self.calls.append((include_appinfo, include_played_free_games))
        if self.inaccessible:
            return VisibleOwnedSnapshot(
                snapshot_state="data_inaccessible",
                games=(),
                reported_game_count=None,
                include_appinfo=include_appinfo,
                include_played_free_games=include_played_free_games,
            )
        games = (
            VisibleOwnedGame(
                appid=10,
                name="Owned",
                playtime_forever_minutes=0,
                playtime_windows_forever_minutes=None,
                playtime_mac_forever_minutes=None,
                playtime_linux_forever_minutes=None,
                last_played_unix=None,
            ),
        )
        if include_played_free_games:
            games += (
                VisibleOwnedGame(
                    appid=20,
                    name="Played Free",
                    playtime_forever_minutes=None,
                    playtime_windows_forever_minutes=None,
                    playtime_mac_forever_minutes=None,
                    playtime_linux_forever_minutes=None,
                    last_played_unix=None,
                ),
            )
        return VisibleOwnedSnapshot(
            snapshot_state="ready",
            games=games,
            reported_game_count=len(games),
            include_appinfo=include_appinfo,
            include_played_free_games=include_played_free_games,
        )


class OwnedOnlyThirtyClient(Client):
    def fetch_visible_owned_games(
        self,
        *,
        steamid: str,
        api_key: SecretValue,
        include_appinfo: bool,
        include_played_free_games: bool,
    ) -> VisibleOwnedSnapshot:
        self.calls.append((include_appinfo, include_played_free_games))
        game = VisibleOwnedGame(
            appid=30,
            name="Owned Only",
            playtime_forever_minutes=12,
            playtime_windows_forever_minutes=None,
            playtime_mac_forever_minutes=None,
            playtime_linux_forever_minutes=None,
            last_played_unix=None,
        )
        return VisibleOwnedSnapshot(
            snapshot_state="ready",
            games=(game,),
            reported_game_count=1,
            include_appinfo=include_appinfo,
            include_played_free_games=include_played_free_games,
        )


def configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, InMemoryCredentialStore]:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
        storage.upsert_credential_reference(
            provider=ref.provider,
            kind=ref.kind,
            profile_id=ref.profile_id,
            backend="os",
            configured_at=NOW,
        )
    credential_store = InMemoryCredentialStore()
    credential_store.put(ref, SecretValue("credential-long-enough"))
    monkeypatch.setattr(
        cli,
        "_credential_store",
        lambda backend, backend_locator=None: credential_store,
    )
    monkeypatch.setattr(cli, "_reserve_provider_request", lambda *args: True)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    return data_dir, credential_store


def test_first_owned_sync_requires_disclosure_then_queries(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    client = Client()
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: client)
    common = ["--data-dir", str(data_dir)]

    code, blocked, stderr = invoke(common + ["sync", "owned"], capsys)
    assert code == 1
    assert stderr == ""
    assert blocked["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"  # type: ignore[index]
    assert client.calls == []

    code, synced, stderr = invoke(
        common + ["sync", "owned", "--acknowledge-local-storage"], capsys
    )
    assert code == 0
    assert stderr == ""
    assert synced["data"]["records_seen"] == 2  # type: ignore[index]
    assert client.calls == [(True, False), (True, True)]

    code, queried, stderr = invoke(
        common + ["games", "query", "--scope", "owned"], capsys
    )
    assert code == 0
    assert stderr == ""
    items = queried["data"]["items"]  # type: ignore[index]
    assert [item["appid"] for item in items] == [10, 20]
    assert [item["inclusion_basis"] for item in items] == [
        "visible_owned",
        "played_free",
    ]
    assert [item["name"] for item in items] == ["Owned", "Played Free"]
    assert queried["data"]["source"]["provider"] == "steam_web_api"  # type: ignore[index]
    assert queried["data"]["source"]["include_appinfo"] is True  # type: ignore[index]
    assert queried["data"]["source"]["base"]["include_played_free_games"] is False  # type: ignore[index]
    assert "steam_id64" not in json.dumps(queried)


def test_failed_owned_sync_preserves_query_truth(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    common = ["--data-dir", str(data_dir)]
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client(inaccessible=True))

    code, failed, _ = invoke(common + ["sync", "owned"], capsys)
    assert code == 1
    assert failed["error"]["code"] == "OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT"  # type: ignore[index]

    code, queried, _ = invoke(common + ["games", "query", "--scope", "owned"], capsys)
    assert code == 0
    assert queried["completeness"]["status"] == "partial"  # type: ignore[index]
    assert [item["appid"] for item in queried["data"]["items"]] == [10, 20]  # type: ignore[index]


def test_account_data_delete_preserves_shared_key(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, credential_store = configured(tmp_path, monkeypatch)
    ref = cli._steam_credential_ref(data_dir / "steam-agent.sqlite3")
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)

    code, deleted, stderr = invoke(
        common
        + [
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--account",
            "primary",
            "--yes",
        ],
        capsys,
    )
    assert code == 0
    assert stderr == ""
    assert deleted["data"]["removed"] is True  # type: ignore[index]
    assert deleted["data"]["shared_credential_preserved"] is True  # type: ignore[index]
    assert credential_store.contains(ref)


def test_accounts_remove_routes_through_account_data_cleanup(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, credential_store = configured(tmp_path, monkeypatch)
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)

    code, removed, _ = invoke(
        common + ["accounts", "remove", "--alias", "primary", "--yes"], capsys
    )

    assert code == 0
    assert removed["data"]["removed"] is True  # type: ignore[index]
    assert removed["data"]["activity_observations_removed"] == 0  # type: ignore[index]
    assert removed["data"]["activity_current_removed"] == 0  # type: ignore[index]
    assert removed["data"]["achievement_demand_removed"] == 0  # type: ignore[index]
    assert removed["data"]["achievement_player_observations_removed"] == 0  # type: ignore[index]
    assert removed["data"]["achievement_player_current_removed"] == 0  # type: ignore[index]
    assert credential_store.contains(ref)
    with Storage(database) as storage:
        assert (
            storage._connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE provider = 'steam_web_api'"
            ).fetchone()[0]
            == 0
        )


def test_joined_library_keeps_owned_and_installed_distinct(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: OwnedOnlyThirtyClient())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)
    invoke(
        common
        + [
            "sync",
            "installed",
            "--machine",
            "fixture-machine",
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
        ],
        capsys,
    )

    code, queried, stderr = invoke(
        common
        + [
            "games",
            "query",
            "--scope",
            "library",
            "--machine",
            "fixture-machine",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    items = {item["appid"]: item for item in queried["data"]["items"]}  # type: ignore[index]
    assert items[10]["installed"] is True
    assert items[10]["visible_in_owned_games"] is False
    assert items[30]["installed"] is False
    assert items[30]["visible_in_owned_games"] is True
    assert items[10]["game_id"].startswith("game:")
    assert items[30]["game_id"].startswith("game:")
    assert items[10]["game_id"] != items[30]["game_id"]
    assert items[30]["playable_now"] is None
    assert "install_dir" not in json.dumps(queried)


def test_all_data_delete_removes_shared_local_key_and_reference(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, credential_store = configured(tmp_path, monkeypatch)
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)

    code, deleted, stderr = invoke(
        common
        + [
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--all",
            "--yes",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert deleted["data"]["accounts_removed"] == 1  # type: ignore[index]
    assert deleted["data"]["catalog_observations_removed"] == 0  # type: ignore[index]
    assert deleted["data"]["catalog_current_removed"] == 0  # type: ignore[index]
    assert deleted["data"]["catalog_sync_runs_removed"] == 0  # type: ignore[index]
    assert deleted["data"]["catalog_evidence_removed"] == 0  # type: ignore[index]
    assert deleted["data"]["local_credential_removed"] is True  # type: ignore[index]
    assert deleted["data"]["valve_key_revoked"] is False  # type: ignore[index]
    assert not credential_store.contains(ref)
    with Storage(database) as storage:
        assert storage.list_accounts() == []
        assert (
            storage.get_credential_reference(
                provider=ref.provider,
                kind=ref.kind,
                profile_id=ref.profile_id,
            )
            is None
        )


def test_all_data_delete_restores_key_when_database_transaction_fails(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, credential_store = configured(tmp_path, monkeypatch)
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)

    def fail_delete(self: Storage, **_: object) -> object:
        raise RuntimeError("injected deletion failure")

    monkeypatch.setattr(Storage, "delete_all_steam_account_data", fail_delete)
    code, value, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--all",
            "--yes",
        ],
        capsys,
    )

    assert code == 1
    assert value["error"]["code"] == "INTERNAL_ERROR"  # type: ignore[index]
    assert credential_store.contains(ref)
    with Storage(database) as storage:
        assert len(storage.list_accounts()) == 1


def test_all_data_delete_completes_when_key_is_already_absent(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, credential_store = configured(tmp_path, monkeypatch)
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    assert credential_store.delete(ref)

    code, deleted, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--all",
            "--yes",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert deleted["data"]["credential_already_absent"] is True  # type: ignore[index]
    with Storage(database) as storage:
        assert storage.list_accounts() == []
        assert (
            storage.get_credential_reference(
                provider=ref.provider,
                kind=ref.kind,
                profile_id=ref.profile_id,
            )
            is None
        )


def test_owned_and_library_table_formats_render(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)

    assert (
        cli.main(common + ["games", "query", "--scope", "owned", "--format", "table"])
        == 0
    )
    owned = capsys.readouterr()  # type: ignore[attr-defined]
    assert "APPID\tNAME\tVISIBLE\tBASIS\tPLAYTIME" in owned.out
    assert "\ttrue\tvisible_owned\t" in owned.out

    assert (
        cli.main(common + ["games", "query", "--scope", "library", "--format", "table"])
        == 0
    )
    library = capsys.readouterr()  # type: ignore[attr-defined]
    assert "APPID\tNAME\tVISIBLE\tINSTALLED\tTYPE\tPLAYTIME" in library.out


def test_owned_snapshot_ages_to_stale_and_one_source_join_is_partial(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(hours=25))
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert account is not None
        storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at=NOW + timedelta(hours=24, minutes=55),
        )

    _, owned, _ = invoke(common + ["games", "query", "--scope", "owned"], capsys)
    assert owned["completeness"]["status"] == "partial"  # type: ignore[index]
    assert "owned.visible.read" in owned["completeness"]["stale_capabilities"]  # type: ignore[index]

    _, library, _ = invoke(common + ["games", "query", "--scope", "library"], capsys)
    assert library["completeness"]["status"] == "partial"  # type: ignore[index]
    assert "installed.read" in library["completeness"]["missing_capabilities"]  # type: ignore[index]


def test_joined_library_describes_partial_installed_scan_as_incomplete(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)

    code, scanned, _ = invoke(
        common
        + [
            "sync",
            "installed",
            "--steam-root",
            str(FIXTURES / "problems" / "root"),
        ],
        capsys,
    )
    assert code == 0
    assert scanned["completeness"]["status"] == "partial"

    code, library, stderr = invoke(
        common + ["games", "query", "--scope", "library"], capsys
    )

    assert code == 0
    assert stderr == ""
    assert library["completeness"]["status"] == "partial"
    installed_snapshot = library["data"]["snapshots"]["installed"]
    assert installed_snapshot["last_attempt_status"] == "partial"
    messages = [
        warning["message"].lower() for warning in library["completeness"]["warnings"]
    ]
    assert any(
        "installed games synchronization was incomplete" in value for value in messages
    )
    assert all(
        "installed games synchronization failed" not in value for value in messages
    )


def test_joined_library_marks_old_running_installed_sync_abandoned(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: Client())
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "owned", "--acknowledge-local-storage"], capsys)
    invoke(
        common
        + [
            "sync",
            "installed",
            "--steam-root",
            str(FIXTURES / "valid" / "root"),
        ],
        capsys,
    )
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id="local",
            started_at=NOW - timedelta(minutes=16),
        )

    code, library, stderr = invoke(
        common + ["games", "query", "--scope", "library"], capsys
    )

    library_warning_codes = {
        warning["code"] for warning in library["completeness"]["warnings"]
    }
    assert code == 0
    assert stderr == ""
    assert library["completeness"]["status"] == "partial"
    assert "installed.read" in library["completeness"]["stale_capabilities"]
    assert "SYNC_ABANDONED" in library_warning_codes
    assert "SYNC_IN_PROGRESS" not in library_warning_codes

    code, installed, stderr = invoke(
        common + ["games", "query", "--scope", "installed"], capsys
    )

    direct_warning_codes = {
        warning["code"] for warning in installed["completeness"]["warnings"]
    }
    assert code == 0
    assert stderr == ""
    assert installed["completeness"]["status"] == "complete"
    assert direct_warning_codes == {"SYNC_IN_PROGRESS"}


def test_empty_account_target_cannot_trigger_all_data_delete(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, credential_store = configured(tmp_path, monkeypatch)
    ref = cli._steam_credential_ref(data_dir / "steam-agent.sqlite3")

    code, value, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--account",
            "",
            "--yes",
        ],
        capsys,
    )

    assert code == 2
    assert value["error"]["code"] == "INVALID_ARGUMENT"  # type: ignore[index]
    assert credential_store.contains(ref)


class MutatingDeleteStore(InMemoryCredentialStore):
    def delete(self, ref: object) -> bool:
        super().delete(ref)  # type: ignore[arg-type]
        raise CredentialError("CREDENTIAL_DELETE_FAILED")


class UnreadableStore(InMemoryCredentialStore):
    def resolve(self, ref: object) -> SecretValue | None:
        raise CredentialError("CREDENTIAL_READ_FAILED")


def test_all_delete_restores_key_when_backend_reports_after_mutation(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
        storage.upsert_credential_reference(
            provider=ref.provider,
            kind=ref.kind,
            profile_id=ref.profile_id,
            backend="os",
            configured_at=NOW,
        )
    store = MutatingDeleteStore()
    store.put(ref, SecretValue("credential-long-enough"))
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: store
    )

    code, value, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--all",
            "--yes",
        ],
        capsys,
    )

    assert code == 1
    assert value["error"]["code"] == "CREDENTIAL_DELETE_FAILED"  # type: ignore[index]
    assert store.contains(ref)
    with Storage(database) as storage:
        assert len(storage.list_accounts()) == 1


def test_all_delete_removes_unreadable_key_and_account_data(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
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

    code, value, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--all",
            "--yes",
        ],
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert value["data"]["local_credential_removed"] is True  # type: ignore[index]
    assert not store.contains(ref)
    with Storage(database) as storage:
        assert storage.list_accounts() == []


def test_all_delete_reports_split_state_when_unreadable_key_cannot_be_restored(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
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

    def fail_delete(self: Storage, **_: object) -> object:
        raise RuntimeError("injected deletion failure")

    monkeypatch.setattr(Storage, "delete_all_steam_account_data", fail_delete)

    code, value, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "data",
            "delete",
            "--provider",
            "steam-web-api",
            "--all",
            "--yes",
        ],
        capsys,
    )

    assert code == 1
    assert value["error"]["code"] == "CREDENTIAL_ROLLBACK_FAILED"  # type: ignore[index]
    assert not store.contains(ref)
    with Storage(database) as storage:
        assert len(storage.list_accounts()) == 1
        assert (
            storage.get_credential_reference(
                provider=ref.provider,
                kind=ref.kind,
                profile_id=ref.profile_id,
            )
            is not None
        )


OWNED_AT = NOW - timedelta(hours=2)


class PlaytimeClient(Client):
    def fetch_visible_owned_games(
        self,
        *,
        steamid: str,
        api_key: SecretValue,
        include_appinfo: bool,
        include_played_free_games: bool,
    ) -> VisibleOwnedSnapshot:
        self.calls.append((include_appinfo, include_played_free_games))
        games = (
            VisibleOwnedGame(
                appid=10,
                name="Never Played",
                playtime_forever_minutes=0,
                playtime_windows_forever_minutes=None,
                playtime_mac_forever_minutes=None,
                playtime_linux_forever_minutes=None,
                last_played_unix=None,
            ),
            VisibleOwnedGame(
                appid=20,
                name="Played",
                playtime_forever_minutes=120,
                playtime_windows_forever_minutes=None,
                playtime_mac_forever_minutes=None,
                playtime_linux_forever_minutes=None,
                last_played_unix=None,
            ),
        )
        if include_played_free_games:
            games += (
                VisibleOwnedGame(
                    appid=30,
                    name="Playtime Absent",
                    playtime_forever_minutes=None,
                    playtime_windows_forever_minutes=None,
                    playtime_mac_forever_minutes=None,
                    playtime_linux_forever_minutes=None,
                    last_played_unix=None,
                ),
            )
        return VisibleOwnedSnapshot(
            snapshot_state="ready",
            games=games,
            reported_game_count=len(games),
            include_appinfo=include_appinfo,
            include_played_free_games=include_played_free_games,
        )


def owned_synced(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Synchronize owned games observed two hours before the query clock."""

    data_dir, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: PlaytimeClient())
    monkeypatch.setattr(cli, "_utc_now", lambda: OWNED_AT)
    invoke(
        ["--data-dir", str(data_dir), "sync", "owned", "--acknowledge-local-storage"],
        capsys,
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    return data_dir


def seed_activity(
    data_dir: Path, games: tuple[dict[str, object], ...], observed_at: datetime
) -> int:
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert account is not None
        storage.record_activity_data_consent(
            account_id=account.id,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            accepted_at=observed_at,
            backups_acknowledged=True,
        )
        run = storage.begin_activity_sync(
            account_id=account.id,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            started_at=observed_at,
        )
        storage.complete_activity_snapshot(
            run.id,
            account_id=account.id,
            games=games,
            observed_at=observed_at,
            recent_observed_at=observed_at,
            completed_at=observed_at,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        return run.id


def playtime_states(envelope: dict[str, object]) -> dict[int, tuple[str, str]]:
    return {
        item["appid"]: (item["playtime_state"], item["playtime_reason"])
        for item in envelope["data"]["items"]  # type: ignore[index]
    }


def owned_items_by_appid(envelope: dict[str, object]) -> dict[int, dict[str, object]]:
    return {
        item["appid"]: item
        for item in envelope["data"]["items"]  # type: ignore[index]
    }


def test_owned_query_playtime_zero_vs_null_distinct(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)

    code, queried, stderr = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )

    assert code == 0
    assert stderr == ""
    assert playtime_states(queried) == {
        10: ("zero", "owned_zero_minutes"),
        20: ("positive", "owned_positive_minutes"),
        30: ("unknown", "owned_playtime_absent"),
    }
    assert queried["data"]["playtime_state_counts"] == {  # type: ignore[index]
        "zero": 1,
        "positive": 1,
        "unknown": 1,
    }
    assert queried["context"]["playtime_filter"] == "any"  # type: ignore[index]
    source = queried["data"]["source"]  # type: ignore[index]
    for item in owned_items_by_appid(queried).values():
        assert item["playtime_lineage"] == {
            "authority": "owned",
            "provider": source["provider"],
            "retrieved_at": source["expanded"]["retrieved_at"],
            "observed_at": item["observed_at"],
            "context": {
                "capability": "owned.visible.read",
                "field": "playtime_forever_minutes",
                "sync_run_id": source["sync_run_id"],
            },
            "support_level": source["support_level"],
            "evidence_ids": [f"owned:{item['evidence_ids'][0]}"],
        }


def test_owned_query_activity_newer_positive_upgrades_zero(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)
    activity_run_id = seed_activity(
        data_dir,
        (
            {"appid": 10, "playtime_forever_minutes": 45},
            {"appid": 30, "playtime_forever_minutes": 5},
        ),
        NOW - timedelta(hours=1),
    )

    code, queried, _ = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )

    assert code == 0
    assert playtime_states(queried) == {
        10: ("positive", "activity_newer_positive"),
        20: ("positive", "owned_positive_minutes"),
        30: ("positive", "activity_newer_positive"),
    }
    items = owned_items_by_appid(queried)
    observed_at = items[10]["playtime_lineage"]["observed_at"]  # type: ignore[index]
    for appid in (10, 30):
        assert items[appid]["playtime_lineage"] == {
            "authority": "activity",
            "provider": "steam_web_api",
            "retrieved_at": observed_at,
            "observed_at": observed_at,
            "context": {
                "capability": "activity.read",
                "field": "playtime_forever_minutes",
                "sync_run_id": activity_run_id,
            },
            "support_level": "official_documented",
            "evidence_ids": [f"activity:{activity_run_id}:{appid}"],
        }
    assert items[20]["playtime_lineage"]["authority"] == "owned"  # type: ignore[index]
    rendered_lineage = json.dumps(
        [item["playtime_lineage"] for item in items.values()], sort_keys=True
    )
    assert "76561198000000000" not in rendered_lineage


def test_owned_query_activity_older_than_owned_is_ignored(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)
    seed_activity(
        data_dir,
        ({"appid": 10, "playtime_forever_minutes": 45},),
        OWNED_AT - timedelta(hours=1),
    )

    code, queried, _ = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )

    assert code == 0
    assert playtime_states(queried)[10] == ("zero", "owned_zero_minutes")
    assert owned_items_by_appid(queried)[10]["playtime_lineage"][  # type: ignore[index]
        "authority"
    ] == "owned"


def test_owned_query_future_activity_is_ignored_after_clock_correction(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)
    seed_activity(
        data_dir,
        ({"appid": 10, "playtime_forever_minutes": 45},),
        NOW + timedelta(minutes=1),
    )

    code, queried, _ = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )

    assert code == 0
    assert playtime_states(queried)[10] == ("zero", "owned_zero_minutes")


def test_owned_query_activity_never_downgrades_positive(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)
    seed_activity(
        data_dir,
        ({"appid": 20, "playtime_forever_minutes": 0},),
        NOW - timedelta(minutes=1),
    )

    code, queried, _ = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )

    assert code == 0
    assert playtime_states(queried)[20] == ("positive", "owned_positive_minutes")


def test_owned_query_expired_activity_not_consulted(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)
    database = data_dir / "steam-agent.sqlite3"
    observed_at = NOW - timedelta(days=10)
    with Storage(database) as storage:
        account = storage.get_account("primary")
        assert account is not None
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="owned-v1",
            accepted_at=observed_at,
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
            [
                OwnedObservation(
                    appid=10,
                    name="Never Played",
                    playtime_forever_minutes=0,
                    inclusion_basis="visible_owned",
                    observed_at=observed_at,
                )
            ],
            base_retrieved_at=observed_at,
            expanded_retrieved_at=observed_at,
            base_reported_count=1,
            expanded_reported_count=1,
            completed_at=NOW,
        )
    # Newer than the owned observation, but past the activity hard retention.
    seed_activity(
        data_dir,
        ({"appid": 10, "playtime_forever_minutes": 45},),
        NOW - timedelta(days=8),
    )

    code, queried, _ = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )

    assert code == 0
    assert playtime_states(queried)[10] == ("zero", "owned_zero_minutes")


def test_owned_query_playtime_filter_zero_excludes_unknown_and_reports_counts_and_limitations(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)

    code, queried, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "owned",
            "--playtime",
            "zero",
        ],
        capsys,
    )

    assert code == 0
    assert [item["appid"] for item in queried["data"]["items"]] == [10]  # type: ignore[index]
    assert queried["data"]["playtime_state_counts"] == {  # type: ignore[index]
        "zero": 1,
        "positive": 1,
        "unknown": 1,
    }
    assert queried["context"]["playtime_filter"] == "zero"  # type: ignore[index]
    limitations = queried["data"]["limitations"]  # type: ignore[index]
    assert "never_played_list_is_a_lower_bound" in limitations
    assert "zero_recorded_minutes_is_not_proof_of_never_launched" in limitations

    code, unfiltered, _ = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )
    assert code == 0
    assert (
        "never_played_list_is_a_lower_bound"
        not in unfiltered["data"]["limitations"]  # type: ignore[index]
    )


def test_owned_query_marks_an_authoritative_filtered_projection_empty(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)
    seed_activity(
        data_dir,
        (
            {"appid": 10, "playtime_forever_minutes": 45},
            {"appid": 30, "playtime_forever_minutes": 5},
        ),
        NOW - timedelta(hours=1),
    )

    code, queried, stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "owned",
            "--playtime",
            "zero",
        ],
        capsys,
    )

    assert code == 0 and stderr == ""
    assert queried["data"]["items"] == []  # type: ignore[index]
    assert queried["data"]["empty"] is True  # type: ignore[index]
    assert queried["data"]["playtime_state_counts"] == {  # type: ignore[index]
        "zero": 0,
        "positive": 3,
        "unknown": 0,
    }


def test_owned_query_zero_filter_keeps_limitations_when_account_is_unavailable(
    tmp_path: Path,
    capsys: object,
) -> None:
    code, queried, stderr = invoke(
        [
            "--data-dir",
            str(tmp_path),
            "games",
            "query",
            "--scope",
            "owned",
            "--account",
            "missing",
            "--playtime",
            "zero",
        ],
        capsys,
    )

    assert code == 0 and stderr == ""
    assert queried["completeness"]["status"] == "unavailable"  # type: ignore[index]
    assert queried["data"]["limitations"][-2:] == [  # type: ignore[index]
        "never_played_list_is_a_lower_bound",
        "zero_recorded_minutes_is_not_proof_of_never_launched",
    ]


def test_owned_query_stale_owned_snapshot_yields_unknown_no_authoritative_snapshot(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(hours=25))

    code, queried, _ = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "owned"], capsys
    )

    assert code == 0
    assert set(playtime_states(queried).values()) == {
        ("unknown", "no_authoritative_snapshot")
    }
    assert {
        json.dumps(item["playtime_lineage"], sort_keys=True)
        for item in owned_items_by_appid(queried).values()
    } == {
        json.dumps(
            {
                "authority": "none",
                "provider": None,
                "retrieved_at": None,
                "observed_at": None,
                "context": {"required_capability": "owned.visible.read"},
                "support_level": None,
                "evidence_ids": [],
            },
            sort_keys=True,
        )
    }
    assert queried["data"]["playtime_state_counts"] == {  # type: ignore[index]
        "zero": 0,
        "positive": 0,
        "unknown": 3,
    }

    code, filtered, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "owned",
            "--playtime",
            "zero",
        ],
        capsys,
    )
    assert code == 0
    assert filtered["data"]["items"] == []  # type: ignore[index]
    assert filtered["data"]["empty"] is False  # type: ignore[index]


def test_playtime_flag_rejected_for_installed_scope(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)

    code, rejected, _ = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "installed",
            "--playtime",
            "zero",
        ],
        capsys,
    )

    assert code == 2
    assert rejected["error"]["code"] == "INVALID_ARGUMENT"  # type: ignore[index]


def test_playtime_any_is_a_noop_for_installed_scope(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = configured(tmp_path, monkeypatch)

    code, omitted, stderr = invoke(
        ["--data-dir", str(data_dir), "games", "query", "--scope", "installed"],
        capsys,
    )
    explicit_code, explicit, explicit_stderr = invoke(
        [
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "installed",
            "--playtime",
            "any",
        ],
        capsys,
    )

    assert code == explicit_code == 0
    assert stderr == explicit_stderr == ""
    omitted.pop("generated_at")
    explicit.pop("generated_at")
    assert explicit == omitted


def test_owned_playtime_table_output_is_path_free(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = owned_synced(tmp_path, capsys, monkeypatch)

    assert (
        cli.main(
            [
                "--data-dir",
                str(data_dir),
                "games",
                "query",
                "--scope",
                "owned",
                "--playtime",
                "zero",
                "--format",
                "table",
            ]
        )
        == 0
    )

    rendered = capsys.readouterr()  # type: ignore[attr-defined]
    assert "PLAYTIME_STATE\tPLAYTIME_REASON" in rendered.out
    assert "\tzero\towned_zero_minutes" in rendered.out
    assert str(tmp_path) not in rendered.out
