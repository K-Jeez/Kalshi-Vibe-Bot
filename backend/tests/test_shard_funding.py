"""Shard funding moves only the buy shortfall, then the order uses that shard."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.order_outcome import classify_routing_error
from src.bot.shard_funding import fund_buy_shard, plan_shard_funding, reset_shard_funding_memory, transfer_client_id
from src.clients.kalshi_client import KalshiClient
from src.database.models import Base


@pytest.fixture(autouse=True)
def _isolated_shard_funding(monkeypatch):
    reset_shard_funding_memory()
    monkeypatch.setattr("src.bot.shard_funding.BALANCE_READ_MIN_INTERVAL_SEC", 0.0)
    yield
    reset_shard_funding_memory()


def _factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fund.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class _Client:
    def __init__(self, portfolio: dict):
        self.portfolio = portfolio
        self.transfers = []
        self.reads = 0

    async def get_portfolio(self, force_refresh: bool = False):
        self.reads += 1
        return dict(self.portfolio)

    async def transfer_event_contract_shards(self, **kwargs):
        self.transfers.append(kwargs)
        dest = kwargs["destination_shard"]
        amount = float(kwargs["amount_dollars"])
        shards = self.portfolio["shard_cash"]
        shards[dest] = float(shards.get(dest, 0.0)) + amount
        shards[kwargs["source_shard"]] = float(shards[kwargs["source_shard"]]) - amount
        self.portfolio["shard_cash"] = shards
        return {"ok": True, "ambiguous": False, "transfer_id": "tr-1", "status_code": 200}


def test_normalize_market_keeps_exchange_index_and_defaults_to_zero():
    client = KalshiClient.__new__(KalshiClient)
    sports = client._normalize_market({"ticker": "KXGAME", "exchange_index": 3, "status": "active"})
    plain = client._normalize_market({"ticker": "KXOTHER", "status": "active"})
    assert sports["exchange_index"] == 3
    assert plain["exchange_index"] is None


def test_balance_breakdown_sums_dollar_strings_into_total_cash():
    parsed = KalshiClient._parse_portfolio_balance_payload({
        "balance": 1150,
        "portfolio_value": 0,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": "10.0000"},
            {"exchange_index": 3, "balance": "1.5000"},
        ],
    })
    assert parsed["cash"] == 11.50
    assert parsed["shard_cash"] == {0: 10.0, 3: 1.5}
    assert round(sum(parsed["shard_cash"].values()), 2) == parsed["cash"]


def test_plan_pulls_shard_zero_first_then_the_largest():
    plan = plan_shard_funding(
        shard_cash={0: 0.20, 1: 9.0, 2: 5.0, 3: 0.0},
        target_shard=3,
        order_all_in=0.98,
        total_cash=14.20,
        bankroll=100.0,
    )
    assert plan["ok"] is True
    assert [(row["source_shard"], row["amount_dollars"]) for row in plan["transfers"]] == [
        (0, 0.20),
        (1, 0.80),
    ]


def test_funded_shard_does_not_transfer(tmp_path):
    client = _Client({"cash": 20.0, "shard_cash": {0: 10.0, 3: 5.0}})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.95,
            bankroll=100.60,
            ticker="KXNCAAMBSPREAD-26NOV11UMKCFAMU-UMKC4",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert client.transfers == []


def test_shortfall_transfers_the_exact_cent_amount(tmp_path):
    client = _Client({"cash": 20.50, "shard_cash": {0: 20.0, 3: 0.50}})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.95,
            bankroll=100.60,
            ticker="KXNCAAMBSPREAD-26NOV11UMKCFAMU-UMKC4",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert client.transfers == [{
        "source_shard": 0,
        "destination_shard": 3,
        "amount_dollars": 1.47,
        "client_transfer_id": client.transfers[0]["client_transfer_id"],
    }]
    assert len(client.transfers[0]["client_transfer_id"]) == 36


def test_insufficient_total_does_not_transfer(tmp_path):
    client = _Client({"cash": 0.40, "shard_cash": {0: 0.30, 3: 0.10}})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.95,
            bankroll=100.0,
            ticker="KX",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "total cash" in result["reason"]
    assert client.transfers == []


def test_transfer_above_the_entry_cap_is_refused(tmp_path):
    client = _Client({"cash": 100.0, "shard_cash": {0: 100.0, 3: 0.0}})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=10.0,
            bankroll=100.0,
            ticker="KX",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "2.5% entry cap" in result["reason"]
    assert client.transfers == []


def test_transfer_failure_skips_without_a_second_post(tmp_path):
    class Failing(_Client):
        async def transfer_event_contract_shards(self, **kwargs):
            self.transfers.append(kwargs)
            return {"ok": False, "ambiguous": False, "error": "HTTP 400: rejected", "status_code": 400}

    client = Failing({"cash": 20.0, "shard_cash": {0: 20.0, 3: 0.0}})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.00,
            bankroll=100.0,
            ticker="KX",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["reason"] == "shard 3 funding failed: HTTP 400: rejected"
    assert len(client.transfers) == 1


def test_ambiguous_transfer_rereads_balance_and_does_not_repost(tmp_path):
    class Ambiguous(_Client):
        async def transfer_event_contract_shards(self, **kwargs):
            self.transfers.append(kwargs)
            raise TimeoutError("timed out")

    client = Ambiguous({"cash": 20.0, "shard_cash": {0: 20.0, 3: 0.0}})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.00,
            bankroll=100.0,
            ticker="KX",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "funding failed" in result["reason"]
    assert len(client.transfers) == 1
    assert client.reads >= 2


def test_buy_posts_ticker_and_exchange_index():
    client = KalshiClient.__new__(KalshiClient)
    client.invalidate_market_detail_cache = lambda *_a, **_k: None
    client.invalidate_portfolio_balance_cache = lambda: None
    captured = {}

    async def post(_path, body, timeout=15.0):
        captured["body"] = body
        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {
            "order_id": "ord-1",
            "fill_count": "1.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.6500",
        }
        response.raise_for_status = MagicMock()
        return response

    client._http_post_signed_json = post

    async def run():
        return await client.place_buy_ioc_limit(
            "KXNCAAMBSPREAD-26NOV11UMKCFAMU-UMKC4",
            "YES",
            3,
            0.65,
            client_order_id="cid-1",
            exchange_index=3,
        )

    out = asyncio.run(run())
    assert out.get("error") is None
    assert captured["body"]["ticker"] == "KXNCAAMBSPREAD-26NOV11UMKCFAMU-UMKC4"
    assert captured["body"]["exchange_index"] == 3
    assert "market_ticker" not in captured["body"]

    async def omitted():
        return await client.place_buy_ioc_limit(
            "KXOTHER",
            "YES",
            1,
            0.40,
            client_order_id="cid-2",
            exchange_index=None,
        )

    asyncio.run(omitted())
    assert captured["body"]["ticker"] == "KXOTHER"
    assert "exchange_index" not in captured["body"]


def test_transfer_request_uses_centicents():
    client = KalshiClient.__new__(KalshiClient)
    client.invalidate_portfolio_balance_cache = lambda: None
    captured = {}

    async def post(_path, body, timeout=15.0):
        captured["path"] = _path
        captured["body"] = body
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"transfer_id": "tr-9"}
        return response

    client._http_post_signed_json = post

    async def run():
        return await client.transfer_event_contract_shards(
            source_shard=0,
            destination_shard=3,
            amount_dollars=1.47,
            client_transfer_id="11111111-1111-1111-1111-111111111111",
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert captured["path"] == "/trade-api/v2/portfolio/intra_exchange_instance_transfer"
    assert captured["body"] == {
        "source": "event_contract",
        "destination": "event_contract",
        "amount": 14700,
        "source_exchange_shard": 0,
        "destination_exchange_shard": 3,
        "client_transfer_id": "11111111-1111-1111-1111-111111111111",
    }


def test_stale_breakdown_does_not_transfer_twice_to_the_same_shard(tmp_path):
    class Stale(_Client):
        async def transfer_event_contract_shards(self, **kwargs):
            self.transfers.append(kwargs)
            return {"ok": True, "ambiguous": False, "transfer_id": "tr-1", "status_code": 200}

    client = Stale({"cash": 20.0, "shard_cash": {0: 20.0, 3: 0.0}, "balance_authoritative": True})

    async def run():
        first = await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.00,
            bankroll=100.0,
            ticker="KXFIRST",
            client_order_id="order-a",
            session_factory=_factory(tmp_path),
        )
        second = await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.00,
            bankroll=100.0,
            ticker="KXSECOND",
            client_order_id="order-b",
            session_factory=_factory(tmp_path),
        )
        return first, second

    first, second = asyncio.run(run())
    assert len(client.transfers) == 1
    assert first["ok"] is False
    assert second["ok"] is False
    assert "unconfirmed" in second["reason"] or "has not updated" in first["reason"]


def test_transfer_id_repeats_for_the_same_intent_and_amount():
    first = transfer_client_id(client_order_id="order-a", source_shard=0, destination_shard=3, amount_cents=147)
    again = transfer_client_id(client_order_id="order-a", source_shard=0, destination_shard=3, amount_cents=147)
    other = transfer_client_id(client_order_id="order-b", source_shard=0, destination_shard=3, amount_cents=147)
    assert first == again
    assert first != other


def test_stale_balance_cache_does_not_authorize_a_transfer(tmp_path):
    client = _Client({"cash": 0.0, "shard_cash": {}, "balance_authoritative": False})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=1.00,
            bankroll=100.0,
            ticker="KX",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert client.transfers == []
    assert "not authoritative" in result["reason"]


def test_buffer_does_not_push_the_transfer_past_the_cap(tmp_path):
    client = _Client({"cash": 100.0, "shard_cash": {0: 100.0, 3: 0.0}})

    async def run():
        return await fund_buy_shard(
            client,
            exchange_index=3,
            order_all_in=2.49,
            bankroll=100.0,
            ticker="KXCAP",
            client_order_id="order-cap",
            session_factory=_factory(tmp_path),
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert client.transfers[0]["amount_dollars"] == 2.50
    assert client.transfers[0]["amount_dollars"] <= 2.50


def test_shard_balance_404_is_still_classified():
    detail = (
        "HTTP 404: This account has no balance on the exchange index (shard) this market trades on. "
        "transfer funds with POST /portfolio/intra_exchange_instance_transfer"
    )
    assert classify_routing_error(detail, 404) == "exchange_shard_balance"
    assert classify_routing_error("Exchange user not found. Exchange Sharding", 404) == "exchange_shard_user"


def test_export_lists_shard_transfers_outside_realized_pnl(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE positions (
            id TEXT, market_id TEXT, event_ticker TEXT, side TEXT, quantity INT, entry_price REAL,
            entry_cost REAL, fees_paid REAL, realized_pnl REAL, exit_reason TEXT, status TEXT, closed_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("p1", "M1", "EV", "YES", 1, 0.4, 1.0, 0.02, 1.95, "stop_loss", "closed", "2026-11-11"),
    )
    conn.execute(
        """
        CREATE TABLE activity_events (
            id TEXT, run_id TEXT, ts_utc TEXT, event_type TEXT, market_id TEXT, action TEXT,
            limit_price REAL, order_status TEXT, detail TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO activity_events VALUES (?,?,?,?,?,?,?,?,?)",
        ("t1", "run", "2026-11-11T16:00:00", "shard_transfer", "KX", "transfer", 1.47, "accepted", "not P&L"),
    )
    conn.commit()
    conn.close()
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "reconcile_export_shards",
        Path(__file__).resolve().parents[1] / "tools" / "reconcile_export.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    report = mod.audit_database(db, cash_start=100.0, cash_end=101.95)
    assert report["components"]["shard_transfer_count"] == 1
    assert report["components"]["shard_transfer_dollars"] == 1.47
    assert "not profit or loss" in report["components"]["shard_transfer_note"]
    assert report["components"]["sum_closed_realized_pnl"] == 1.95
