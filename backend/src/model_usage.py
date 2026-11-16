"""Model call accounting. Estimates are labeled estimates.

Prices are dollars per million tokens and are not a bill. Actual token
counts come only from a usage object the provider returned.
"""

from __future__ import annotations

import hashlib
from typing import Optional

# grok-4.3 public list price, dollars per million tokens. An estimate, not a bill.
_ESTIMATE_USD_PER_MILLION = {
    "input": 1.25,
    "cached_input": 0.20,
    "output": 2.50,
}


def prompt_fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# Published server-side tool rates. Web search is per call. X search is per post
# and per profile fetched, not per call. An estimate, not a bill.
_WEB_SEARCH_USD_PER_CALL = 5.0 / 1000.0
_X_POST_USD = 5.0 / 1000.0
_X_PROFILE_USD = 10.0 / 1000.0


def _as_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def usage_from_response(payload: dict) -> dict:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "actual_usd": None}
    inp = _as_int(usage.get("prompt_tokens"))
    if inp is None:
        inp = _as_int(usage.get("input_tokens"))
    out = _as_int(usage.get("completion_tokens"))
    if out is None:
        out = _as_int(usage.get("output_tokens"))
    billed = usage.get("total_cost_usd")
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    if not details and isinstance(usage.get("input_tokens_details"), dict):
        details = usage.get("input_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is None:
        cached = _as_int(usage.get("cached_tokens"))
    tools = usage.get("server_side_tool_usage_details")
    if not isinstance(tools, dict):
        tools = {}
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cached_input_tokens": cached,
        "actual_usd": float(billed) if isinstance(billed, (int, float)) and not isinstance(billed, bool) else None,
        "web_search_calls": _as_int(tools.get("web_search_calls")) or 0,
        "x_search_calls": _as_int(tools.get("x_search_calls")) or 0,
        "x_posts_fetched": _as_int(tools.get("x_posts_fetched")) or 0,
        "x_users_fetched": _as_int(tools.get("x_users_fetched")) or 0,
    }


def estimate_usd(
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cached_input_tokens: Optional[int] = None,
) -> Optional[float]:
    if input_tokens is None and output_tokens is None:
        return None
    cached = max(0, int(cached_input_tokens or 0))
    fresh = max(0, int(input_tokens or 0) - cached)
    cost = (fresh / 1_000_000.0) * _ESTIMATE_USD_PER_MILLION["input"]
    cost += (cached / 1_000_000.0) * _ESTIMATE_USD_PER_MILLION["cached_input"]
    cost += (int(output_tokens or 0) / 1_000_000.0) * _ESTIMATE_USD_PER_MILLION["output"]
    return round(cost, 6)


def tool_invocation_usd(
    *,
    web_search_calls: int = 0,
    x_posts_fetched: int = 0,
    x_users_fetched: int = 0,
) -> float:
    """Web search is $5/1k calls. X search is $5/1k posts and $10/1k profiles."""
    cost = max(0, int(web_search_calls)) * _WEB_SEARCH_USD_PER_CALL
    cost += max(0, int(x_posts_fetched)) * _X_POST_USD
    cost += max(0, int(x_users_fetched)) * _X_PROFILE_USD
    return round(cost, 6)


def estimate_with_tools(usage: dict) -> Optional[float]:
    """Token list price plus server-side tool rates. None when nothing was counted."""
    tokens = estimate_usd(
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cached_input_tokens"),
    )
    tools = tool_invocation_usd(
        web_search_calls=int(usage.get("web_search_calls") or 0),
        x_posts_fetched=int(usage.get("x_posts_fetched") or 0),
        x_users_fetched=int(usage.get("x_users_fetched") or 0),
    )
    if tokens is None and tools <= 0:
        return None
    return round((tokens or 0.0) + tools, 6)
