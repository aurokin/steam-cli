from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from steam_agent.credentials import SecretValue
from steam_agent.steam_wishlist import WishlistCount, WishlistItem, WishlistItems
from steam_agent.storage import Storage, WishlistObservation
from steam_agent.wishlist_library import WishlistSyncError, sync_wishlist


T0 = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class Client:
    def __init__(self, items: tuple[WishlistItem, ...], count: int) -> None:
        self.items = items
        self.count = count
        self.calls: list[str] = []

    def fetch_items(self, **_: object) -> WishlistItems:
        self.calls.append("items")
        return WishlistItems("ready", self.items)

    def fetch_count(self, **_: object) -> WishlistCount:
        self.calls.append("count")
        return WishlistCount("ready", self.count)


class AmbiguousClient(Client):
    def __init__(self, endpoint: str) -> None:
        super().__init__((WishlistItem(20, 0, 200),), 1)
        self.endpoint = endpoint

    def fetch_items(self, **kwargs: object) -> WishlistItems:
        if self.endpoint == "items":
            self.calls.append("items")
            return WishlistItems("ambiguous")
        return super().fetch_items(**kwargs)

    def fetch_count(self, **kwargs: object) -> WishlistCount:
        if self.endpoint == "count":
            self.calls.append("count")
            return WishlistCount("ambiguous")
        return super().fetch_count(**kwargs)


def clock() -> object:
    values = iter(T0 + timedelta(seconds=value) for value in range(20))
    return lambda: next(values)


def configured(path: Path) -> tuple[Storage, int]:
    storage = Storage(path)
    account = storage.configure_steam_account(
        alias="primary",
        steam_id64="76561198000000000",
        configured_at=T0,
    )
    storage.record_wishlist_data_consent(
        account_id=account.id,
        disclosure_version="test",
        accepted_at=T0,
        backups_acknowledged=True,
    )
    return storage, account.id


def test_matching_pair_promotes_stable_id_and_no_raw_body(tmp_path: Path) -> None:
    storage, account_id = configured(tmp_path / "db.sqlite3")
    with storage:
        client = Client((WishlistItem(20, 2, 200), WishlistItem(10, 0, 100)), 2)
        result = sync_wishlist(
            storage,
            account_id=account_id,
            steamid="76561198000000000",
            api_key=SecretValue("secret"),
            client=client,  # type: ignore[arg-type]
            clock=clock(),  # type: ignore[arg-type]
        )
        snapshot = storage.read_wishlist_snapshot(account_id)

        assert result.item_count == 2
        assert client.calls == ["count", "items"]
        assert [game.appid for game in snapshot.games] == [10, 20]
        assert len(dict(snapshot.stable_game_ids_by_appid)[10]) == 36
        evidence = storage._connection.execute(  # noqa: SLF001
            "SELECT payload_json, context_json FROM evidence "
            "WHERE capability = 'wishlist.read' ORDER BY id"
        ).fetchall()
        assert len(evidence) == 2
        assert all(
            "response" not in row[0] and "response" not in row[1] for row in evidence
        )


def test_mismatch_and_ambiguous_pair_preserve_last_good(tmp_path: Path) -> None:
    storage, account_id = configured(tmp_path / "db.sqlite3")
    with storage:
        sync_wishlist(
            storage,
            account_id=account_id,
            steamid="76561198000000000",
            api_key=SecretValue("secret"),
            client=Client((WishlistItem(10, 0, 100),), 1),  # type: ignore[arg-type]
            clock=clock(),  # type: ignore[arg-type]
        )
        with pytest.raises(WishlistSyncError, match="PROVIDER_RESPONSE_INVALID"):
            sync_wishlist(
                storage,
                account_id=account_id,
                steamid="76561198000000000",
                api_key=SecretValue("secret"),
                client=Client((WishlistItem(20, 0, 200),), 2),  # type: ignore[arg-type]
                clock=clock(),  # type: ignore[arg-type]
            )
        snapshot = storage.read_wishlist_snapshot(account_id)
        assert [game.appid for game in snapshot.games] == [10]
        assert snapshot.latest is not None and snapshot.latest.status == "failed"
        assert snapshot.latest.error_code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("endpoint", ["count", "items"])
def test_ambiguous_response_on_either_endpoint_preserves_last_good(
    tmp_path: Path, endpoint: str
) -> None:
    storage, account_id = configured(tmp_path / "db.sqlite3")
    with storage:
        sync_wishlist(
            storage,
            account_id=account_id,
            steamid="76561198000000000",
            api_key=SecretValue("secret"),
            client=Client((WishlistItem(10, 0, 100),), 1),  # type: ignore[arg-type]
            clock=clock(),  # type: ignore[arg-type]
        )
        with pytest.raises(
            WishlistSyncError, match="WISHLIST_INACCESSIBLE_OR_AUTH_AMBIGUOUS"
        ):
            sync_wishlist(
                storage,
                account_id=account_id,
                steamid="76561198000000000",
                api_key=SecretValue("secret"),
                client=AmbiguousClient(endpoint),  # type: ignore[arg-type]
                clock=clock(),  # type: ignore[arg-type]
            )
        assert [
            game.appid for game in storage.read_wishlist_snapshot(account_id).games
        ] == [10]


def test_explicit_matching_zero_clears_projection_and_delete_reports_it(
    tmp_path: Path,
) -> None:
    storage, account_id = configured(tmp_path / "db.sqlite3")
    with storage:
        sync_wishlist(
            storage,
            account_id=account_id,
            steamid="76561198000000000",
            api_key=SecretValue("secret"),
            client=Client((WishlistItem(10, 0, 100),), 1),  # type: ignore[arg-type]
            clock=clock(),  # type: ignore[arg-type]
        )
        sync_wishlist(
            storage,
            account_id=account_id,
            steamid="76561198000000000",
            api_key=SecretValue("secret"),
            client=Client((), 0),  # type: ignore[arg-type]
            clock=clock(),  # type: ignore[arg-type]
        )
        assert storage.read_wishlist_snapshot(account_id).games == ()
        result = storage.delete_steam_account_data(account_id)
        assert result.account_removed is True
        assert result.wishlist_current_removed == 0


def test_older_completion_cannot_replace_or_prune_newer_snapshot(
    tmp_path: Path,
) -> None:
    storage, account_id = configured(tmp_path / "db.sqlite3")
    with storage:
        older = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=T0,
        )
        newer = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=T0 + timedelta(seconds=1),
        )
        storage.complete_wishlist_snapshot(
            newer.id,
            [WishlistObservation(20, 0, 200, T0)],
            item_list_retrieved_at=T0,
            item_count_retrieved_at=T0,
            item_list_reported_count=1,
            item_count_reported_count=1,
            completed_at=T0 + timedelta(seconds=2),
        )
        storage.complete_wishlist_snapshot(
            older.id,
            [WishlistObservation(10, 0, 100, T0)],
            item_list_retrieved_at=T0,
            item_count_retrieved_at=T0,
            item_list_reported_count=1,
            item_count_reported_count=1,
            completed_at=T0 + timedelta(seconds=3),
        )

        snapshot = storage.read_wishlist_snapshot(account_id)
        assert [game.appid for game in snapshot.games] == [20]
        assert snapshot.latest_complete is not None
        assert snapshot.latest_complete.id == newer.id
        assert snapshot.latest_complete_provenance is not None
        assert snapshot.latest_complete_provenance.sync_run_id == newer.id
