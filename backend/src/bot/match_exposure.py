"""One open position per underlying match, game, or asset hour.

The key ignores the series. Tennis exact-score, spread, and total markets for
the same players share a key. NFL props for the same game share a key. Crypto
strikes in the same hour share the asset and the expiry hour.
"""

from __future__ import annotations

import re

_DATE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})(.*)$")
_CRYPTO_SERIES = frozenset({
    "KXETH",
    "KXBTC",
    "KXBTCD",
    "KXSOL",
    "KXXRP",
    "KXDOGE",
    "KXBNB",
    "KXADA",
})


def match_exposure_key(ticker: str) -> str:
    text = str(ticker or "").strip().upper()
    parts = [part for part in text.split("-") if part]
    if len(parts) < 2:
        return text
    series, event = parts[0], parts[1]
    if series in _CRYPTO_SERIES:
        asset = series[2:]
        return f"asset:{asset}:{event}"
    dated = _DATE.match(event)
    if dated:
        date, rest = dated.group(1), dated.group(2)
        rest = re.sub(r"^\d+", "", rest)
        if rest:
            return f"match:{date}:{rest}"
        return f"match:{date}:{event}"
    return f"event:{event}"
