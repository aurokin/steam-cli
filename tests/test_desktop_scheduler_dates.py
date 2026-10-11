from datetime import datetime, timezone
import os
import time

import pytest

from steam_agent.execution.desktop_bridge import _scheduler_run_epoch


@pytest.fixture
def mountain_local_time(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("controlled local timezone requires tzset")
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "MST7MDT,M3.2.0,M11.1.0")
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_scheduler_com_date_is_local_wall_time(mountain_local_time):
    # pywin32 labels the scheduler's local fields UTC, six hours too early.
    reported = datetime(2026, 10, 10, 21, 45, tzinfo=timezone.utc)
    assert _scheduler_run_epoch(reported) == datetime(2026, 10, 11, 3, 45, tzinfo=timezone.utc).timestamp()


def test_scheduler_ambiguous_local_time_uses_earliest_possible_run(mountain_local_time):
    reported = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)
    earliest = datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc).timestamp()
    assert _scheduler_run_epoch(reported) == earliest
    # A job created in the repeated hour cannot use the earlier run as proof.
    created_in_second_hour = datetime(2026, 11, 1, 8, 15, tzinfo=timezone.utc).timestamp()
    assert _scheduler_run_epoch(reported) < created_in_second_hour
