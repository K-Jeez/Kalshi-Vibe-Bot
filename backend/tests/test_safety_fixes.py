"""Regression tests for accidental-risk fixes and the activity trace."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.activity_log import (
    classify_order_result,
    fresh_unresolved_order_hold,
    persist_order_intent,
    record_activity,
    record_order_ack,
    scrub_text,
)
from src.api.local_guard import client_is_local, mutation_is_allowed, path_is_protected
from src.bot.loop import (
    losing_exit_cooldown_market_ids,
    monitor_positions,
    reentry_cooldown_minutes_from_settings,
)
from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import _parse_event_batch_json
from src.config import Settings
from src.database.models import ActivityEvent, Base, Position
from src.decision_engine.strategy_math import kelly_contracts_for_order, kelly_order_skip_summary
from src.util.datetimes import utc_now


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'act.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session


def test_reentry_reads_minutes_not_hours():
    settings = SimpleNamespace(reentry_cooldown_minutes=120, reentry_cooldown_hours=48)
    assert reentry_cooldown_minutes_from_settings(settings) == 120
    assert reentry_cooldown_minutes_from_settings(SimpleNamespace()) == 0


def test_reentry_window_is_minutes():
    now = utc_now()
    rows = [
        ("KXTEST-1", now - timedelta(minutes=30), -2.0),
        ("KXTEST-2", now - timedelta(hours=3), -2.0),
        ("KXTEST-3", now - timedelta(minutes=10), 1.5),
    ]
    held = losing_exit_cooldown_market_ids(rows, now=now, cooldown_minutes=120)
    assert held == {"KXTEST-1"}


def test_stake_cap_does_not_expand_to_full_cash_and_still_allows_one_contract():
    # Cap cannot buy one contract: skip, even though cash could buy several.
    assert kelly_contracts_for_order(
        10.46,
        "YES",
        80,
        0.67,
        0.40,
        0.67,
        0.40,
        per_contract_premium=0.67,
        max_kelly_contracts=0,
    ) == (0, "none")
    summary = kelly_order_skip_summary(
        10.46,
        "YES",
        80,
        0.67,
        0.40,
        0.67,
        0.40,
        per_contract_premium=0.67,
        max_kelly_contracts=0,
    )
    assert summary is not None and "not enlarged to full cash" in summary

    # When the cap can fund one contract, a positive fractional Kelly still buys one.
    qty, tag = kelly_contracts_for_order(
        10.0,
        "YES",
        42,
        0.40,
        0.60,
        0.40,
        0.60,
        per_contract_premium=0.40,
        max_kelly_contracts=1,
    )
    assert (qty, tag) == (1, "single_contract_retry")


def test_invalid_batch_market_id_does_not_keep_the_side():
    allowed = {"LEG-A", "LEG-B"}
    content = json.dumps(
        {
            "best_market_id": "NOT-IN-BATCH",
            "direction": "YES",
            "ai_probability_yes_pct": 80,
            "reasoning": "buy the missing id",
            "real_time_context": "x",
            "key_factors": [],
            "evidence": [],
        }
    )
    parsed = _parse_event_batch_json(content, allowed_ids=allowed)
    assert parsed["direction"] == "SKIP"
    assert parsed["best_market_id"] == ""
    assert parsed["best_market_id"] not in allowed
    assert parsed.get("batch_market_id_rejected") is True
    assert "LEG-A" not in parsed["best_market_id"]


def test_host_default_is_localhost_only():
    assert Settings.model_fields["host"].default == "127.0.0.1"


def test_remote_mutation_requires_token_and_local_dashboard_does_not():
    assert client_is_local("127.0.0.1")
    assert client_is_local("::1")
    assert path_is_protected("POST", "/bot/state")
    assert path_is_protected("POST", "/positions/abc/close")
    assert not path_is_protected("GET", "/portfolio")
    assert mutation_is_allowed(host="127.0.0.1", headers={}, configured_token="")
    assert not mutation_is_allowed(host="192.168.1.20", headers={}, configured_token="")
    assert not mutation_is_allowed(
        host="192.168.1.20",
        headers={"x-vibe-bot-token": "nope"},
        configured_token="secret-token",
    )
    assert mutation_is_allowed(
        host="192.168.1.20",
        headers={"x-vibe-bot-token": "secret-token"},
        configured_token="secret-token",
    )

    app = FastAPI()
    from src.api.local_guard import LocalMutationGuard

    app.add_middleware(LocalMutationGuard)

    @app.post("/bot/state")
    def _state():
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/bot/state").status_code == 200


def test_activity_log_persists_intent_scrubs_secrets_and_blocks_unresolved(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    cid = persist_order_intent(
        {
            "market_id": "KXTEST-1",
            "side": "YES",
            "action": "buy",
            "quantity": 1,
            "limit_price": 0.4,
            "model_id": "grok-4.3",
            "ai_probability_yes_pct": 70,
            "detail": "token bearer SECRET and BEGIN PRIVATE KEY",
        },
        session_factory=factory,
    )
    assert cid
    db = factory()
    row = db.query(ActivityEvent).filter(ActivityEvent.client_order_id == cid).one()
    assert row.event_type == "order_intent"
    assert row.order_status == "pending"
    assert row.detail == "redacted"
    assert "SECRET" not in (row.detail or "")
    assert row.run_id
    db.close()

    hold = fresh_unresolved_order_hold("KXTEST-1", "buy", session_factory=factory)
    assert hold and "unresolved" in hold

    record_order_ack(
        {"error": "HTTP 400: no", "status_code": 400, "client_order_id": cid},
        {"market_id": "KXTEST-1", "action": "buy", "side": "YES", "client_order_id": cid},
        session_factory=factory,
    )
    assert fresh_unresolved_order_hold("KXTEST-1", "buy", session_factory=factory) is None

    def _fail(payload, session_factory=None):
        return False

    monkeypatch.setattr("src.activity_log.record_activity", _fail)
    assert persist_order_intent({"market_id": "KXTEST-2", "action": "buy"}, session_factory=factory) is None
    assert scrub_text("ok") == "ok"
    assert classify_order_result({"error": "timed out"}) == "ambiguous"
    assert classify_order_result({"error": "HTTP 400: rejected", "status_code": 400}) == "rejected"


def test_monitor_with_open_position_and_no_exit_does_not_raise(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    db = factory()
    db.add(
        Position(
            id="p1",
            market_id="KXTEST-1",
            market_title="Test market",
            side="YES",
            quantity=2,
            entry_price=0.5,
            entry_cost=1.0,
            current_price=0.5,
            estimated_price=0.5,
            status="open",
            trade_mode="live",
        )
    )
    db.commit()
    db.close()

    async def _reconcile(*_a, **_k):
        return (1, 0, 0, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(
        "src.reconcile.kalshi_live_sync.reconcile_live_positions_from_kalshi",
        _reconcile,
    )
    calls = []

    async def _broadcast(payload):
        calls.append(payload)

    settings = SimpleNamespace(
        exit_grace_minutes=10.0,
        stop_loss_drawdown_pct=0.8,
        stop_loss_selling_enabled=False,
    )

    asyncio.run(
        monitor_positions(
            kalshi_client=SimpleNamespace(),
            db_factory=factory,
            broadcast_fn=_broadcast,
            settings=settings,
            allow_exits=True,
        )
    )
    assert calls == []
    db = factory()
    pos = db.query(Position).one()
    assert pos.status == "open"
    db.close()


def test_buy_reuses_one_client_order_id_across_ticker_alias(monkeypatch):
    seen = []

    async def _post(self, path, body, timeout=15.0):
        seen.append(body["client_order_id"])
        request = httpx.Request("POST", "https://example.test" + path)
        if len(seen) == 1:
            response = httpx.Response(404, json={"error": {"message": "not found"}}, request=request)
            response.raise_for_status()
        response = httpx.Response(
            200,
            json={"order_id": "ord-1", "fill_count": "1.00", "average_fill_price": "0.40", "remaining_count": "0"},
            request=request,
        )
        return response

    monkeypatch.setattr(KalshiClient, "_http_post_signed_json", _post)
    client = KalshiClient("test-key", "/tmp/does-not-need-to-exist.pem")

    async def _run():
        return await client.place_buy_ioc_limit(
            "KXXTEST-1",
            "YES",
            1,
            0.40,
            client_order_id="cid-fixed",
        )

    result = asyncio.run(_run())
    assert seen == ["cid-fixed", "cid-fixed"]
    assert result.get("client_order_id") == "cid-fixed"
    assert result.get("error") is None
