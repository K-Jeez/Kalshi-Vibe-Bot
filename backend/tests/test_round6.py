"""Favorites window, flat stake, search off, and the daily AI budget."""

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.daily_budget import budget_status
from src.database.models import Base, DecisionLog
from src.decision_engine.strategy_gates import autonomous_buy_gate_failure
from src.decision_engine.strategy_math import whole_contracts_for_stake
from src.event_live_lookup import context_for_market
from src.vetting.screen import buyable_leg


def test_price_window_is_50_to_85_cents():
    assert autonomous_buy_gate_failure(
        side="YES", ai_yes_pct=80, edge_pct=10, entry_price_dollars=0.42
    )
    assert autonomous_buy_gate_failure(
        side="YES", ai_yes_pct=80, edge_pct=10, entry_price_dollars=0.90
    )
    assert (
        autonomous_buy_gate_failure(
            side="YES", ai_yes_pct=80, edge_pct=10, entry_price_dollars=0.72
        )
        is None
    )
    cheap = {
        "yes_bid": 0.38, "yes_ask": 0.42, "yes_spread": 0.04, "yes_ask_size": 20,
        "no_bid": 0.55, "no_ask": 0.60, "no_spread": 0.05, "no_ask_size": 5,
    }
    assert buyable_leg(cheap, min_upside=0.10, max_spread=0.15, min_top_size=1) == "NO"
    neither = {
        "yes_bid": 0.20, "yes_ask": 0.24, "yes_spread": 0.04, "yes_ask_size": 20,
        "no_bid": 0.70, "no_ask": 0.90, "no_spread": 0.05, "no_ask_size": 20,
    }
    assert buyable_leg(neither, min_upside=0.10, max_spread=0.15, min_top_size=1) == ""


def test_flat_stake_floors_and_does_not_round_up():
    assert whole_contracts_for_stake(3, 0.60, 100) == 5
    assert whole_contracts_for_stake(0.40, 0.60, 100) == 0
    assert whole_contracts_for_stake(3, 0.60, 0.50) == 0
    assert settings.flat_stake_dollars == 0
    assert whole_contracts_for_stake(__import__("src.decision_engine.strategy_math", fromlist=["stake_dollars_for_edge"]).stake_dollars_for_edge(100, 10), 0.50, 100) == 20
    assert settings.min_ai_win_prob_buy_side_pct == 70
    assert settings.vibe_live_search_enabled is False
    assert settings.daily_xai_budget_usd == 0
    assert settings.max_edge_to_buy_pct == 22


def test_search_off_never_calls_the_tool():
    class Client:
        calls = 0

        async def search_event_facts(self, *_a, **_k):
            self.calls += 1
            return {"facts": "score", "confirmed_empty": False, "client_error": None, "usage": None}

    client = Client()
    import asyncio

    result = asyncio.run(
        context_for_market(
            market={"id": "M", "event_ticker": "E"},
            fact={"skip": False, "context": "", "reason": ""},
            client=client,
            seconds_until=300,
            title="Game",
        )
    )
    assert result == ("", "")
    assert client.calls == 0


def test_daily_budget_counts_denver_spend_and_sets_the_note():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    noon = datetime(2026, 11, 16, 19, 0, tzinfo=timezone.utc)  # noon Denver (MST)
    db.add(
        DecisionLog(
            id="a",
            market_id="M",
            market_title="Game",
            decision="BUY_YES",
            xai_analysis='{"usage":{"estimated_usd":3.0}}',
            confidence=0.8,
            timestamp=noon,
        )
    )
    db.commit()
    from src.config import settings as app_settings
    app_settings.daily_xai_budget_usd = 3
    status = budget_status(db, noon)
    app_settings.daily_xai_budget_usd = 0
    assert status["reached"] is True
    assert status["note"] == "Daily AI budget reached, analysis resumes tomorrow"
    db.close()
