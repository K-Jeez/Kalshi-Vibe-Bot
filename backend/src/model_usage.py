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


def usage_from_response(payload: dict) -> dict:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "actual_usd": None}
    inp = usage.get("prompt_tokens")
    out = usage.get("completion_tokens")
    billed = usage.get("total_cost_usd")
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cached = details.get("cached_tokens")
    if not isinstance(cached, int):
        cached = usage.get("cached_tokens")
    return {
        "input_tokens": int(inp) if isinstance(inp, int) else None,
        "output_tokens": int(out) if isinstance(out, int) else None,
        "cached_input_tokens": int(cached) if isinstance(cached, int) else None,
        "actual_usd": float(billed) if isinstance(billed, (int, float)) else None,
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
