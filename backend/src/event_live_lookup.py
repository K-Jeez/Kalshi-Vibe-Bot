"""One live web/X lookup per event, shared by every contract on that event."""

from __future__ import annotations

import time
from typing import Any, Optional

from src.config import settings

# key -> (stored_at, facts or None on a failed call, confirmed_empty)
_cache: dict[str, tuple[float, Optional[str], bool]] = {}


def clear_event_search_cache() -> None:
    _cache.clear()


def in_play(seconds_until: Optional[float]) -> bool:
    window = float(getattr(settings, "vibe_live_search_within_sec", 10800))
    if seconds_until is None:
        return False
    return 0 < float(seconds_until) <= window


async def lookup_event_facts(client: Any, *, event_key: str, title: str) -> tuple[Optional[str], bool]:
    """Return ``(facts, confirmed_empty)``. A failed call is ``(None, False)`` and is cached."""
    key = (event_key or title or "").strip()
    if not key:
        return None, False
    now = time.monotonic()
    ttl = float(getattr(settings, "vibe_live_search_cache_sec", 90))
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1], hit[2]
    facts: Optional[str]
    try:
        facts = await client.search_event_facts(
            title or key,
            max_results=int(getattr(settings, "vibe_live_search_max_results", 4)),
        )
    except Exception:
        facts = None
    confirmed = facts == ""
    _cache[key] = (now, facts, confirmed)
    if len(_cache) > 400:
        oldest = min(_cache, key=lambda item: _cache[item][0])
        _cache.pop(oldest, None)
    return facts, confirmed


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
    facts, confirmed_empty = await lookup_event_facts(client, event_key=event_key, title=title)
    if facts:
        return facts, ""
    if confirmed_empty:
        return "", "no live feed and no live search result; skipped before the model"
    return "", hard_skip
