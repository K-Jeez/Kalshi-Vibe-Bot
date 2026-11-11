"""SKIP still shows the YES ask, and series without a feed never call the model."""

from __future__ import annotations

import asyncio

from src.clients.xai_client import PROMPT_VERSION, SYSTEM_PROMPT, _parse_json
from src.decision_engine.analyzer import _enrich_strategy_fields, _parse_ai_yes_pct
from src.live_facts import live_fact_for_market, match_espn_event, series_of


def test_skip_keeps_the_yes_ask_and_zeroes_edge():
    prices = {"yes": 0.70, "no": 0.30, "yes_ask": 0.73, "no_ask": 0.30}
    fields = _enrich_strategy_fields(direction="SKIP", ai_yes_pct=50, current_prices=prices, bankroll=100)
    assert fields["market_implied_probability_pct"] == 73
    assert fields["edge_pct"] == 0.0
    assert fields["kelly_contracts"] == 0
    assert fields["ai_probability_yes_pct"] == 50


def test_missing_model_probability_uses_the_yes_ask():
    assert _parse_ai_yes_pct({}, {"yes": 0.4, "yes_ask": 0.41, "no": 0.6, "no_ask": 0.62}) == 41


def test_abstain_skips_without_replacing_a_stated_probability():
    parsed = _parse_json(
        '{"direction":"YES","ai_probability_yes_pct":73,"uncertainty":{"level":"abstain","missing":["rules"]}}'
    )
    assert parsed["direction"] == "SKIP"
    assert parsed["ai_probability_yes_pct"] == 73
    assert PROMPT_VERSION == "decision-2026-11-11"
    assert "never by itself a reason to SKIP" in SYSTEM_PROMPT
    assert "Never write 50" in SYSTEM_PROMPT


def test_pytest_database_url_is_not_the_operator_file():
    from src.config import settings

    assert "vibe-pytest-" in settings.database_url


