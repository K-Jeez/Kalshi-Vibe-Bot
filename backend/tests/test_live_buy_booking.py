"""A filled live buy must be booked with the real Settings object."""

from __future__ import annotations

import asyncio
import inspect

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.fill_bookkeeping import apply_cumulative_fill
from src.bot.loop import monitor_positions, run_bot_loop
from src.config import Settings, settings
from src.database.models import Base, Position, Trade


def test_real_settings_books_a_filled_buy(tmp_path, monkeypatch):
    assert isinstance(settings, Settings)
    engine = create_engine(f"sqlite:///{tmp_path / 'book.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("src.activity_log.get_session_local", lambda: factory)
    db = factory()
    booked = apply_cumulative_fill(
        db,
        action="buy",
        side="NO",
        market_id="KXSERIEASCORE-26NOV22ATAINT-ATA1INT3",
        cumulative_qty=6,
        cumulative_fees=0.04,
        price=5.04 / 6,
        client_order_id="cid-ata-1",
        exchange_order_id="01a204fa-2c90-7874-80e3-f8e9ebe229d7",
        market_title="Atalanta score",
    )
    db.commit()
    pos = db.query(Position).one()
    trade = db.query(Trade).one()
    db.close()
    assert booked["ok"] is True
    assert pos.side == "NO"
    assert pos.quantity == 6
    assert pos.status == "open"
    assert abs(float(pos.fees_paid) - 0.04) < 1e-9
    assert trade.action == "buy"
    assert trade.quantity == 6
    assert trade.id.startswith("01a204fa-2c90-7874-80e3-f8e9ebe229d7")


def test_empty_database_still_imports_kalshi_holdings(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    called = {}

    async def _reconcile(*_args, **_kwargs):
        called["ran"] = True
        return (0, 1, 0, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(
        "src.reconcile.kalshi_live_sync.reconcile_live_positions_from_kalshi",
        _reconcile,
    )

    async def _go():
        await monitor_positions(object(), factory, None, settings, allow_exits=False, bot_state_label="stop")

    asyncio.run(_go())
    assert called.get("ran") is True
    source = inspect.getsource(run_bot_loop)
    assert source.index("reconcile_live_positions_from_kalshi") < source.index("run_parallel_cycles")
