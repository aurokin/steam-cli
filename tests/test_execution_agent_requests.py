from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import steam_agent.execution.agent_requests as intake
from steam_agent.execution.agent_requests import DirectRequest, record_or_reuse
from steam_agent.execution.ledger import ExecutionLedger, LedgerError
from steam_agent.execution.policy import PolicyError


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[ExecutionLedger]:
    with_ledger = ExecutionLedger(tmp_path / "ledger.sqlite3")
    try:
        yield with_ledger
    finally:
        with_ledger.close()


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text('[grants]\nstop="confirm"\n[stop]\nallowed_appids=[480,481]\n')
    return path


@pytest.fixture
def config(tmp_path: Path) -> dict[str, str]:
    return {
        "library": str(tmp_path / "library"),
        "machine_id": "gaming",
        "account_alias": "primary",
        "steamcmd": "",
        "steam_login": "",
    }


def no_floor(_: Path, __: int | None) -> str | None:
    return None


def pending_request(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
) -> dict[str, object]:
    return record_or_reuse(
        ledger, config, policy_path, DirectRequest("stop", 480, "request-0001"),
        platform="linux", disk_floor=no_floor,
    )


@pytest.mark.parametrize("expired_from", ("pending_confirmation", "authorized"))
def test_expired_direct_request_keeps_receipt_and_exact_content_binding(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
    expired_from: str,
) -> None:
    first = pending_request(ledger, config, policy_path)
    operation_id = first["operation_id"]
    assert isinstance(operation_id, int)
    if expired_from == "authorized":
        nonce = first["nonce"]
        assert isinstance(nonce, str)
        ledger.confirm(nonce=nonce, actor="owner", execution_window_seconds=0)
    else:
        ledger._connection.execute(  # noqa: SLF001
            "UPDATE operations SET nonce_expires_at='2000-01-01T00:00:00Z'"
        )
        ledger._connection.commit()  # noqa: SLF001

    second = pending_request(ledger, config, policy_path)
    assert second == {"operation_id": operation_id, "state": "expired", "reused": True}
    assert ledger.active() is None
    with pytest.raises(LedgerError, match="different plan content"):
        record_or_reuse(
            ledger, config, policy_path, DirectRequest("stop", 481, "request-0001"),
            platform="linux", disk_floor=no_floor,
        )
    assert len(ledger.recent(10)) == 1


def test_reuse_returns_same_nonce_even_when_policy_is_unavailable(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
) -> None:
    first = pending_request(ledger, config, policy_path)
    policy_path.unlink()
    second = pending_request(ledger, config, policy_path)
    assert second["operation_id"] == first["operation_id"]
    assert second["nonce"] == first["nonce"]
    assert second["reused"] is True


@pytest.mark.parametrize("identity", ("account_alias", "machine_id"))
def test_config_identity_change_cannot_rebind_request_id(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
    identity: str,
) -> None:
    first = pending_request(ledger, config, policy_path)
    changed = {**config, identity: "other"}
    with pytest.raises(LedgerError, match="different plan content"):
        pending_request(ledger, changed, policy_path)
    assert ledger.active() is not None
    assert ledger.active().operation_id == first["operation_id"]


@pytest.mark.parametrize(
    ("platform", "steamcmd", "login", "alias"),
    (
        ("linux", "", "synthetic_login", "primary"),
        ("linux", "/synthetic/steamcmd", "", "primary"),
        ("linux", "/synthetic/steamcmd", "synthetic_login", "other"),
        ("win32", "/synthetic/steamcmd", "synthetic_login", "primary"),
    ),
)
@pytest.mark.parametrize("operation", ("install", "verify"))
def test_unprovisioned_content_is_refused_before_occupying_active_slot(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
    platform: str, steamcmd: str, login: str, alias: str, operation: str,
) -> None:
    policy_path.write_text(f'[grants]\n{operation}="allow"\n[limits]\nmin_free_gb=0\n')
    configured = {**config, "steamcmd": steamcmd, "steam_login": login}
    with pytest.raises(PolicyError) as failure:
        record_or_reuse(
            ledger, configured, policy_path,
            DirectRequest(operation, 480, "content-0001", account_alias=alias),
            platform=platform, disk_floor=no_floor,
        )
    assert "synthetic_login" not in str(failure.value)
    assert ledger.active() is None
    assert ledger.recent(10) == []


