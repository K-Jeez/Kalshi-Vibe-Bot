"""Sizing and edge floors from this app's own settled trades.

Nothing here picks a new fixed edge number. The owner's configured floor is
the start. A bucket moves only after a minimum sample, and it moves back when
the bucket stops losing.
"""

from __future__ import annotations

import math
from typing import Optional

from src.decision_engine.bucket_caps import MIN_SAMPLE, edge_bucket

PRIOR_BLEND_WEIGHT = 0.25
# A floor at or above the bucket's top would stop that bucket from ever trading again.
_BUCKET_TOP = {"4-6": 6.0, "6-8": 8.0, "8-10": 10.0, "10-13": 13.0}


def market_yes_probability(implied_pct: float, side: str) -> float:
    """YES probability from a buy-side implied percent.

    ``market_implied_probability_pct`` is the purchased side. A NO buy at 70
    is a YES price of 0.30, not 0.70.
    """
    raw = float(implied_pct)
    unit = raw / 100.0 if raw > 1.0 else raw
    unit = max(0.0, min(1.0, unit))
    held = str(side or "").upper()
    if held in ("NO", "BUY_NO"):
        return 1.0 - unit
    return unit


def calibrated_blend_weight(rows: list[dict], *, prior: float = PRIOR_BLEND_WEIGHT, min_sample: int = MIN_SAMPLE) -> float:
    """Trust the model more when its YES Brier beats the fee-free mid.

    ``rows`` need ``model_yes``, ``market_yes`` (0–1) and ``outcome_yes`` (0 or 1).
    Fewer than ``min_sample`` rows keeps ``prior`` (the configured blend weight).
    """
    start = max(0.0, min(1.0, float(prior)))
    usable = []
    for row in rows or []:
        try:
            model = float(row.get("model_yes"))
            market = float(row.get("market_yes"))
            outcome = float(row.get("outcome_yes"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (model, market, outcome)):
            continue
        usable.append((max(0.0, min(1.0, model)), max(0.0, min(1.0, market)), 1.0 if outcome >= 0.5 else 0.0))
    if len(usable) < int(min_sample):
        return start
    model_brier = sum((m - y) ** 2 for m, _k, y in usable) / len(usable)
    market_brier = sum((k - y) ** 2 for _m, k, y in usable) / len(usable)
    if market_brier <= 1e-12:
        return start
    skill = (market_brier - model_brier) / market_brier
    if not math.isfinite(skill):
        return start
    # Skill 0 keeps the prior. Skill 1 moves the rest of the way to the model.
    # A model that loses to the market shrinks the weight toward 0.
    if skill >= 0:
        weight = start + min(1.0, skill) * (1.0 - start)
    else:
        weight = start * max(0.0, 1.0 + skill)
    return max(0.0, min(1.0, weight))


def effective_edge_floor(
    base_floor: float,
    *,
    realized_pnl: float,
    cost: float,
    model_cost: float,
    n: int,
    min_sample: int = MIN_SAMPLE,
    bucket: Optional[str] = None,
) -> float:
    """Owner floor, plus one point per full 25% of stake lost.

    A loss smaller than 25% of stake does not move the floor. The add-on is at
    most the owner's floor. It also stays below the bucket's top edge, so the
    top of a losing bucket can still trade and the floor can fall back.
    """
    base = max(0.0, float(base_floor))
    if int(n) < int(min_sample) or float(cost) <= 1e-9:
        return base
    net = float(realized_pnl) - max(0.0, float(model_cost or 0.0))
    if net >= -1e-9:
        return base
    roi = net / float(cost)
    if not math.isfinite(roi) or roi >= 0:
        return base
    steps = min(int(math.floor(base)) if base >= 1 else 0, int(math.floor(abs(roi) / 0.25 + 1e-12)))
    floor = base + float(max(0, steps))
    top = _BUCKET_TOP.get(str(bucket or ""))
    if top is not None:
        floor = min(floor, float(top) - 0.05)
    return max(base, floor)


def floors_from_closed(rows: list[dict], base_floor: float) -> list[dict]:
    grouped = {name: {"n": 0, "realized": 0.0, "cost": 0.0, "model_cost": 0.0} for name in ("4-6", "6-8", "8-10", "10-13", "13+")}
    for row in rows or []:
        name = edge_bucket(float(row.get("edge") or 0.0))
        if name is None or name not in grouped:
            continue
        grouped[name]["n"] += 1
        grouped[name]["realized"] += float(row.get("realized_pnl") or 0.0)
        grouped[name]["cost"] += max(0.0, float(row.get("cost") or 0.0))
        grouped[name]["model_cost"] += max(0.0, float(row.get("model_cost") or 0.0))
    out = []
    for name, item in grouped.items():
        floor = effective_edge_floor(
            base_floor,
            realized_pnl=item["realized"],
            cost=item["cost"],
            model_cost=item["model_cost"],
            n=item["n"],
            bucket=name,
        )
        out.append({"bucket": name, "edge_floor": floor, "n": item["n"]})
    return out


def floor_for_edge(table: list[dict], edge_pts: float, base_floor: float) -> float:
    name = edge_bucket(float(edge_pts))
    if name is None:
        return max(0.0, float(base_floor))
    for row in table or []:
        if row.get("bucket") == name:
            return float(row.get("edge_floor") if row.get("edge_floor") is not None else base_floor)
    return max(0.0, float(base_floor))


def expected_xai_cost_per_buy(costs: list[float], executed_buys: int, *, min_buys: int = 4) -> float:
    """Total estimated model spend divided by filled buys. Too few buys stays 0."""
    buys = int(executed_buys or 0)
    if buys < int(min_buys):
        return 0.0
    spent = 0.0
    for raw in costs or []:
        try:
            spent += max(0.0, float(raw))
        except (TypeError, ValueError):
            continue
    if spent <= 0:
        return 0.0
    return spent / float(buys)


def load_edge_floors(db, base_floor: float) -> list[dict]:
    """Per-bucket floors from closed positions and their entry edge."""
    from src.database.models import DecisionLog, Position

    rows = []
    try:
        found = (
            db.query(Position.realized_pnl, Position.entry_cost, Position.fees_paid, DecisionLog.edge)
            .join(DecisionLog, DecisionLog.id == Position.entry_decision_log_id)
            .filter(Position.status == "closed")
            .all()
        )
    except Exception:
        return floors_from_closed([], base_floor)
    per_buy = 0.0
    costs, buys = load_model_spend(db)
    if buys > 0 and costs:
        per_buy = sum(costs) / float(buys)
    for realized, cost, fees, edge in found:
        rows.append(
            {
                "edge": float(edge or 0.0),
                "realized_pnl": float(realized or 0.0),
                "cost": float(cost or 0.0) + float(fees or 0.0),
                "model_cost": per_buy,
            }
        )
    return floors_from_closed(rows, base_floor)


def load_settled_calibration(db) -> list[dict]:
    """Settled entry decisions: model YES, market YES, and the yes/no outcome."""
    from src.database.models import DecisionLog, Position

    rows = []
    try:
        found = (
            db.query(DecisionLog, Position.kalshi_market_result)
            .join(Position, Position.entry_decision_log_id == DecisionLog.id)
            .filter(Position.status == "closed")
            .all()
        )
    except Exception:
        return []
    for decision, result in found:
        outcome = str(result or "").strip().lower()
        if outcome not in ("yes", "no"):
            continue
        model = getattr(decision, "ai_probability_yes_pct", None)
        market = getattr(decision, "market_implied_probability_pct", None)
        if model is None or market is None:
            continue
        side = str(getattr(decision, "decision", "") or "")
        rows.append(
            {
                "model_yes": float(model) / 100.0,
                "market_yes": market_yes_probability(float(market), side),
                "outcome_yes": 1.0 if outcome == "yes" else 0.0,
                "edge": float(getattr(decision, "edge", 0.0) or 0.0),
            }
        )
    return rows


def _analysis_cost(raw_analysis) -> float:
    import json

    try:
        analysis = json.loads(raw_analysis) if isinstance(raw_analysis, str) else (raw_analysis or {})
    except (TypeError, ValueError):
        return 0.0
    usage = analysis.get("usage") if isinstance(analysis, dict) and isinstance(analysis.get("usage"), dict) else {}
    try:
        return max(0.0, float(usage.get("estimated_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _filled_buy(raw_action) -> bool:
    import json

    try:
        action = json.loads(raw_action) if isinstance(raw_action, str) else (raw_action or {})
    except (TypeError, ValueError):
        return False
    return isinstance(action, dict) and action.get("status") in ("executed", "partial")


def load_model_spend(db) -> tuple[list[float], int]:
    """Recent estimated model dollars and the filled buys in that same window.

    The newest logs are the ones that record ``usage.estimated_usd``. An
    unordered read returns the oldest rows, which have no cost, and the
    per-buy estimate stays zero.
    """
    from src.database.models import DecisionLog

    try:
        found = (
            db.query(DecisionLog.xai_analysis, DecisionLog.action_taken, DecisionLog.timestamp)
            .order_by(DecisionLog.timestamp.desc())
            .limit(2000)
            .all()
        )
    except Exception:
        return [], 0
    parsed = []
    oldest_cost = None
    for raw_analysis, raw_action, stamp in found:
        cost = _analysis_cost(raw_analysis)
        parsed.append((cost, _filled_buy(raw_action), stamp))
        if cost > 0 and (oldest_cost is None or (stamp is not None and stamp < oldest_cost)):
            oldest_cost = stamp
    if oldest_cost is None:
        return [], 0
    costs: list[float] = []
    buys = 0
    for cost, bought, stamp in parsed:
        if stamp is not None and stamp < oldest_cost:
            continue
        if cost > 0:
            costs.append(cost)
        if bought:
            buys += 1
    return costs, buys


def order_covers_model_cost(ev_dollars: Optional[float], model_cost: float) -> bool:
    """True when this size's fee-inclusive EV clears the expected model cost.

    A missing cost does not block. A missing EV does not count as covering it.
    """
    cost = max(0.0, float(model_cost or 0.0))
    if cost <= 1e-12:
        return True
    if ev_dollars is None or not math.isfinite(float(ev_dollars)):
        return False
    return float(ev_dollars) > cost + 1e-12
