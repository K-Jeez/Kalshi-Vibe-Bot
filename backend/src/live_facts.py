"""Live numbers for short-horizon markets, and a pre-filter when none can be supplied.

Crypto uses a public Coinbase spot as a proxy for the CF Benchmarks print Kalshi
settles. Temperature uses the NWS observation for the station named on the
contract. Sports scores come from the public ESPN scoreboard only when both
team names match one event. A fetch failure or an unmatched game does not
invent a fact. Series with no source are skipped before the model call.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.config import settings
from src.logger import setup_logging

logger = setup_logging("live_facts")

_CACHE_SEC = 20.0
_NEG_CACHE_SEC = 45.0
_NWS_MAX_AGE_SEC = 90 * 60
_TIMEOUT = 2.5
_SKIP_LOG_SEC = 15 * 60
_MISS = object()
_cache: dict[str, tuple[float, object]] = {}
_skip_logged: dict[str, float] = {}

# Public spot proxy. Not the CF Benchmarks settlement index.
_CRYPTO = {
    "KXBTCD": "BTC-USD",
    "KXBTC": "BTC-USD",
    "KXETH": "ETH-USD",
    "KXSOL": "SOL-USD",
    "KXDOGE": "DOGE-USD",
    "KXXRP": "XRP-USD",
}
_TEMP = ("KXTEMP", "KXHIGH", "KXLOW")
_ESPN = {
    "KXNCAAM": ("basketball", "mens-college-basketball"),
    "KXNCAAW": ("basketball", "womens-college-basketball"),
    "KXNBA": ("basketball", "nba"),
    "KXNFL": ("football", "nfl"),
    "KXNHL": ("hockey", "nhl"),
}
_DEFAULT_NO_SOURCE = (
    "KXESOCCER",
    "KXCS2",
    "KXCSGO",
    "KXDOTA",
    "KXVALORANT",
    "KXLOL",
    "KXITF",
    "KXCHALLENGER",
    "KXATPCHALLENGER",
    "KXWTACHALLENGER",
)
_DEFAULT_NUMERIC_NO_SOURCE = ("KXSILVER", "KXGOLD", "KXINX", "KXNDX", "KXSPX")
_ICAO = re.compile(r"\b(K[A-Z]{3})\b")
_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)")


def series_of(market: dict) -> str:
    explicit = str(market.get("series_ticker") or "").strip().upper()
    if explicit:
        return explicit
    raw = str(market.get("id") or market.get("market_id") or "").strip().upper()
    return raw.split("-", 1)[0]


def _prefixes(raw: Optional[str], fallback: tuple[str, ...]) -> tuple[str, ...]:
    """None means the built-in list. An empty string means no prefixes."""
    if raw is None:
        return fallback
    text = str(raw).strip()
    if not text:
        return ()
    return tuple(part.strip().upper() for part in text.split(",") if part.strip())


def _starts(series: str, prefixes: tuple[str, ...]) -> bool:
    return any(series.startswith(prefix) for prefix in prefixes)


def note_live_fact_skip(series: str, reason: str) -> None:
    now = time.monotonic()
    if now - _skip_logged.get(series, 0.0) < _SKIP_LOG_SEC:
        return
    _skip_logged[series] = now
    logger.info("Skip model series %s — %s", series, reason)


def _cached(key: str):
    hit = _cache.get(key)
    if not hit:
        return None
    stored_at, value = hit
    ttl = _NEG_CACHE_SEC if value is _MISS else _CACHE_SEC
    if time.monotonic() - stored_at >= ttl:
        return None
    return value


def _store(key: str, value):
    _cache[key] = (time.monotonic(), value)
    return value


def _remember_miss(key: str) -> None:
    _store(key, _MISS)


async def _get_json(url: str, headers: Optional[dict] = None):
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            return None
        body = response.json()
        return body if isinstance(body, (dict, list)) else None
    except Exception:
        return None


def _strike(market: dict) -> Optional[float]:
    for key in ("floor_strike", "cap_strike"):
        raw = market.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    title = f"{market.get('title') or ''} {market.get('subtitle') or ''} {market.get('id') or ''}"
    numbers = _NUMBER.findall(title)
    if not numbers:
        return None
    try:
        return float(numbers[-1])
    except ValueError:
        return None


def _minutes_left(market: dict) -> Optional[int]:
    raw = market.get("expires_in_days")
    try:
        days = float(raw)
    except (TypeError, ValueError):
        return None
    if days < 0:
        return 0
    return int(days * 24 * 60)


def _fact_block(lines: list[str]) -> str:
    return "LIVE FACTS (server, not a model guess):\n" + "\n".join(lines)


async def _crypto_context(market: dict, product: str) -> Optional[str]:
    key = f"crypto:{product}"
    cached = _cached(key)
    if cached is _MISS:
        return None
    if cached is None:
        spot_body = await _get_json(f"https://api.coinbase.com/v2/prices/{product}/spot")
        candles = await _get_json(
            f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=60"
        )
        amount = None
        if isinstance(spot_body, dict):
            data = spot_body.get("data") if isinstance(spot_body.get("data"), dict) else {}
            try:
                amount = float(data.get("amount"))
            except (TypeError, ValueError):
                amount = None
        vol = None
        if isinstance(candles, list) and len(candles) >= 3:
            closes = []
            for row in candles[:10]:
                try:
                    closes.append(float(row[4]))
                except (TypeError, ValueError, IndexError):
                    continue
            if len(closes) >= 3:
                mean = sum(closes) / len(closes)
                vol = (sum((item - mean) ** 2 for item in closes) / len(closes)) ** 0.5
        if amount is None:
            _remember_miss(key)
            return None
        cached = _store(key, {"spot": amount, "vol": vol})
    spot = float(cached["spot"])
    strike = _strike(market)
    lines = [
        f"Source: Coinbase public spot for {product}. Proxy only, not the CF Benchmarks settlement print.",
        f"Current proxy price: {spot:.4f}",
    ]
    if strike is not None:
        lines.append(f"Strike or threshold: {strike:.4f}")
        lines.append(f"Distance (spot minus strike): {spot - strike:.4f}")
    minutes = _minutes_left(market)
    if minutes is not None:
        lines.append(f"Minutes left: {minutes}")
    if cached.get("vol") is not None:
        lines.append(f"Recent 1-minute close volatility (std dev, last prints): {float(cached['vol']):.4f}")
    return _fact_block(lines)


def _nws_observation(props: object) -> Optional[dict]:
    if not isinstance(props, dict):
        return None
    raw = props.get("temperature")
    value = raw.get("value") if isinstance(raw, dict) else None
    try:
        temp_c = float(value)
    except (TypeError, ValueError):
        return None
    observed = str(props.get("timestamp") or "").strip()
    if not observed:
        return None
    try:
        parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    return {
        "c": temp_c,
        "f": temp_c * 9.0 / 5.0 + 32.0,
        "observed_at": observed,
        "stale": age > _NWS_MAX_AGE_SEC,
    }


async def _temp_context(market: dict) -> Optional[str]:
    blob = " ".join(
        str(market.get(key) or "")
        for key in ("rules_primary", "rules_secondary", "title", "subtitle", "market_description")
    )
    found = _ICAO.findall(blob.upper())
    if not found:
        return None
    station = found[0]
    key = f"nws:{station}"
    cached = _cached(key)
    if cached is _MISS:
        return None
    if cached is None:
        body = await _get_json(
            f"https://api.weather.gov/stations/{station}/observations/latest",
            headers={"User-Agent": "kalshi-vibe-bot", "Accept": "application/geo+json"},
        )
        props = body.get("properties") if isinstance(body, dict) else None
        parsed = _nws_observation(props)
        if parsed is None:
            _remember_miss(key)
            return None
        cached = _store(key, parsed)
    if cached.get("stale"):
        return None
    lines = [
        f"Source: NWS api.weather.gov latest observation at {station}.",
        "Settlement uses the official NWS climate report, not this observation.",
        f"Observation time: {cached['observed_at']}",
        f"Temperature: {cached['f']:.1f} F ({cached['c']:.1f} C)",
    ]
    strike = _strike(market)
    if strike is not None:
        lines.append(f"Strike or threshold: {strike:.1f}")
        lines.append(f"Distance (F minus strike): {cached['f'] - strike:.1f}")
    minutes = _minutes_left(market)
    if minutes is not None:
        lines.append(f"Minutes left: {minutes}")
    return _fact_block(lines)


def _norm_name(text: str) -> str:
    cleaned = text.lower().replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return " ".join(cleaned.split())


def _split_kalshi_matchup(title: str) -> Optional[tuple[str, str]]:
    """Split on Kalshi's ``vs`` separator only. ``at`` can be part of a school name."""
    parts = re.split(r"\s+vs\.?\s+", title.strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    left, right = _norm_name(parts[0]), _norm_name(parts[1])
    if not left or not right or left == right:
        return None
    return left, right


def _team_identities(team: dict) -> set[str]:
    """Full names, location plus nickname, and abbreviation. Whole-string equality only."""
    if not isinstance(team, dict):
        return set()
    fields = []
    for key in ("displayName", "shortDisplayName", "location", "name", "nickname", "abbreviation"):
        value = str(team.get(key) or "").strip()
        if value:
            fields.append(value)
    location = str(team.get("location") or "").strip()
    nickname = str(team.get("nickname") or team.get("name") or "").strip()
    if location and nickname and nickname.lower() != location.lower():
        fields.append(f"{location} {nickname}")
    out = set()
    for value in fields:
        normalized = _norm_name(value)
        if normalized:
            out.add(normalized)
    return out


def _competitors(event: dict) -> list[dict]:
    competitions = event.get("competitions") if isinstance(event, dict) else None
    if not isinstance(competitions, list) or not competitions:
        return []
    sides = competitions[0].get("competitors") or []
    return [side for side in sides if isinstance(side, dict)]


def _side_matches(competitors: list[dict], side: str) -> list[dict]:
    hits = []
    for competitor in competitors:
        if side in _team_identities(competitor.get("team") or {}):
            hits.append(competitor)
    return hits


def match_espn_event(events: list, title: str) -> Optional[dict]:
    """One event, and only when each title side equals one different team on it.

    ``Georgia`` does not match ``Georgia Tech``. ``University at Albany`` stays
    one name. Anything ambiguous returns nothing.
    """
    split = _split_kalshi_matchup(title)
    if split is None:
        return None
    left, right = split
    hits = []
    for event in events or []:
        competitors = _competitors(event)
        left_hits = _side_matches(competitors, left)
        right_hits = _side_matches(competitors, right)
        if len(left_hits) == 1 and len(right_hits) == 1 and left_hits[0] is not right_hits[0]:
            hits.append(event)
    if len(hits) != 1:
        return None
    return hits[0]


def _score_line(event: dict) -> Optional[str]:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    comp = competitions[0]
    status = ((comp.get("status") or {}).get("type") or {}).get("shortDetail") or ""
    bits = []
    for side in comp.get("competitors") or []:
        team = side.get("team") or {}
        name = team.get("shortDisplayName") or team.get("displayName") or ""
        score = side.get("score") or ""
        if name:
            bits.append(f"{name} {score}".strip())
    if not bits:
        return None
    clock = f" Status: {status}." if status else ""
    return "Score: " + " , ".join(bits) + "." + clock


async def _sports_context(market: dict, sport: str, league: str) -> Optional[str]:
    key = f"espn:{sport}:{league}"
    cached = _cached(key)
    if cached is _MISS:
        return None
    if cached is None:
        body = await _get_json(
            f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        )
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list):
            _remember_miss(key)
            return None
        cached = _store(key, events)
    title = f"{market.get('title') or ''} {market.get('subtitle') or ''}"
    event = match_espn_event(cached or [], title)
    if event is None:
        return None
    line = _score_line(event)
    if not line:
        return None
    return _fact_block([
        f"Source: ESPN public scoreboard ({sport}/{league}). Injected only because both names matched one event.",
        line,
    ])


