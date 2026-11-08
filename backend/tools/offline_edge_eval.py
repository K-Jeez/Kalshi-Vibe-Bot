#!/usr/bin/env python3
"""Offline synthetic stress test of the corrected Kalshi Vibe Bot buy gates.

This does not call Kalshi or xAI. It does not read ignored databases, logs, or
secrets. Forecasts are simulated. They are not historical bot predictions.

The script imports the repository's real edge, Kelly, and autonomous-gate
functions. It refuses to run if the small-account stake cap still expands to
full cash (that was the pre-fix behavior).

Run from the repository (backend on PYTHONPATH, or this file's default which
inserts ``backend/``):

    python offline_edge_eval.py --out /path/to/outdir

A tiny deterministic self-check:

    python offline_edge_eval.py --self-check
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _backend_dir() -> Path:
    # Artifacts copy lives outside the repo. The committed copy lives in backend/tools.
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[2] / "backend",  # repo/backend when file is backend/tools/...
        here.parents[3] / "workspace" / "backend",
        Path("/workspace/backend"),
    ):
        if (candidate / "src" / "decision_engine" / "strategy_math.py").exists():
            return candidate
    raise SystemExit("Could not find backend/src next to this script or at /workspace/backend")


sys.path.insert(0, str(_backend_dir()))

from src.decision_engine.strategy_gates import (  # noqa: E402
    autonomous_buy_gate_failure,
    kelly_contract_cap_for_bankroll,
)
from src.decision_engine.strategy_math import (  # noqa: E402
    ai_win_prob_pct_on_buy_side,
    edge_pct_for_side,
    effective_buy_gate_thresholds,
)

# Locked study design. Fees and spreads are scenario inputs, not a Kalshi schedule.
SEED = 20261108
N_REPS = 200
N_WAVES = 10
PER_WAVE = 12
MIN_EDGE = 5.0
MIN_AI = 60
MAX_OPEN = 30
P_CLIP = 1e-3

EXPECTED_SAFETY_COMMIT_PREFIX = "c2994d3"


def assert_corrected_stake_cap() -> None:
    """The corrected cap returns 0 when 5% of cash cannot buy one contract."""
    got = kelly_contract_cap_for_bankroll(10.46, 0.67)
    if got != 0:
        raise SystemExit(
            "Refusing to run: kelly_contract_cap_for_bankroll(10.46, 0.67) "
            f"returned {got}, expected 0. This study evaluates the corrected "
            "gates (safety commit c2994d3), not the older full-cash fallback."
        )


def clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def book_from_yes_probability(yes_prob: float, half_spread: float) -> Dict[str, float]:
    """Symmetric illustrative book. no prices are the YES complement.

    ``market_yes_prob`` is the probability used as the market forecast.
    It is the pre-spread probability, not the ask. Asks are what the bot buys.
    """
    mid = clip(float(yes_prob), 0.08, 0.92)
    hs = max(0.0, float(half_spread))
    yes_bid = clip(mid - hs, 0.01, 0.98)
    yes_ask = clip(mid + hs, 0.02, 0.99)
    if yes_ask < yes_bid:
        yes_ask = yes_bid
    no_bid = clip(1.0 - yes_ask, 0.01, 0.98)
    no_ask = clip(1.0 - yes_bid, 0.02, 0.99)
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": (yes_bid + yes_ask) / 2.0,
        "no_mid": (no_bid + no_ask) / 2.0,
        "market_yes_prob": mid,
    }


def forecast_yes_pct(model: str, market_yes_prob: float, true_p: float, rng: random.Random) -> int:
    """Simulated P(YES) in percent. Never reads an outcome.

    ``true_p`` is the data-generating probability. Using it is an assumption
    about the forecaster, not a measured property of xAI.
    """
    if model == "market_mid":
        return int(round(100.0 * market_yes_prob))
    if model == "flat_50":
        return 50
    if model == "null_noise":
        return int(rng.randint(5, 95))
    if model == "overconfident":
        ph = 0.5 + 1.75 * (market_yes_prob - 0.5)
        return int(round(100.0 * clip(ph, 0.02, 0.98)))
    if model == "true_p":
        return int(round(100.0 * clip(true_p, 0.02, 0.98)))
    raise ValueError(model)


def side_from_forecast(model: str, ai_yes_pct: int) -> Optional[str]:
    """Match the prompt rule: do not buy a side the forecast says is less likely."""
    if model == "flat_50" or ai_yes_pct == 50:
        return None
    return "YES" if ai_yes_pct > 50 else "NO"


def _auto_reason(msg: str) -> str:
    if "edge" in msg and "autonomous cap" in msg:
        return "edge_cap"
    if "AI win prob" in msg and "autonomous cap" in msg:
        return "ai_cap"
    if "autonomous floor" in msg:
        return "min_entry"
    if "calibration guard" in msg:
        return "mid_price_guard"
    return "autonomous_other"


def decide_buy(
    *,
    cash: float,
    ai_yes_pct: int,
    side: Optional[str],
    book: Dict[str, float],
    illustrative_fee: float,
) -> Dict[str, Any]:
    """Apply the production gate order. Returns a decision dict. Does not use the outcome."""
    if side not in ("YES", "NO"):
        return {"action": "skip", "reason": "no_side", "qty": 0, "tag": "none", "ask": None, "edge": None}
    ask = float(book["yes_ask"] if side == "YES" else book["no_ask"])
    edge = edge_pct_for_side(
        side,
        ai_yes_pct,
        book["yes_ask"],
        book["no_ask"],
        book["yes_mid"],
        book["no_mid"],
    )
    eff_edge, eff_ai, _tier = effective_buy_gate_thresholds(
        side=side,
        ai_yes_pct=ai_yes_pct,
        yes_ask=book["yes_ask"],
        no_ask=book["no_ask"],
        yes_mid=book["yes_mid"],
        no_mid=book["no_mid"],
        min_edge_base=MIN_EDGE,
        min_ai_win_prob_base=MIN_AI,
    )
    auto = autonomous_buy_gate_failure(
        side=side,
        ai_yes_pct=ai_yes_pct,
        edge_pct=edge,
        entry_price_dollars=ask,
    )
    if auto:
        return {"action": "skip", "reason": _auto_reason(auto), "qty": 0, "tag": "none", "ask": ask, "edge": edge}
    ai_buy = ai_win_prob_pct_on_buy_side(side, ai_yes_pct)
    if ai_buy < eff_ai:
        return {"action": "skip", "reason": "min_ai", "qty": 0, "tag": "none", "ask": ask, "edge": edge}
    if edge + 1e-9 < float(eff_edge):
        return {"action": "skip", "reason": "min_edge", "qty": 0, "tag": "none", "ask": ask, "edge": edge}
    kcap = kelly_contract_cap_for_bankroll(cash, ask)
    from src.decision_engine.strategy_math import kelly_contracts_for_order

    qty, tag = kelly_contracts_for_order(
        cash,
        side,
        ai_yes_pct,
        book["yes_ask"],
        book["no_ask"],
        book["yes_mid"],
        book["no_mid"],
        per_contract_premium=ask,
        max_kelly_contracts=kcap,
    )
    if qty < 1:
        reason = "stake_cap" if kcap < 1 and cash + 1e-12 >= ask else "kelly_zero"
        return {"action": "skip", "reason": reason, "qty": 0, "tag": tag, "ask": ask, "edge": edge}
    unit = ask + max(0.0, illustrative_fee)
    if qty * unit > cash + 1e-9:
        qty2 = int(math.floor(cash / unit + 1e-12)) if unit > 1e-12 else 0
        if qty2 < 1:
            return {
                "action": "skip",
                "reason": "fee_reserve",
                "qty": 0,
                "tag": tag,
                "ask": ask,
                "edge": edge,
            }
        qty = qty2
        tag = tag + "+fee_reserve"
    return {"action": "buy", "reason": "pass", "qty": int(qty), "tag": tag, "ask": ask, "edge": edge}


def fill_price(ask: float, drift_mode: str, rng: random.Random) -> Optional[float]:
    """IOC at the scanned ask. A higher later ask does not fill.

    Drift sizes are illustrative quote-move assumptions, not measured slippage.
    """
    if drift_mode == "none":
        return ask
    if drift_mode == "always_worse_2c":
        return None
    if drift_mode == "coin_flip_2c":
        return None if rng.random() < 0.5 else ask
    raise ValueError(drift_mode)


def _brier(pairs: Iterable[tuple]) -> Optional[float]:
    rows = list(pairs)
    if not rows:
        return None
    return sum((p - y) ** 2 for p, y in rows) / len(rows)


def _logloss(pairs: Iterable[tuple]) -> Optional[float]:
    rows = list(pairs)
    if not rows:
        return None
    total = 0.0
    for p, y in rows:
        pc = clip(p, P_CLIP, 1.0 - P_CLIP)
        total += -(y * math.log(pc) + (1.0 - y) * math.log(1.0 - pc))
    return total / len(rows)


def _accuracy(pairs: Iterable[tuple]) -> Optional[float]:
    """Fraction of calls on the correct side. A 50% forecast is not a correct call."""
    rows = list(pairs)
    if not rows:
        return None
    good = 0
    for p, y in rows:
        if p > 0.5:
            good += int(y == 1.0)
        elif p < 0.5:
            good += int(y == 0.0)
    return good / len(rows)


def _blank_mean(xs: List[float]) -> Optional[float]:
    return statistics.fmean(xs) if xs else None


def _pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    ordered = sorted(xs)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    w = pos - lo
    return ordered[lo] * (1.0 - w) + ordered[hi] * w


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    world: str
    model: str
    skill_label: str
    cash: float
    illustrative_fee: float
    half_spread: float
    drift_mode: str
    description: str


def scenario_catalog() -> List[Scenario]:
    """Explicit list. Not a silent full factorial."""
    return [
        Scenario("eff_market_c500", "efficient", "market_mid", "market_baseline", 500, 0.0, 0.02, "none",
                 "Market probability as the forecast, efficient book, no illustrative fee."),
        Scenario("eff_flat50_c500", "efficient", "flat_50", "zero_information", 500, 0.0, 0.02, "none",
                 "Forecast is always 50. No side is taken."),
        Scenario("eff_null_c500_f0", "efficient", "null_noise", "zero_information", 500, 0.0, 0.02, "none",
                 "Forecast is noise, independent of the true probability and the book."),
        Scenario("eff_null_c500_f2c", "efficient", "null_noise", "zero_information", 500, 0.02, 0.02, "none",
                 "Same noise forecast with an illustrative 2 cent fee per contract."),
        Scenario("eff_null_c10_f0", "efficient", "null_noise", "zero_information", 10, 0.0, 0.02, "none",
                 "Noise forecast on a $10 account, where the 5% cap often cannot buy one contract."),
        Scenario("eff_over_c500_f0", "efficient", "overconfident", "miscalibrated", 500, 0.0, 0.02, "none",
                 "Forecast exaggerates the market mid. The market mid is the true probability."),
        Scenario("eff_over_c500_f2c", "efficient", "overconfident", "miscalibrated", 500, 0.02, 0.02, "none",
                 "Overconfident forecast plus an illustrative 2 cent fee."),
        Scenario("eff_over_c10_f0", "efficient", "overconfident", "miscalibrated", 10, 0.0, 0.02, "none",
                 "Overconfident forecast on a $10 account."),
        Scenario("eff_truep_spread2c", "efficient", "true_p", "oracle_equals_market", 500, 0.0, 0.02, "none",
                 "Forecast equals the true probability, and the market mid does too. Not an edge over the market."),
        Scenario("eff_truep_spread0", "efficient", "true_p", "oracle_equals_market", 500, 0.0, 0.0, "none",
                 "Same oracle, illustrative spread set to zero. Still compared with the 5 point gate."),
        Scenario("under_truep_c500_f0", "underreact", "true_p", "assumed_positive_not_evidence", 500, 0.0, 0.02, "none",
                 "ASSUMED: forecast knows the true probability and the market mid is shrunk toward 50%."),
        Scenario("under_truep_c500_f1c", "underreact", "true_p", "assumed_positive_not_evidence", 500, 0.01, 0.02, "none",
                 "Assumed-skill world with an illustrative 1 cent fee."),
        Scenario("under_truep_c500_f2c", "underreact", "true_p", "assumed_positive_not_evidence", 500, 0.02, 0.02, "none",
                 "Assumed-skill world with an illustrative 2 cent fee."),
        Scenario("under_truep_c10_f0", "underreact", "true_p", "assumed_positive_not_evidence", 10, 0.0, 0.02, "none",
                 "Assumed-skill world on a $10 account."),
        Scenario("under_market_c500", "underreact", "market_mid", "market_baseline", 500, 0.0, 0.02, "none",
                 "Market-mid forecast in the shrunk-market world. Same contracts as the assumed-skill rows."),
        Scenario("under_truep_drift_flip", "underreact", "true_p", "assumed_positive_not_evidence", 500, 0.0, 0.02, "coin_flip_2c",
                 "Assumed skill, and half of IOC orders miss because the ask is illustratively 2 cents worse."),
        Scenario("under_truep_drift_always", "underreact", "true_p", "assumed_positive_not_evidence", 500, 0.0, 0.02, "always_worse_2c",
                 "Assumed skill, and every IOC misses a 2 cent adverse ask move."),
        Scenario("corr_truep_c500", "correlated_underreact", "true_p", "assumed_positive_not_evidence", 500, 0.0, 0.02, "none",
                 "Assumed skill, but each wave is 12 copies of one shared outcome."),
        Scenario("corr_over_c500", "correlated_efficient", "overconfident", "miscalibrated", 500, 0.0, 0.02, "none",
                 "Overconfident forecast, efficient mids, 12 copies of one shared outcome per wave."),
    ]


def _market_prob(world: str, true_p: float) -> float:
    if world in ("efficient", "correlated_efficient"):
        return true_p
    if world in ("underreact", "correlated_underreact"):
        return clip(0.5 + 0.45 * (true_p - 0.5), 0.12, 0.88)
    raise ValueError(world)


def build_contracts(scenario: Scenario, rep: int) -> List[Dict[str, Any]]:
    """Build offers and forecasts first. Draw outcomes only after forecasts exist."""
    world_rng = random.Random(SEED + rep * 10007 + _world_salt(scenario.world))
    noise_rng = random.Random(SEED + 500_000 + rep * 10007)
    contracts: List[Dict[str, Any]] = []
    for wave in range(N_WAVES):
        shared_p = world_rng.uniform(0.15, 0.85) if scenario.world.startswith("correlated_") else None
        for _j in range(PER_WAVE):
            true_p = shared_p if shared_p is not None else world_rng.uniform(0.15, 0.85)
            market_p = _market_prob(scenario.world, true_p)
            book = book_from_yes_probability(market_p, scenario.half_spread)
            ai = forecast_yes_pct(scenario.model, book["market_yes_prob"], true_p, noise_rng)
            contracts.append(
                {
                    "wave": wave,
                    "true_p": true_p,
                    "ai_yes_pct": ai,
                    "side": side_from_forecast(scenario.model, ai),
                    "book": book,
                    "y": None,
                }
            )
    # Outcomes after every forecast is stored.
    outcome_rng = random.Random(SEED + 800_000 + rep * 10007 + _world_salt(scenario.world))
    shared_y: Dict[int, int] = {}
    for c in contracts:
        if scenario.world.startswith("correlated_"):
            if c["wave"] not in shared_y:
                shared_y[c["wave"]] = 1 if outcome_rng.random() < c["true_p"] else 0
            c["y"] = shared_y[c["wave"]]
        else:
            c["y"] = 1 if outcome_rng.random() < c["true_p"] else 0
    return contracts


def _world_salt(world: str) -> int:
    return {
        "efficient": 11,
        "underreact": 29,
        "correlated_underreact": 47,
        "correlated_efficient": 71,
    }[world]


def forecast_metrics(contracts: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    model_pairs = [(c["ai_yes_pct"] / 100.0, float(c["y"])) for c in contracts]
    market_pairs = [(c["book"]["market_yes_prob"], float(c["y"])) for c in contracts]
    return {
        "n_forecasts": len(contracts),
        "brier_model": _brier(model_pairs),
        "brier_market": _brier(market_pairs),
        "logloss_model": _logloss(model_pairs),
        "logloss_market": _logloss(market_pairs),
        "accuracy_model": _accuracy(model_pairs),
        "accuracy_market": _accuracy(market_pairs),
    }


def simulate_trading(scenario: Scenario, contracts: List[Dict[str, Any]], rep: int) -> Dict[str, Any]:
    drift_rng = random.Random(SEED + 900_000 + rep * 10007 + _world_salt(scenario.world))
    cash = float(scenario.cash)
    wealth = [cash]
    fills = 0
    wins = 0
    profitable = 0
    net_pnl = 0.0
    reasons: Dict[str, int] = {}
    tags: Dict[str, int] = {}
    open_in_wave = 0
    current_wave = contracts[0]["wave"] if contracts else 0
    pending: List[Dict[str, Any]] = []

    def _settle() -> None:
        nonlocal cash, pending
        for pos in pending:
            payout = float(pos["qty"]) if pos["won"] else 0.0
            cash += payout
        pending = []

    for c in contracts:
        if c["wave"] != current_wave:
            _settle()
            wealth.append(cash)
            current_wave = c["wave"]
            open_in_wave = 0
        if open_in_wave >= MAX_OPEN:
            reasons["position_cap"] = reasons.get("position_cap", 0) + 1
            continue
        decision = decide_buy(
            cash=cash,
            ai_yes_pct=c["ai_yes_pct"],
            side=c["side"],
            book=c["book"],
            illustrative_fee=scenario.illustrative_fee,
        )
        if decision["action"] != "buy":
            reasons[decision["reason"]] = reasons.get(decision["reason"], 0) + 1
            continue
        px = fill_price(float(decision["ask"]), scenario.drift_mode, drift_rng)
        if px is None:
            reasons["ioc_miss"] = reasons.get("ioc_miss", 0) + 1
            continue
        qty = int(decision["qty"])
        cost = qty * (px + scenario.illustrative_fee)
        if cost > cash + 1e-9:
            reasons["cash_short"] = reasons.get("cash_short", 0) + 1
            continue
        won = (c["side"] == "YES" and c["y"] == 1) or (c["side"] == "NO" and c["y"] == 0)
        pnl = (qty if won else 0.0) - cost
        cash -= cost
        fills += 1
        wins += int(won)
        profitable += int(pnl > 1e-9)
        net_pnl += pnl
        tags[decision["tag"]] = tags.get(decision["tag"], 0) + 1
        open_in_wave += 1
        pending.append({"qty": qty, "won": won})
    _settle()
    wealth.append(cash)
    peak = wealth[0]
    max_dd = 0.0
    for w in wealth:
        peak = max(peak, w)
        if peak > 1e-12:
            max_dd = max(max_dd, (peak - w) / peak)
    wave_rets = []
    for a, b in zip(wealth, wealth[1:]):
        if a > 1e-12:
            wave_rets.append(b / a - 1.0)
    wave_ratio = None
    if len(wave_rets) >= 2:
        sd = statistics.pstdev(wave_rets)
        if sd > 1e-12:
            wave_ratio = statistics.fmean(wave_rets) / sd
    start = float(scenario.cash)
    return {
        "terminal_return": (cash - start) / start,
        "final_cash": cash,
        "max_drawdown": max_dd,
        "wave_return_ratio": wave_ratio,
        "fills": fills,
        "wins": wins,
        "profitable_fills": profitable,
        "net_pnl": net_pnl,
        "reasons": reasons,
        "tags": tags,
    }


def run_scenario(scenario: Scenario, n_reps: int = N_REPS) -> Dict[str, Any]:
    rep_rows = []
    forecast_acc = []
    for rep in range(n_reps):
        contracts = build_contracts(scenario, rep)
        fm = forecast_metrics(contracts)
        tr = simulate_trading(scenario, contracts, rep)
        tr["rep"] = rep
        tr["scenario_id"] = scenario.scenario_id
        tr["brier_model"] = fm["brier_model"]
        tr["brier_market"] = fm["brier_market"]
        forecast_acc.append(fm)
        rep_rows.append(tr)
    run_scenario.last_reps = getattr(run_scenario, "last_reps", []) + [
        {
            "scenario_id": scenario.scenario_id,
            "rep": r["rep"],
            "terminal_return": r["terminal_return"],
            "max_drawdown": r["max_drawdown"],
            "fills": r["fills"],
            "wins": r["wins"],
            "net_pnl": r["net_pnl"],
            "brier_model": r["brier_model"],
            "brier_market": r["brier_market"],
            "final_cash": r["final_cash"],
        }
        for r in rep_rows
    ]
    returns = [r["terminal_return"] for r in rep_rows]
    dds = [r["max_drawdown"] for r in rep_rows]
    fills = sum(r["fills"] for r in rep_rows)
    wins = sum(r["wins"] for r in rep_rows)
    profitable = sum(r["profitable_fills"] for r in rep_rows)
    net = sum(r["net_pnl"] for r in rep_rows)
    reason_totals: Dict[str, int] = {}
    tag_totals: Dict[str, int] = {}
    for r in rep_rows:
        for k, v in r["reasons"].items():
            reason_totals[k] = reason_totals.get(k, 0) + v
        for k, v in r["tags"].items():
            tag_totals[k] = tag_totals.get(k, 0) + v
    std_ret = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    brier_m = _blank_mean([f["brier_model"] for f in forecast_acc])
    brier_k = _blank_mean([f["brier_market"] for f in forecast_acc])
    n_contracts = n_reps * N_WAVES * PER_WAVE
    return {
        "scenario_id": scenario.scenario_id,
        "world": scenario.world,
        "model": scenario.model,
        "skill_label": scenario.skill_label,
        "description": scenario.description,
        "starting_cash_usd": scenario.cash,
        "illustrative_fee_usd_per_contract": scenario.illustrative_fee,
        "half_spread": scenario.half_spread,
        "drift_mode": scenario.drift_mode,
        "max_open_positions": MAX_OPEN,
        "n_reps": n_reps,
        "n_waves": N_WAVES,
        "contracts_per_wave": PER_WAVE,
        "n_forecasts": n_contracts,
        "brier_model": brier_m,
        "brier_market": brier_k,
        "brier_skill_vs_market": None
        if brier_m is None or brier_k is None or brier_k <= 1e-12
        else 1.0 - (brier_m / brier_k),
        "logloss_model": _blank_mean([f["logloss_model"] for f in forecast_acc]),
        "logloss_market": _blank_mean([f["logloss_market"] for f in forecast_acc]),
        "accuracy_model": _blank_mean([f["accuracy_model"] for f in forecast_acc]),
        "accuracy_market": _blank_mean([f["accuracy_market"] for f in forecast_acc]),
        "mean_terminal_return": statistics.fmean(returns),
        "std_terminal_return": std_ret,
        "p05_terminal_return": _pct(returns, 0.05),
        "p50_terminal_return": _pct(returns, 0.50),
        "p95_terminal_return": _pct(returns, 0.95),
        "return_mean_over_std": None if std_ret <= 1e-12 else statistics.fmean(returns) / std_ret,
        "mean_max_drawdown": statistics.fmean(dds),
        "total_fills": fills,
        "mean_fills_per_rep": fills / n_reps,
        "coverage": fills / n_contracts,
        "side_win_rate": None if fills == 0 else wins / fills,
        "profitable_fill_rate": None if fills == 0 else profitable / fills,
        "mean_net_pnl_per_fill": None if fills == 0 else net / fills,
        "total_net_pnl": net,
        "tag_full_kelly": tag_totals.get("full_kelly", 0) + tag_totals.get("full_kelly+fee_reserve", 0),
        "tag_cash_capped": tag_totals.get("cash_capped", 0) + tag_totals.get("cash_capped+fee_reserve", 0),
        "tag_single_contract": tag_totals.get("single_contract_retry", 0)
        + tag_totals.get("single_contract_retry+fee_reserve", 0),
        "skip_no_side": reason_totals.get("no_side", 0),
        "skip_min_edge": reason_totals.get("min_edge", 0),
        "skip_min_ai": reason_totals.get("min_ai", 0),
        "skip_edge_cap": reason_totals.get("edge_cap", 0),
        "skip_ai_cap": reason_totals.get("ai_cap", 0),
        "skip_min_entry": reason_totals.get("min_entry", 0),
        "skip_mid_price_guard": reason_totals.get("mid_price_guard", 0),
        "skip_stake_cap": reason_totals.get("stake_cap", 0),
        "skip_kelly_zero": reason_totals.get("kelly_zero", 0),
        "skip_fee_reserve": reason_totals.get("fee_reserve", 0),
        "ioc_misses": reason_totals.get("ioc_miss", 0),
        "skip_position_cap": reason_totals.get("position_cap", 0),
        "reps_with_a_fill": sum(1 for r in rep_rows if r["fills"] > 0),
    }


def no_trade_row() -> Dict[str, Any]:
    return {
        "scenario_id": "no_trade_cash",
        "world": "none",
        "model": "no_forecast",
        "skill_label": "cash_baseline",
        "description": "Do nothing. Terminal return is zero. This row has no forecast scores.",
        "starting_cash_usd": "",
        "illustrative_fee_usd_per_contract": 0.0,
        "half_spread": "",
        "drift_mode": "none",
        "max_open_positions": 0,
        "n_reps": N_REPS,
        "n_waves": N_WAVES,
        "contracts_per_wave": PER_WAVE,
        "n_forecasts": 0,
        "brier_model": None,
        "brier_market": None,
        "brier_skill_vs_market": None,
        "logloss_model": None,
        "logloss_market": None,
        "accuracy_model": None,
        "accuracy_market": None,
        "mean_terminal_return": 0.0,
        "std_terminal_return": 0.0,
        "p05_terminal_return": 0.0,
        "p50_terminal_return": 0.0,
        "p95_terminal_return": 0.0,
        "return_mean_over_std": None,
        "mean_max_drawdown": 0.0,
        "total_fills": 0,
        "mean_fills_per_rep": 0.0,
        "coverage": 0.0,
        "side_win_rate": None,
        "profitable_fill_rate": None,
        "mean_net_pnl_per_fill": None,
        "total_net_pnl": 0.0,
        "tag_full_kelly": 0,
        "tag_cash_capped": 0,
        "tag_single_contract": 0,
        "skip_no_side": 0,
        "skip_min_edge": 0,
        "skip_min_ai": 0,
        "skip_edge_cap": 0,
        "skip_ai_cap": 0,
        "skip_min_entry": 0,
        "skip_mid_price_guard": 0,
        "skip_stake_cap": 0,
        "skip_kelly_zero": 0,
        "skip_fee_reserve": 0,
        "ioc_misses": 0,
        "skip_position_cap": 0,
        "reps_with_a_fill": 0,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.8g}"
    return str(v)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    fields: List[str] = []
    seen = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _fmt(row.get(k)) for k in fields})


def write_svg_returns(path: Path, rows: List[Dict[str, Any]]) -> None:
    plotted = [r for r in rows if r["scenario_id"] != "no_trade_cash" and r["total_fills"] is not None]
    if not plotted:
        return
    width, height = 1100, 42 * len(plotted) + 70
    lo = min(r["p05_terminal_return"] for r in plotted)
    hi = max(r["p95_terminal_return"] for r in plotted)
    span = max(hi - lo, 1e-6)
    left, right = 280, width - 40
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="20" y="28" font-family="sans-serif" font-size="16">Synthetic terminal return by scenario</text>',
        '<text x="20" y="48" font-family="sans-serif" font-size="12" fill="#444">Dot is the mean. Bar is the 5th to 95th percentile across replications. Not historical P&amp;L.</text>',
    ]
    zero_x = left + (0 - lo) / span * (right - left)
    lines.append(f'<line x1="{zero_x:.1f}" y1="60" x2="{zero_x:.1f}" y2="{height - 10}" stroke="#999"/>')
    for i, r in enumerate(plotted):
        y = 70 + i * 42
        x1 = left + (r["p05_terminal_return"] - lo) / span * (right - left)
        x2 = left + (r["p95_terminal_return"] - lo) / span * (right - left)
        xm = left + (r["mean_terminal_return"] - lo) / span * (right - left)
        lines.append(f'<text x="12" y="{y + 4}" font-family="sans-serif" font-size="11">{r["scenario_id"]}</text>')
        lines.append(f'<line x1="{x1:.1f}" y1="{y}" x2="{x2:.1f}" y2="{y}" stroke="#1f4b99" stroke-width="4"/>')
        lines.append(f'<circle cx="{xm:.1f}" cy="{y}" r="4" fill="#111"/>')
    lines.append("</svg>")
    path.write_text("\n".join(lines))


def write_svg_brier(path: Path, rows: List[Dict[str, Any]]) -> None:
    # One row per model/world, take the fee=0 cash=500 drift=none spread=0.02 representative.
    picked = []
    seen = set()
    for r in rows:
        if r["illustrative_fee_usd_per_contract"] not in (0, 0.0):
            continue
        if r["drift_mode"] != "none" or r["half_spread"] != 0.02:
            continue
        if r["starting_cash_usd"] not in (500, 500.0):
            continue
        key = (r["world"], r["model"])
        if key in seen or r["brier_model"] is None:
            continue
        seen.add(key)
        picked.append(r)
    if not picked:
        return
    width, height = 900, 36 * len(picked) + 80
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="16" y="28" font-family="sans-serif" font-size="16">Brier score (lower is better) on every synthetic contract</text>',
        '<text x="16" y="48" font-family="sans-serif" font-size="12" fill="#444">Dark bar: simulated forecast. Light bar: market probability. Not a live model.</text>',
    ]
    max_b = max(max(r["brier_model"], r["brier_market"]) for r in picked)
    max_b = max(max_b, 1e-6)
    for i, r in enumerate(picked):
        y = 70 + i * 36
        label = f'{r["world"]} / {r["model"]}'
        w1 = 420 * (r["brier_model"] / max_b)
        w2 = 420 * (r["brier_market"] / max_b)
        lines.append(f'<text x="16" y="{y}" font-family="sans-serif" font-size="11">{label}</text>')
        lines.append(f'<rect x="280" y="{y - 12}" width="{w1:.1f}" height="8" fill="#1f4b99"/>')
        lines.append(f'<rect x="280" y="{y - 2}" width="{w2:.1f}" height="8" fill="#b7c7e3"/>')
        lines.append(
            f'<text x="{290 + max(w1, w2):.1f}" y="{y}" font-family="sans-serif" font-size="11">{r["brier_model"]:.3f} vs {r["brier_market"]:.3f}</text>'
        )
    lines.append("</svg>")
    path.write_text("\n".join(lines))


def assumptions_payload(n_reps: int) -> Dict[str, Any]:
    return {
        "study": "offline synthetic stress of corrected buy gates",
        "safety_commit_expected_prefix": EXPECTED_SAFETY_COMMIT_PREFIX,
        "seed": SEED,
        "n_reps": n_reps,
        "n_waves": N_WAVES,
        "contracts_per_wave": PER_WAVE,
        "contracts_per_replication": N_WAVES * PER_WAVE,
        "min_edge_points": MIN_EDGE,
        "min_ai_win_prob_on_buy_side": MIN_AI,
        "max_open_positions_per_wave": MAX_OPEN,
        "position_cap_note": "Waves hold at most 12 contracts, so the 30-position cap does not bind. It is still passed through the same constant.",
        "true_p": "Independent Uniform(0.15, 0.85), except correlated worlds share one probability and one outcome per wave.",
        "efficient_market": "Market probability equals true probability before the spread.",
        "underreacting_market": "Market probability = clip(0.5 + 0.45*(true_p-0.5), 0.12, 0.88). This shrinkage is an assumption.",
        "half_spread_default": 0.02,
        "book": "yes_ask = mid + half_spread, yes_bid = mid - half_spread, NO is the complement.",
        "fill": "Full size at the scanned ask if the later ask is not worse. No partial-book walk.",
        "settlement": "Held to settlement. Winning contract pays $1, losing pays $0. Stop-loss is not simulated because the product default is off.",
        "fees": "Illustrative dollars per contract subtracted from cash. Not a verified Kalshi fee schedule. Values used: 0, 0.01, 0.02.",
        "drift": "none, or a 2 cent worse ask that the IOC limit (the original ask) does not pay.",
        "sizing": "Production Kelly, 5% cash cap, one-contract retry when the fraction is positive but rounds to zero, and the small-account skip when the cap cannot buy one contract.",
        "forecasts_do_not_see_outcomes": True,
        "historical_bot_rows_used": 0,
        "return_definition": "Terminal return = (cash after the last settlement - starting cash) / starting cash. One return per replication.",
        "drawdown_definition": "Peak-to-trough fraction on wealth after each wave settlement, including the starting cash.",
        "risk_adjusted_definition": "return_mean_over_std = mean(terminal return across replications) / population std of those returns. Not annualized. Blank when the std is 0.",
        "brier_definition": "Mean squared error of P(YES) versus the YES outcome, on every contract, traded or not.",
        "logloss_clip": P_CLIP,
        "accuracy_definition": "Share of forecasts strictly on the correct side of 50%. An exact 50% forecast is not counted as correct.",
        "win_rate_definition": "Share of fills whose bought side won. Blank when there are no fills. A win can still lose money after the ask and the illustrative fee.",
    }


def run_study(out_dir: Path, n_reps: int = N_REPS) -> List[Dict[str, Any]]:
    assert_corrected_stake_cap()
    out_dir.mkdir(parents=True, exist_ok=True)
    run_scenario.last_reps = []
    rows = [no_trade_row()]
    for scenario in scenario_catalog():
        print(f"running {scenario.scenario_id} ...", flush=True)
        rows.append(run_scenario(scenario, n_reps=n_reps))
    if run_scenario.last_reps:
        write_csv(out_dir / "replications.csv", run_scenario.last_reps)
    write_csv(out_dir / "summary.csv", rows)
    (out_dir / "assumptions.json").write_text(json.dumps(assumptions_payload(n_reps), indent=2))
    write_svg_returns(out_dir / "terminal_return_by_scenario.svg", rows)
    write_svg_brier(out_dir / "brier_model_vs_market.svg", rows)
    return rows


def self_check() -> None:
    assert_corrected_stake_cap()
    # Oracle equal to an efficient mid does not clear a 5 point edge over the ask.
    book = book_from_yes_probability(0.60, 0.02)
    d = decide_buy(cash=500, ai_yes_pct=60, side="YES", book=book, illustrative_fee=0.0)
    if d["action"] != "skip" or d["reason"] != "min_edge":
        raise SystemExit(f"self-check failed: honest efficient quote should skip, got {d}")
    book0 = book_from_yes_probability(0.60, 0.0)
    d0 = decide_buy(cash=500, ai_yes_pct=60, side="YES", book=book0, illustrative_fee=0.0)
    if d0["action"] != "skip":
        raise SystemExit(f"self-check failed: zero-spread honest quote should still skip, got {d0}")
    # Small account cannot buy a 67 cent contract inside the 5% cap.
    book_hi = book_from_yes_probability(0.65, 0.02)
    # Force the ask path by calling the cap directly and a rich YES forecast.
    d_small = decide_buy(cash=10.0, ai_yes_pct=80, side="YES", book=book_hi, illustrative_fee=0.0)
    if book_hi["yes_ask"] > 0.50 and d_small["reason"] != "stake_cap":
        raise SystemExit(f"self-check failed: expected stake_cap skip, got {d_small} ask={book_hi['yes_ask']}")
    # Forecast ignores a planted outcome.
    rng = random.Random(1)
    a = forecast_yes_pct("null_noise", 0.4, 0.7, rng)
    rng2 = random.Random(1)
    b = forecast_yes_pct("null_noise", 0.4, 0.7, rng2)
    if a != b:
        raise SystemExit("self-check failed: forecast rng not deterministic")
    print("self-check ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline synthetic edge stress test")
    parser.add_argument("--out", type=Path, default=Path("/opt/cursor/artifacts/edge-eval"))
    parser.add_argument("--reps", type=int, default=N_REPS)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows = run_study(args.out, n_reps=args.reps)
    print(f"wrote {len(rows)} scenario rows to {args.out}")


if __name__ == "__main__":
    main()
