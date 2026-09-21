"""Guards for the live Jev suite.

These tests call the real API, which costs money and needs a key. They are
excluded from the default run by `addopts = -m "not live"` in pytest.ini and
skip entirely when no key is present, so `pytest -q` never reaches them.

A hard cap on calls per session is enforced here rather than left to each
test, so a loop bug cannot run up a bill.
"""

from __future__ import annotations

import os

import pytest

MAX_CALLS = int(os.environ.get("SPEND_GUARD_LIVE_MAX_CALLS", "20"))


class CallBudget:
    """Counts calls across the whole session and fails once the cap is hit."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.latencies_ms: list[float] = []

    def spend(self, count: int = 1) -> None:
        if self.used + count > self.limit:
            pytest.fail(
                f"live call budget exhausted: {self.used}/{self.limit} used. "
                "Raise SPEND_GUARD_LIVE_MAX_CALLS deliberately if this is expected."
            )
        self.used += count

    def record(self, latency_ms: float) -> None:
        self.latencies_ms.append(latency_ms)


@pytest.fixture(scope="session")
def budget() -> CallBudget:
    return CallBudget(MAX_CALLS)


@pytest.fixture(scope="session")
def api_key() -> str | None:
    """The key, or None when an outbound proxy attaches it instead.

    Two ways to reach Jev from a sandbox:

    * `TYPESAFE_API_KEY` - this process holds the key and sets the header.
    * `SPEND_GUARD_JEV_AUTH=proxy` - a proxy attaches the credential after the
      request leaves, so the key never enters the sandbox at all. Preferred:
      nothing running here can read it.
    """
    from spend_guard.judges.jev import AUTH_PROXY, auth_mode

    if auth_mode() == AUTH_PROXY:
        return None
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        pytest.skip("set TYPESAFE_API_KEY, or SPEND_GUARD_JEV_AUTH=proxy to let a proxy attach it")
    return key


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Print the observed latency spread; the live suite exists to measure it."""
    budget = getattr(session, "_spend_guard_budget", None)
    if budget and budget.latencies_ms:
        ordered = sorted(budget.latencies_ms)
        p50 = ordered[len(ordered) // 2]
        print(
            f"\n[live] calls={budget.used} p50={p50:.0f}ms max={ordered[-1]:.0f}ms"
        )