def _espn_league(series: str) -> Optional[tuple[str, str]]:
    for prefix, pair in _ESPN.items():
        if series.startswith(prefix):
            return pair
    return None


def _crypto_product(series: str) -> Optional[str]:
    for prefix, product in _CRYPTO.items():
        if series.startswith(prefix):
            return product
    return None


async def live_fact_for_market(market: dict) -> dict:
    """``skip`` means do not call the model. ``context`` is appended to the prompt."""
    if not bool(getattr(settings, "vibe_live_facts_enabled", True)):
        return {"skip": False, "reason": "", "context": ""}
    series = series_of(market)
    if not series:
        return {"skip": False, "reason": "", "context": ""}
    blocked = _prefixes(getattr(settings, "vibe_prefilter_series_prefixes", ""), _DEFAULT_NO_SOURCE)
    numeric = _prefixes(
        getattr(settings, "vibe_numeric_prefilter_series_prefixes", ""),
        _DEFAULT_NUMERIC_NO_SOURCE,
    )
    if _starts(series, blocked):
        return {
            "skip": True,
            "reason": f"no live score source for series {series}; skipped before the model",
            "context": "",
        }
    if _starts(series, numeric):
        return {
            "skip": True,
            "reason": f"no live price source for series {series}; skipped before the model",
            "context": "",
        }
    product = _crypto_product(series)
    if product:
        context = await _crypto_context(market, product)
        if context:
            return {"skip": False, "reason": "", "context": context}
        return {
            "skip": True,
            "reason": f"crypto spot proxy failed for {product}; skipped before the model",
            "context": "",
        }
    if _starts(series, _TEMP):
        context = await _temp_context(market)
        if context:
            return {"skip": False, "reason": "", "context": context}
        return {
            "skip": True,
            "reason": f"no NWS observation for series {series}; skipped before the model",
            "context": "",
        }
    league = _espn_league(series)
    if league:
        context = await _sports_context(market, league[0], league[1])
        return {"skip": False, "reason": "", "context": context or ""}
    return {"skip": False, "reason": "", "context": ""}
