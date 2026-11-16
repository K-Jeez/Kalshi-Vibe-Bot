"""One live web/X lookup per event, shared by every contract on that event."""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from src.config import settings

# key -> (stored_at, facts or None, confirmed_empty, client_error or None)
_cache: dict[str, tuple[float, Optional[str], bool, Optional[int]]] = {}
# Lookups whose cost is not yet on a decision row. One entry per HTTP call.
_unbilled: list[dict] = []


def clear_event_search_cache() -> None:
    _cache.clear()
    _unbilled.clear()


def in_play(seconds_until: Optional[float]) -> bool:
    window = float(getattr(settings, "vibe_live_search_within_sec", 10800))
    if seconds_until is None:
        return False
    return 0 < float(seconds_until) <= window


def _coerce_lookup(raw: Any) -> dict:
    if isinstance(raw, dict) and ("facts" in raw or "client_error" in raw or "usage" in raw):
        usage = raw.get("usage")
        return {
            "facts": raw.get("facts"),
            "client_error": raw.get("client_error"),
            "usage": usage if isinstance(usage, dict) else None,
        }
    if raw is None or isinstance(raw, str):
        return {"facts": raw, "client_error": None, "usage": None}
    return {"facts": None, "client_error": None, "usage": None}


async def lookup_event_facts(client: Any, *, event_key: str, title: str) -> tuple[Optional[str], bool, Optional[int]]:
    """Return ``(facts, confirmed_empty, client_error)``.

    A transport failure is ``(None, False, None)``. A 4xx is ``(None, False, status)``.
    """
    key = (event_key or title or "").strip()
    if not key:
        return None, False, None
    now = time.monotonic()
    ttl = float(getattr(settings, "vibe_live_search_cache_sec", 90))
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1], hit[2], hit[3]
    try:
        raw = await client.search_event_facts(
            title or key,
            max_tool_calls=int(getattr(settings, "vibe_live_search_max_tool_calls", 3)),
        )
    except TypeError:
        raw = await client.search_event_facts(title or key)
    except Exception:
        raw = None
    parsed = _coerce_lookup(raw)
    facts = parsed["facts"]
    if isinstance(facts, str):
        facts = facts.strip()
    client_error = parsed["client_error"]
    try:
        client_error = int(client_error) if client_error is not None else None
    except (TypeError, ValueError):
        client_error = None
    confirmed = client_error is None and facts == ""
    if client_error is not None:
        facts = None
    usage = parsed["usage"]
    if isinstance(usage, dict) and usage.get("estimated_usd"):
        _unbilled.append({"event_key": key, "title": title or key, "usage": usage})
    _cache[key] = (now, facts if isinstance(facts, str) or facts is None else None, confirmed, client_error)
    if len(_cache) > 400:
        oldest = min(_cache, key=lambda item: _cache[item][0])
        _cache.pop(oldest, None)
    return _cache[key][1], confirmed, client_error


def attach_lookup_cost(decision: dict, event_key: str) -> None:
    """Add this event's lookup cost to the analysis usage once."""
    key = (event_key or "").strip()
    if not key or not _unbilled:
        return
    kept: list[dict] = []
    extras: list[dict] = []
    for item in _unbilled:
        if item.get("event_key") == key:
            extras.append(item.get("usage") or {})
        else:
            kept.append(item)
    if not extras:
        return
    _unbilled[:] = kept
    blob = decision.get("xai_analysis")
    if not isinstance(blob, dict):
        blob = {}
        decision["xai_analysis"] = blob
    for extra in extras:
        _merge_usage(blob, extra)


def flush_unbilled_lookups(db) -> None:
    """Write lookup cost that never reached an analysis row."""
    if db is None or not _unbilled:
        return
    pending = list(_unbilled)
    _unbilled.clear()
    from src.config import TRADE_MODE
    from src.database.models import DecisionLog
    from src.json_safe import dumps_payload

    blob: dict = {"usage": {}}
    for item in pending:
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else None
        if usage:
            _merge_usage(blob, usage)
    combined = blob["usage"]
    if not combined.get("estimated_usd"):
        return
    db.add(
        DecisionLog(
            id=str(uuid.uuid4()),
            market_id="live-lookup",
            market_title="In-play lookups",
            decision="SKIP",
            xai_analysis=dumps_payload({"usage": combined, "live_lookup": True}),
            confidence=0.0,
            reasoning="Live lookup cost",
            real_time_context="",
            key_factors="[]",
            yes_confidence=0,
            no_confidence=0,
            escalated_to_xai=False,
            edge=0.0,
            filter_pass=False,
            pre_filter_reason="live lookup cost",
            action_taken=dumps_payload({"status": "skipped", "summary": "live lookup"}),
            trade_mode=TRADE_MODE,
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()


def _merge_usage(blob: dict, extra: dict) -> None:
    usage = blob.get("usage")
    if not isinstance(usage, dict):
        usage = {}
        blob["usage"] = usage
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        if usage.get(key) is None and extra.get(key) is None:
            continue
        usage[key] = int(usage.get(key) or 0) + int(extra.get(key) or 0)
    for key in ("web_search_calls", "x_search_calls", "x_posts_fetched", "x_users_fetched"):
        usage[key] = int(usage.get(key) or 0) + int(extra.get(key) or 0)
    usage["estimated_usd"] = round(
        float(usage.get("estimated_usd") or 0.0) + float(extra.get("estimated_usd") or 0.0),
        6,
    )
    usage["lookup_usd"] = round(float(usage.get("lookup_usd") or 0.0) + float(extra.get("estimated_usd") or 0.0), 6)


async def context_for_market(
    *,
    market: dict,
    fact: dict,
    client: Any,
    seconds_until: Optional[float],
    title: str,
) -> tuple[str, str]:
    """Return ``(context, skip_reason)``. A skip reason means do not call the model."""
    context = str((fact or {}).get("context") or "").strip()
    if context:
        return context, ""
    hard_skip = str((fact or {}).get("reason") or "").strip() if (fact or {}).get("skip") else ""
    if client is None or not bool(getattr(settings, "vibe_live_search_enabled", True)) or not in_play(seconds_until):
        return "", hard_skip
    event_key = str((market or {}).get("event_ticker") or (market or {}).get("id") or title or "").strip()
    facts, confirmed_empty, client_error = await lookup_event_facts(client, event_key=event_key, title=title)
    if client_error:
        return "", f"live search HTTP {client_error}; skipped before the model"
    if facts:
        return facts, ""
    if confirmed_empty:
        return "", "no live feed and no live search result; skipped before the model"
    return "", hard_skip
