from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from steam_agent.credentials import SecretValue
from steam_agent.owned_library import OwnedSyncError, owned_item, sync_owned
from steam_agent.steam_web_api import (
    SteamApiError,
    VisibleOwnedGame,
    VisibleOwnedSnapshot,
)
from steam_agent.storage import Storage


START = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class FakeClient:
    def __init__(
        self,
        base: tuple[VisibleOwnedGame, ...],
        expanded: tuple[VisibleOwnedGame, ...],
        *,
        state: str = "ready",
    ) -> None:
        self.base = base
        self.expanded = expanded
        self.state = state
        self.calls: list[tuple[bool, bool]] = []

    def fetch_visible_owned_games(
        self,
        *,
        steamid: str,
        api_key: SecretValue,
        include_appinfo: bool,
        include_played_free_games: bool,
    ) -> VisibleOwnedSnapshot:
        assert steamid == "76561198000000000"
        assert api_key.reveal() == "credential-long-enough"
        self.calls.append((include_appinfo, include_played_free_games))
        games = self.expanded if include_played_free_games else self.base
        return VisibleOwnedSnapshot(
            snapshot_state=self.state,
            games=games if self.state == "ready" else (),
            reported_game_count=len(games) if self.state == "ready" else None,
            include_appinfo=include_appinfo,
            include_played_free_games=include_played_free_games,
        )


def _game(appid: int, name: str, playtime: int | None) -> VisibleOwnedGame:
    return VisibleOwnedGame(
        appid=appid,
        name=name,
        playtime_forever_minutes=playtime,
        playtime_windows_forever_minutes=None,
        playtime_mac_forever_minutes=None,
        playtime_linux_forever_minutes=None,
        last_played_unix=None,
    )


def _account(storage: Storage) -> object:
    account = storage.configure_steam_account(
        alias="primary",
        steam_id64="76561198000000000",
        configured_at=START,
    )
    storage.record_owned_data_consent(
        account_id=account.id,
        disclosure_version="2026-07-11.m2",
        accepted_at=START,
        backups_acknowledged=True,
    )
    return account


def test_owned_sync_classifies_differential_and_promotes(tmp_path: Path) -> None:
    client = FakeClient(
        (_game(10, "Owned", 0),),
        (_game(10, "Owned", 0), _game(20, "Played Free", None)),
    )
    gate_calls = 0

    def gate() -> None:
        nonlocal gate_calls
        gate_calls += 1

    with Storage(tmp_path / "db.sqlite3") as storage:
        account = _account(storage)
        result = sync_owned(
            storage,
            account_id=account.id,
            steamid=account.provider_account_id,
            api_key=SecretValue("credential-long-enough"),
            client=client,  # type: ignore[arg-type]
            request_gate=gate,
            clock=Clock(),
        )
        snapshot = storage.read_owned_snapshot(account.id)

    assert client.calls == [(True, False), (True, True)]
    assert gate_calls == 2
    assert result.visible_owned_count == 1
    assert result.played_free_count == 1
    assert [game.inclusion_basis for game in snapshot.games] == [
        "visible_owned",
        "played_free",
    ]
    assert snapshot.games[0].playtime_forever_minutes == 0
    assert snapshot.games[1].playtime_forever_minutes is None
    assert [game.name for game in snapshot.games] == ["Owned", "Played Free"]
    assert snapshot.latest_complete_provenance is not None
    assert snapshot.latest_complete_provenance.include_appinfo is True
    assert "account_id" not in owned_item(snapshot.games[0])


def test_inaccessible_sync_preserves_last_good(tmp_path: Path) -> None:
    good = FakeClient((_game(10, "Owned", 5),), (_game(10, "Owned", 5),))
    inaccessible = FakeClient((), (), state="data_inaccessible")
    clock = Clock()
    with Storage(tmp_path / "db.sqlite3") as storage:
        account = _account(storage)
        sync_owned(
            storage,
            account_id=account.id,
            steamid=account.provider_account_id,
            api_key=SecretValue("credential-long-enough"),
            client=good,  # type: ignore[arg-type]
            clock=clock,
        )
        with pytest.raises(
            OwnedSyncError, match="OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT"
        ):
            sync_owned(
                storage,
                account_id=account.id,
                steamid=account.provider_account_id,
                api_key=SecretValue("credential-long-enough"),
                client=inaccessible,  # type: ignore[arg-type]
                clock=clock,
            )
        snapshot = storage.read_owned_snapshot(account.id)

    assert inaccessible.calls == [(True, False)]
    assert [game.appid for game in snapshot.games] == [10]
    assert snapshot.latest is not None and snapshot.latest.status == "failed"
    assert snapshot.latest_complete is not None


def test_inconsistent_pair_never_promotes(tmp_path: Path) -> None:
    client = FakeClient((_game(10, "Owned", 5),), ())
    with Storage(tmp_path / "db.sqlite3") as storage:
        account = _account(storage)
        with pytest.raises(OwnedSyncError, match="PROVIDER_RESPONSE_INVALID"):
            sync_owned(
                storage,
                account_id=account.id,
                steamid=account.provider_account_id,
                api_key=SecretValue("credential-long-enough"),
                client=client,  # type: ignore[arg-type]
                clock=Clock(),
            )
        snapshot = storage.read_owned_snapshot(account.id)

    assert snapshot.games == ()
    assert snapshot.latest is not None and snapshot.latest.status == "failed"
    assert snapshot.latest_complete is None


class ErrorClient:
    def fetch_visible_owned_games(self, **_: object) -> VisibleOwnedSnapshot:
        raise SteamApiError("RATE_LIMITED", retryable=True)


def test_provider_errors_use_stable_public_codes(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account = _account(storage)
        with pytest.raises(OwnedSyncError, match="PROVIDER_RATE_LIMITED"):
            sync_owned(
                storage,
                account_id=account.id,
                steamid=account.provider_account_id,
                api_key=SecretValue("credential-long-enough"),
                client=ErrorClient(),  # type: ignore[arg-type]
                clock=Clock(),
            )
        snapshot = storage.read_owned_snapshot(account.id)

    assert snapshot.latest is not None
    assert snapshot.latest.error_code == "PROVIDER_RATE_LIMITED"
