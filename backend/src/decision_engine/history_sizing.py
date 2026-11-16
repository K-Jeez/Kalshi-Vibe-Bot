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
_BUCKET_TOP = {"4-6": 6.0, "6-8": 8.0, "8-13": 13.0}


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


def brier_skill(rows: list[dict]) -> tuple[float, float, int]:
    """Return ``(skill, t_stat, n)`` for model-versus-market YES Brier.

    Skill is ``(market − model) / market``. Negative means the model is worse.
    ``t_stat`` is the paired difference of squared errors (positive when the
    model is worse) divided by its standard error. ``n`` is the usable count.
    """
    diffs: list[float] = []
    model_brier = 0.0
    market_brier = 0.0
    for row in rows or []:
        try:
            model = float(row.get("model_yes"))
            market = float(row.get("market_yes"))
            outcome = float(row.get("outcome_yes"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (model, market, outcome)):
            continue
        model = max(0.0, min(1.0, model))
        market = max(0.0, min(1.0, market))
        y = 1.0 if outcome >= 0.5 else 0.0
        model_se = (model - y) ** 2
        market_se = (market - y) ** 2
        model_brier += model_se
        market_brier += market_se
        diffs.append(model_se - market_se)
    n = len(diffs)
    if n < 1:
        return 0.0, 0.0, 0
    model_brier /= n
    market_brier /= n
    skill = 0.0 if market_brier <= 1e-12 else (market_brier - model_brier) / market_brier
    mean = sum(diffs) / n
    if n < 2:
        return float(skill), 0.0, n
    var = sum((item - mean) ** 2 for item in diffs) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    if se <= 1e-12:
        t_stat = math.inf if mean > 1e-12 else 0.0
    else:
        t_stat = mean / se
    return float(skill), float(t_stat), n


def sizing_model_weight(rows: list[dict], *, min_sample: int = MIN_SAMPLE) -> float:
    """Share of the raw model used for size. Gates stay on the raw model.

    The weight is 1 until settled trades show the model is significantly worse
    than the market (paired Brier t above 2, and at least ``min_sample``
    rows). The shrink is the skill itself, so a skill of about −0.04 stays
    near 1 and a large negative skill moves size toward the fee-free mid.
    """
    skill, t_stat, n = brier_skill(rows)
    if n < int(min_sample) or skill >= -1e-12:
        return 1.0
    if math.isfinite(t_stat) and t_stat <= 2.0:
        return 1.0
    return max(0.0, min(1.0, 1.0 + float(skill)))


def calibrated_blend_weight(rows: list[dict], *, prior: float = PRIOR_BLEND_WEIGHT, min_sample: int = MIN_SAMPLE) -> float:
    """Sizing weight. ``prior`` is ignored; evidence decides any shrink."""
    del prior
    return sizing_model_weight(rows, min_sample=min_sample)


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
    grouped = {name: {"n": 0, "realized": 0.0, "cost": 0.0, "model_cost": 0.0} for name in ("4-6", "6-8", "8-13", "13+")}
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


def edge_floor_skip_text(edge: float, effective: float, setting: float, bucket: Optional[str]) -> str:
    """Plain skip line. Names the history add-on when it is above the Settings value."""
    shown_edge = float(edge)
    floor = float(effective)
    base = float(setting)
    if floor <= base + 1e-9:
        return f"Skipped — the edge is {shown_edge:.1f} points, below the {floor:.0f}-point minimum"
    extra = floor - base
    band = bucket or "that"
    return (
        f"Skipped — edge {shown_edge:.1f} below {floor:.1f} — your setting is {base:.0f}, "
        f"+{extra:.0f} because past {band} point trades lost money"
    )


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
