"""Shared fixtures for weekly-advisor tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from weekly_telemetry_aggregator.models import Period, StepFinish


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Drop-in httpx-ish client for source tests (status + json only)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout=None,
        **kwargs,
    ):
        self.calls.append((url, params, headers))
        return self.handler(url, params, headers)

    def close(self):
        pass


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def tzutc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def make_step(
    session_id: str,
    ts: datetime,
    model: str = "provider/m1",
    *,
    cost: float | None = 0.01,
    cache_read: float = 0,
    cache_write: float = 0,
    fresh: int = 100,
    out: int = 10,
    reason: int = 0,
    api: int = 1,
) -> StepFinish:
    return StepFinish(
        session_id=session_id,
        timestamp=ts,
        model=model,
        tokens_input=float(fresh),
        tokens_output=float(out),
        tokens_reasoning=float(reason),
        tokens_cache_read=float(cache_read),
        tokens_cache_write=float(cache_write),
        cost=cost,
    )


@pytest.fixture
def week() -> Period:
    end = datetime(2026, 8, 10, 6, 0, 0, tzinfo=UTC)
    return Period(start=end - timedelta(hours=168), end=end)