@pytest.mark.parametrize("operation", ("install", "verify"))
def test_existing_content_receipt_survives_missing_provisioning(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
    operation: str,
) -> None:
    policy_path.write_text(f'[grants]\n{operation}="confirm"\n')
    configured = {
        **config, "steamcmd": "/synthetic/steamcmd", "steam_login": "synthetic_login",
    }
    request = DirectRequest(operation, 480, "content-0001")
    first = record_or_reuse(
        ledger, configured, policy_path, request, platform="linux", disk_floor=no_floor,
    )
    policy_path.unlink()
    reused = record_or_reuse(
        ledger, config, policy_path, request, platform="win32", disk_floor=no_floor,
    )
    assert reused == {**first, "reused": True}
    active = ledger.active()
    assert active is not None
    assert active.operation_id == first["operation_id"]
    assert len(ledger.events(active.operation_id)) == 1


def test_stop_autoauthorizes_without_content_or_disk_floor(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
) -> None:
    policy_path.write_text('[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n')

    def unexpected_floor(_: Path, __: int | None) -> str | None:
        pytest.fail("stopping must not depend on free disk space")

    receipt = record_or_reuse(
        ledger, config, policy_path, DirectRequest("stop", 480, "stop-0001"),
        platform="linux", disk_floor=unexpected_floor,
    )
    assert receipt["state"] == "authorized"
    active = ledger.active()
    assert active is not None
    assert active.confirmation_actor is not None
    assert active.confirmation_actor.startswith("policy:")


def test_disk_floor_failure_keeps_launch_explicitly_confirmable(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
) -> None:
    policy_path.write_text(
        '[grants]\nlaunch="allow"\n[limits]\nmin_free_gb=25\n[launch]\nallowed_appids=[480]\n'
    )
    calls: list[tuple[Path, int | None]] = []

    def floor(library: Path, minimum: int | None) -> str | None:
        calls.append((library, minimum))
        return "below disk floor"

    receipt = record_or_reuse(
        ledger, config, policy_path, DirectRequest("launch", 480, "launch-0001"),
        platform="linux", disk_floor=floor,
    )
    assert receipt["state"] == "pending_confirmation"
    assert receipt["auto_confirm_denied"] == "below disk floor"
    assert calls == [(Path(config["library"]), 25)]
    nonce = receipt["nonce"]
    assert isinstance(nonce, str)
    ledger.confirm(nonce=nonce, actor="owner")
    active = ledger.active()
    assert active is not None
    assert active.state == "authorized"


def test_policy_revocation_between_intake_and_autoauthorization_keeps_nonce_pending(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path.write_text('[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n')
    original = intake.load_policy
    calls = 0

    def read(path: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            policy_path.write_text('[grants]\nstop="deny"\n')
        return original(path)

    monkeypatch.setattr(intake, "load_policy", read)
    receipt = pending_request(ledger, config, policy_path)
    assert calls == 2
    assert receipt["state"] == "pending_confirmation"
    assert "nonce" in receipt


@pytest.mark.parametrize(
    "direct_request",
    (
        DirectRequest("uninstall", 480, "request-0001"),
        DirectRequest("stop", 0, "request-0001"),
        DirectRequest("stop", 0x100000000, "request-0001"),
        DirectRequest("stop", True, "request-0001"),
        DirectRequest("stop", 480, "short"),
        DirectRequest("stop", 480, "réquest-0001"),
        DirectRequest("launch", 480, "request-0001", force=True),
        replace(DirectRequest("stop", 480, "request-0001"), force="true"),  # type: ignore[arg-type]
    ),
)
def test_invalid_direct_request_does_not_write_ledger(
    ledger: ExecutionLedger, config: dict[str, str], policy_path: Path,
    direct_request: DirectRequest,
) -> None:
    with pytest.raises(PolicyError):
        record_or_reuse(
            ledger, config, policy_path, direct_request, platform="linux", disk_floor=no_floor,
        )
    assert ledger.active() is None
    assert ledger.recent(10) == []
