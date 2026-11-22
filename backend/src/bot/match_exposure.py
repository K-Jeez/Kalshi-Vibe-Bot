"""One open position per underlying match, game, or asset hour.

The key ignores the series. Tennis exact-score, spread, and total markets for
the same players share a key. NFL props for the same game share a key. Crypto
strikes in the same hour share the asset and the expiry hour.
"""

from __future__ import annotations

import re

_DATE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})(.*)$")


def _crypto_asset(series: str) -> str | None:
    if series.startswith("KXETH"):
        return "ETH"
    if series.startswith("KXBTC"):
        return "BTC"
    for prefix, asset in (("KXSOL", "SOL"), ("KXXRP", "XRP"), ("KXDOGE", "DOGE")):
        if series.startswith(prefix):
            return asset
    return None


def match_exposure_key(ticker: str) -> str:
    text = str(ticker or "").strip().upper()
    parts = [part for part in text.split("-") if part]
    if len(parts) < 2:
        return text
    series, event = parts[0], parts[1]
    asset = _crypto_asset(series)
    dated = _DATE.match(event)
    if asset and dated:
        return f"asset:{asset}:{dated.group(1)}"
    if dated:
        date, rest = dated.group(1), dated.group(2)
        rest = re.sub(r"^\d+", "", rest)
        if rest:
            return f"match:{date}:{rest}"
        return f"match:{series}:{date}"
    return f"event:{series}:{event}"
