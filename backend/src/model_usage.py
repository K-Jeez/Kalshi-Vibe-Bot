"""Model call accounting. Estimates are labeled estimates.

Prices are dollars per million tokens and are not a bill. Actual token
counts come only from a usage object the provider returned.
"""

from __future__ import annotations

import hashlib
from typing import Optional

# Public list price used only as an estimate. Not a charge.
_ESTIMATE_USD_PER_MILLION = {
    "input": 0.20,
    "output": 0.50,
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
    return {
        "input_tokens": int(inp) if isinstance(inp, int) else None,
        "output_tokens": int(out) if isinstance(out, int) else None,
        "actual_usd": float(billed) if isinstance(billed, (int, float)) else None,
    }


def estimate_usd(input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    if input_tokens is None and output_tokens is None:
        return None
    cost = 0.0
    if input_tokens is not None:
        cost += (input_tokens / 1_000_000.0) * _ESTIMATE_USD_PER_MILLION["input"]
    if output_tokens is not None:
        cost += (output_tokens / 1_000_000.0) * _ESTIMATE_USD_PER_MILLION["output"]
    return round(cost, 6)
