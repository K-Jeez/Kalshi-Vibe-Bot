"""``GET /portfolio/balance`` is 1 req/s (Basic) with no burst — cache + coalesce under load."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.clients.kalshi_client import KalshiClient


def _client() -> KalshiClient:
    c = KalshiClient.__new__(KalshiClient)
    c.api_key = "test"
    c.base_url = "https://external-api.kalshi.com"
    c.private_key_path = ""
    c.private_key = None
    c._portfolio_balance_cache = None
    c._portfolio_balance_lock = asyncio.Lock()
    c._portfolio_balance_inflight = None
    c._PORTFOLIO_BALANCE_CACHE_TTL_SEC = 2.5
    c._PORTFOLIO_BALANCE_STALE_OK_SEC = 120.0
    c._PORTFOLIO_BALANCE_429_MAX_ATTEMPTS = 3
    return c


def _ok_response(balance_cents: int = 183, portfolio_value_cents: int = 0) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://external-api.kalshi.com/trade-api/v2/portfolio/balance"
    resp.json.return_value = {"balance": balance_cents, "portfolio_value": portfolio_value_cents}
    resp.raise_for_status = MagicMock()
    return resp


def _429_response() -> httpx.Response:
    req = httpx.Request("GET", "https://external-api.kalshi.com/trade-api/v2/portfolio/balance")
    return httpx.Response(429, request=req, json={"error": "too many requests"})


def test_get_portfolio_parses_cents():
    async def _run():
        c = _client()
        c._http_get_signed = AsyncMock(return_value=_ok_response(183, 50))
        out = await c.get_portfolio()
        assert out["cash"] == pytest.approx(1.83)
        assert out["positions_value"] == pytest.approx(0.50)
        assert out["portfolio_value"] == pytest.approx(2.33)
        assert c._http_get_signed.await_count == 1

    asyncio.run(_run())


def test_get_portfolio_uses_fresh_cache():
    async def _run():
        c = _client()
        c._http_get_signed = AsyncMock(return_value=_ok_response(200, 0))
        a = await c.get_portfolio()
        b = await c.get_portfolio()
        assert a["cash"] == b["cash"] == pytest.approx(2.0)
        assert c._http_get_signed.await_count == 1

    asyncio.run(_run())


def test_get_portfolio_coalesces_concurrent_calls():
    async def _run():
        c = _client()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_get(*_a, **_k):
            started.set()
            await release.wait()
            return _ok_response(100, 0)

        c._http_get_signed = AsyncMock(side_effect=slow_get)
        t1 = asyncio.create_task(c.get_portfolio())
        await started.wait()
        t2 = asyncio.create_task(c.get_portfolio())
        await asyncio.sleep(0.02)
        release.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1["cash"] == r2["cash"] == pytest.approx(1.0)
        assert c._http_get_signed.await_count == 1

    asyncio.run(_run())


def test_get_portfolio_429_then_success(monkeypatch):
    async def _run():
        c = _client()
        c._PORTFOLIO_BALANCE_429_MAX_ATTEMPTS = 2
        c._http_get_signed = AsyncMock(side_effect=[_429_response(), _ok_response(250, 0)])
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        out = await c.get_portfolio()
        assert out["cash"] == pytest.approx(2.50)
        assert c._http_get_signed.await_count == 2

    asyncio.run(_run())


def test_get_portfolio_429_serves_stale_cache(monkeypatch):
    async def _run():
        c = _client()
        c._http_get_signed = AsyncMock(return_value=_ok_response(300, 0))
        first = await c.get_portfolio()
        assert first["cash"] == pytest.approx(3.0)
        c._portfolio_balance_cache = (time.monotonic() - 10.0, dict(first))
        c._PORTFOLIO_BALANCE_CACHE_TTL_SEC = 0.01
        c._PORTFOLIO_BALANCE_429_MAX_ATTEMPTS = 2
        c._http_get_signed = AsyncMock(side_effect=[_429_response(), _429_response()])
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        out = await c.get_portfolio()
        assert out["cash"] == pytest.approx(3.0)

    asyncio.run(_run())
