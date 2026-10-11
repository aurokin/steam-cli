from __future__ import annotations

import os
import sys

import pytest

from steam_agent.process_environment import child_environment
from steam_agent.execution.linux_session import run_command


def test_native_child_loses_provider_key_but_keeps_desktop_context(monkeypatch) -> None:
    monkeypatch.setenv("STEAM_WEB_API_KEY", "synthetic-provider-canary")
    monkeypatch.setenv("DISPLAY", ":fixture")
    result = run_command([
        sys.executable, "-c",
        "import os; print('STEAM_WEB_API_KEY' in os.environ); print(os.environ['DISPLAY'])",
    ])

    assert result.returncode == 0
    assert result.stdout == "False\n:fixture\n"
    assert os.environ["STEAM_WEB_API_KEY"] == "synthetic-provider-canary"


def test_child_environment_filters_windows_case_variants_without_mutating_input() -> None:
    source = {"Steam_Web_Api_Key": "synthetic-canary", "XDG_RUNTIME_DIR": "/fixture"}
    result = child_environment(source=source, overrides={"HOME": "/private-fixture"})

    assert result == {"XDG_RUNTIME_DIR": "/fixture", "HOME": "/private-fixture"}
    assert source["Steam_Web_Api_Key"] == "synthetic-canary"


def test_native_environment_override_cannot_reintroduce_provider_key() -> None:
    with pytest.raises(ValueError, match="provider credentials"):
        child_environment(source={}, overrides={"STEAM_WEB_API_KEY": "synthetic-canary"})