def test_esoccer_is_prefiltered_and_ncaa_score_requires_both_names(monkeypatch):
    async def no_network(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr("src.live_facts._get_json", no_network)
    decision = asyncio.run(live_fact_for_market({"id": "KXESOCCERGAME-26NOV111307LEXILOTT-LEXI", "title": "Lexi"}))
    assert decision["skip"] is True
    assert series_of({"series_ticker": "KXETH15M", "id": "KXETH15M-1"}) == "KXETH15M"

    events = [
        {"competitions": [{"competitors": [
            {"team": {"displayName": "Incarnate Word"}, "score": "70"},
            {"team": {"displayName": "UTSA"}, "score": "68"},
        ], "status": {"type": {"shortDetail": "Final"}}}]},
        {"competitions": [{"competitors": [
            {"team": {"displayName": "Incarnate Word"}, "score": "1"},
            {"team": {"displayName": "Other"}, "score": "2"},
        ]}]},
    ]
    assert match_espn_event(events, "Incarnate Word vs UTSA")["competitions"][0]["competitors"][1]["score"] == "68"
    assert match_espn_event(events, "Incarnate Word vs Somebody Else") is None


def test_crypto_proxy_is_labeled_and_a_failed_spot_skips(monkeypatch):
    async def spot(url, headers=None):
        if "candles" in url:
            return [[0, 1, 2, 1, 100.0], [0, 1, 2, 1, 102.0], [0, 1, 2, 1, 101.0]]
        return {"data": {"amount": "101.5"}}

    monkeypatch.setattr("src.live_facts._get_json", spot)
    got = asyncio.run(live_fact_for_market({
        "series_ticker": "KXETH15M",
        "id": "KXETH15M-1",
        "title": "ETH above 100",
        "floor_strike": 100,
        "expires_in_days": 15 / 1440,
    }))
    assert got["skip"] is False
    assert "Coinbase" in got["context"]
    assert "not the CF Benchmarks" in got["context"]
    assert "101.5" in got["context"]

    async def down(*_args, **_kwargs):
        return None

    monkeypatch.setattr("src.live_facts._get_json", down)
    from src.live_facts import _cache
    _cache.clear()
    missed = asyncio.run(live_fact_for_market({"series_ticker": "KXBTCD", "id": "KXBTCD-1", "title": "BTC"}))
    assert missed["skip"] is True


def test_nws_observation_uses_the_station_in_the_rules(monkeypatch):
    async def nws(url, headers=None):
        assert "KMIA" in url
        return {"properties": {"temperature": {"value": 26.0}, "timestamp": "2099-01-01T00:00:00Z"}}

    monkeypatch.setattr("src.live_facts._get_json", nws)
    got = asyncio.run(live_fact_for_market({
        "series_ticker": "KXTEMPMIAH",
        "id": "KXTEMPMIAH-1",
        "title": "Miami high",
        "rules_primary": "Settlement uses the NWS station KMIA.",
        "floor_strike": 80,
        "expires_in_days": 1 / 24,
    }))
    assert got["skip"] is False
    assert "KMIA" in got["context"]
    assert "78.8 F" in got["context"]
    assert "official NWS" in got["context"]
    assert "2099-01-01T00:00:00Z" in got["context"]


def _team(display: str, location: str, nickname: str, abbreviation: str) -> dict:
    return {
        "team": {
            "displayName": display,
            "shortDisplayName": location,
            "location": location,
            "nickname": nickname,
            "name": nickname,
            "abbreviation": abbreviation,
        },
        "score": "0",
    }


def _event(*teams: dict) -> dict:
    return {"competitions": [{"competitors": list(teams), "status": {"type": {"shortDetail": "Final"}}}]}


def test_espn_match_rejects_near_name_collisions_and_keeps_university_at():
    georgia_tech = _event(
        _team("Georgia Tech Yellow Jackets", "Georgia Tech", "Yellow Jackets", "GT"),
        _team("Kentucky Wildcats", "Kentucky", "Wildcats", "UK"),
    )
    georgia = _event(
        _team("Georgia Bulldogs", "Georgia", "Bulldogs", "UGA"),
        _team("Kentucky Wildcats", "Kentucky", "Wildcats", "UK"),
    )
    utah_state = _event(
        _team("Utah State Aggies", "Utah State", "Aggies", "USU"),
        _team("Colorado State Rams", "Colorado State", "Rams", "CSU"),
    )
    utah = _event(
        _team("Utah Utes", "Utah", "Utes", "UTAH"),
        _team("Colorado Buffaloes", "Colorado", "Buffaloes", "COLO"),
    )
    miami_ohio = _event(
        _team("Miami Hurricanes", "Miami", "Hurricanes", "MIA"),
        _team("Miami (OH) RedHawks", "Miami (OH)", "RedHawks", "MOH"),
    )
    miami_only = _event(
        _team("Miami Hurricanes", "Miami", "Hurricanes", "MIA"),
        _team("Florida State Seminoles", "Florida State", "Seminoles", "FSU"),
    )
    albany = _event(
        _team("University at Albany Great Danes", "University at Albany", "Great Danes", "ALB"),
        _team("Binghamton Bearcats", "Binghamton", "Bearcats", "BING"),
    )
    wrong_albany = _event(
        _team("Albany Great Danes", "Albany", "Great Danes", "ALB"),
        _team("Binghamton Bearcats", "Binghamton", "Bearcats", "BING"),
    )
    assert match_espn_event([georgia_tech], "Georgia vs Kentucky") is None
    assert match_espn_event([georgia_tech, georgia], "Georgia vs Kentucky") is georgia
    assert match_espn_event([utah_state], "Utah vs Colorado") is None
    assert match_espn_event([utah_state, utah], "Utah vs Colorado") is utah
    assert match_espn_event([miami_only, miami_ohio], "Miami vs Miami (OH)") is miami_ohio
    assert match_espn_event([albany, wrong_albany], "University at Albany vs Binghamton") is albany
    assert match_espn_event([wrong_albany], "University at Albany vs Binghamton") is None
    assert match_espn_event([georgia], "Georgia vs Kentucky -3.5") is georgia
    assert match_espn_event([georgia], "Georgia vs Kentucky: Over 22.5 games") is georgia
    assert match_espn_event([georgia_tech], "Georgia vs Kentucky: Over 22.5") is None


def test_btc_ladder_keeps_each_legs_strike(monkeypatch):
    async def spot(url, headers=None):
        if "candles" in url:
            return [[0, 1, 2, 1, 100.0], [0, 1, 2, 1, 101.0], [0, 1, 2, 1, 102.0]]
        return {"data": {"amount": "100"}}

    monkeypatch.setattr("src.live_facts._get_json", spot)
    from src.live_facts import _cache
    _cache.clear()
    low = asyncio.run(live_fact_for_market({
        "series_ticker": "KXBTCD", "id": "KXBTCD-T100", "title": "BTC above 100", "floor_strike": 100,
    }))
    high = asyncio.run(live_fact_for_market({
        "series_ticker": "KXBTCD", "id": "KXBTCD-T110", "title": "BTC above 110", "floor_strike": 110,
    }))
    assert "Strike or threshold: 100.0000" in low["context"]
    assert "Strike or threshold: 110.0000" not in low["context"]
    assert "Strike or threshold: 110.0000" in high["context"]
    assert "Strike or threshold: 100.0000" not in high["context"]


def test_failed_spot_is_not_fetched_again_immediately(monkeypatch):
    calls = {"n": 0}

    async def down(url, headers=None):
        calls["n"] += 1
        return None

    monkeypatch.setattr("src.live_facts._get_json", down)
    from src.live_facts import _cache
    _cache.clear()
    first = asyncio.run(live_fact_for_market({"series_ticker": "KXBTCD", "id": "KXBTCD-1"}))
    after = calls["n"]
    second = asyncio.run(live_fact_for_market({"series_ticker": "KXBTCD", "id": "KXBTCD-2"}))
    assert first["skip"] and second["skip"]
    assert calls["n"] == after


def test_empty_prefilter_lists_disable_the_skip(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "vibe_prefilter_series_prefixes", "")
    monkeypatch.setattr(settings, "vibe_numeric_prefilter_series_prefixes", "")
    decision = asyncio.run(live_fact_for_market({"id": "KXESOCCERGAME-1", "series_ticker": "KXESOCCERGAME"}))
    assert decision["skip"] is False
    silver = asyncio.run(live_fact_for_market({"id": "KXSILVER15M-1", "series_ticker": "KXSILVER15M"}))
    assert silver["skip"] is False


def test_atp_and_wta_challenger_prefixes_are_prefiltered():
    for series in ("KXATPCHALLENGER", "KXWTACHALLENGER"):
        decision = asyncio.run(live_fact_for_market({"id": f"{series}-1", "series_ticker": series}))
        assert decision["skip"] is True


def test_stale_nws_observation_is_dropped(monkeypatch):
    async def nws(url, headers=None):
        return {"properties": {"temperature": {"value": 10.0}, "timestamp": "2000-01-01T00:00:00Z"}}

    monkeypatch.setattr("src.live_facts._get_json", nws)
    from src.live_facts import _cache
    _cache.clear()
    got = asyncio.run(live_fact_for_market({
        "series_ticker": "KXTEMPMIAH",
        "id": "KXTEMPMIAH-OLD",
        "rules_primary": "Station KMIA.",
    }))
    assert got["skip"] is True
    assert got["context"] == ""
