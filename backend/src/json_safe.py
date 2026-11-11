"""JSON for decision and fill records that may contain Decimal or datetime values."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def payload_default(value: Any) -> Any:
    """Decimal becomes a JSON number. datetime becomes an ISO string."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps_payload(payload: Any, **kwargs: Any) -> str:
    """Serialize a decision, action, or activity payload."""
    return json.dumps(payload, default=payload_default, **kwargs)
