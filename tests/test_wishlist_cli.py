from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.credentials import InMemoryCredentialStore, SecretValue
from steam_agent.steam_wishlist import WishlistCount, WishlistItem, WishlistItems
from steam_agent.storage import Storage


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class Client:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.ambiguous = ambiguous
        self.calls: list[str] = []

    def fetch_items(self, **_: object) -> WishlistItems:
        self.calls.append("items")
        if self.ambiguous:
            return WishlistItems("ambiguous")
        return WishlistItems(
            "ready",
            (WishlistItem(20, 2, 200), WishlistItem(10, 0, 100)),
        )

    def fetch_count(self, **_: object) -> WishlistCount:
        self.calls.append("count")
        return WishlistCount(
            "ambiguous" if self.ambiguous else "ready", None if self.ambiguous else 2
        )


def invoke(argv: list[str], capsys: object) -> tuple[int, dict[str, object], str]:
    code = cli.main(argv)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    return data_dir


def test_wishlist_sync_query_last_good_freshness_and_deletion(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = configured(tmp_path, monkeypatch)
    client = Client()
    monkeypatch.setattr(cli, "_steam_wishlist_client", lambda: client)
    common = ["--data-dir", str(data_dir)]

    code, blocked, stderr = invoke(common + ["sync", "wishlist"], capsys)
    assert code == 1 and stderr == ""
    assert blocked["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"  # type: ignore[index]
    assert client.calls == []

    code, synced, _ = invoke(
        common + ["sync", "wishlist", "--acknowledge-local-storage"], capsys
    )
    assert code == 0
    assert synced["data"]["wishlist_count"] == 2  # type: ignore[index]
    assert client.calls == ["count", "items"]

    code, queried, _ = invoke(
        common + ["games", "query", "--scope", "wishlist"], capsys
    )
    assert code == 0
    assert queried["completeness"]["status"] == "complete"  # type: ignore[index]
    assert [item["appid"] for item in queried["data"]["items"]] == [10, 20]  # type: ignore[index]
    assert queried["data"]["items"][0]["priority"] == 0  # type: ignore[index]
    assert "76561198000000000" not in json.dumps(queried)

    code = cli.main(
        common + ["games", "query", "--scope", "wishlist", "--format", "table"]
    )
    table = capsys.readouterr().out  # type: ignore[attr-defined]
    assert code == 0
    assert "APPID\tWISHLISTED\tPRIORITY\tDATE_ADDED" in table

    monkeypatch.setattr(cli, "_steam_wishlist_client", lambda: Client(ambiguous=True))
    code, failed, _ = invoke(common + ["sync", "wishlist"], capsys)
    assert code == 1
    assert failed["error"]["code"] == "WISHLIST_INACCESSIBLE_OR_AUTH_AMBIGUOUS"  # type: ignore[index]
    code, retained, _ = invoke(
        common + ["games", "query", "--scope", "wishlist"], capsys
    )
    assert retained["completeness"]["status"] == "partial"  # type: ignore[index]
    assert [item["appid"] for item in retained["data"]["items"]] == [10, 20]  # type: ignore[index]

    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(hours=25))
    _, stale, _ = invoke(common + ["games", "query", "--scope", "wishlist"], capsys)
    assert "wishlist.read" in stale["completeness"]["stale_capabilities"]  # type: ignore[index]

    code, deleted, _ = invoke(
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
    assert deleted["data"]["wishlist_current_removed"] == 2  # type: ignore[index]
