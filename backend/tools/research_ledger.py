#!/usr/bin/env python3
"""Observation-only ledger for later forecast scoring.

This file does not import the trading bot, Kalshi client, or settings.
It cannot submit or cancel orders. It does not read API keys.

Records are append-only. A forecast identity is (market_id, forecast_time,
model_id). Replaying the same bytes is a no-op. A different payload for the
same identity is rejected. Outcomes are joined only when the official resolve
time is strictly after the forecast time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

P_CLIP = 1e-3
_SECRET_HINTS = ("api_key", "private_key", "begin private", "bearer ", "kalshi-access")


class LedgerError(Exception):
    """Rejected input. The ledger files are left unchanged."""


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{field} must be an ISO-8601 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LedgerError(f"{field} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise LedgerError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _price(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LedgerError(f"{field} must be a number between 0 and 1")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0 or number >= 1.0:
        raise LedgerError(f"{field} must be between 0 and 1, exclusive")
    return number


def _prob(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LedgerError("ai_probability_yes must be a probability from 0 to 1")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise LedgerError("ai_probability_yes must be from 0 to 1")
    return number


def _text(value: object, field: str, limit: int = 80) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{field} is required")
    text = " ".join(value.split())
    if any(hint in text.lower() for hint in _SECRET_HINTS):
        raise LedgerError(f"{field} looks like a secret and was rejected")
    if len(text) > limit:
        raise LedgerError(f"{field} is longer than {limit} characters")
    return text


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sha256(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LedgerError(f"missing file {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{path.name} is not valid JSON") from exc


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path.name} line {line_no} is not valid JSON") from exc
        if not isinstance(item, dict):
            raise LedgerError(f"{path.name} line {line_no} must be an object")
        rows.append(item)
    return rows


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(payload) + "\n")


def validate_experiment(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerError("experiment config must be a JSON object")
    experiment_id = _text(raw.get("experiment_id"), "experiment_id", 80)
    model_id = _text(raw.get("model_id"), "model_id", 80)
    score_not_before = _parse_time(raw.get("score_not_before"), "score_not_before")
    try:
        max_age = int(raw.get("max_quote_age_seconds"))
    except (TypeError, ValueError) as exc:
        raise LedgerError("max_quote_age_seconds must be a positive integer") from exc
    if max_age <= 0 or max_age > 86400:
        raise LedgerError("max_quote_age_seconds must be from 1 to 86400")
    hypo = raw.get("hypothetical")
    if not isinstance(hypo, dict):
        raise LedgerError("hypothetical assumptions are required")
    try:
        min_edge = float(hypo.get("min_edge_points"))
        fee = float(hypo.get("illustrative_fee_per_contract_usd"))
        draws = int(raw.get("bootstrap_draws"))
        seed = int(raw.get("bootstrap_seed"))
    except (TypeError, ValueError) as exc:
        raise LedgerError("hypothetical edge, fee, bootstrap_draws, and bootstrap_seed are required numbers") from exc
    if min_edge < 0 or fee < 0:
        raise LedgerError("min_edge_points and illustrative fee cannot be negative")
    if draws < 0 or draws > 5000:
        raise LedgerError("bootstrap_draws must be from 0 to 5000")
    clean = {
        "experiment_id": experiment_id,
        "model_id": model_id,
        "score_not_before": _iso(score_not_before),
        "max_quote_age_seconds": max_age,
        "bootstrap_draws": draws,
        "bootstrap_seed": seed,
        "hypothetical": {
            "min_edge_points": min_edge,
            "illustrative_fee_per_contract_usd": fee,
            "contracts": 1,
            "label": "hypothetical_one_contract_not_a_live_order",
        },
    }
    return clean


def normalize_snapshot(raw: Dict[str, Any], experiment: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerError("snapshot must be a JSON object")
    model_id = _text(raw.get("model_id"), "model_id", 80)
    if model_id != experiment["model_id"]:
        raise LedgerError("model_id does not match the frozen experiment")
    forecast_at = _parse_time(raw.get("forecast_time"), "forecast_time")
    quote_at = _parse_time(raw.get("quote_time"), "quote_time")
    if quote_at > forecast_at:
        raise LedgerError("quote_time is after forecast_time")
    age = (forecast_at - quote_at).total_seconds()
    if age > experiment["max_quote_age_seconds"]:
        raise LedgerError("quote is older than max_quote_age_seconds")
    yes_bid = _price(raw.get("yes_bid"), "yes_bid")
    yes_ask = _price(raw.get("yes_ask"), "yes_ask")
    no_bid = _price(raw.get("no_bid"), "no_bid")
    no_ask = _price(raw.get("no_ask"), "no_ask")
    if yes_bid > yes_ask or no_bid > no_ask:
        raise LedgerError("bid cannot be above ask")
    version = raw.get("model_version")
    if version is None or (isinstance(version, str) and not version.strip()):
        version_text = ""
    else:
        version_text = _text(version, "model_version", 80)
    row = {
        "market_id": _text(raw.get("market_id"), "market_id", 120),
        "event_ticker": _text(raw.get("event_ticker"), "event_ticker", 120),
        "forecast_time": _iso(forecast_at),
        "quote_time": _iso(quote_at),
        "model_id": model_id,
        "model_version": version_text,
        "ai_probability_yes": _prob(raw.get("ai_probability_yes")),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
    }
    row["record_sha256"] = _sha256(row)
    return row


def normalize_outcome(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerError("outcome must be a JSON object")
    result = raw.get("result")
    if not isinstance(result, str) or result.strip().lower() not in ("yes", "no"):
        raise LedgerError("result must be yes or no")
    resolved = _parse_time(raw.get("resolved_at"), "resolved_at")
    row = {
        "market_id": _text(raw.get("market_id"), "market_id", 120),
        "resolved_at": _iso(resolved),
        "result": result.strip().lower(),
    }
    row["record_sha256"] = _sha256(row)
    return row


class ResearchLedger:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.experiment_path = self.directory / "experiment.json"
        self.hash_path = self.directory / "experiment.sha256"
        self.forecast_path = self.directory / "forecasts.jsonl"
        self.outcome_path = self.directory / "outcomes.jsonl"

    def init(self, experiment: Dict[str, Any]) -> Dict[str, Any]:
        if self.experiment_path.exists():
            raise LedgerError("ledger already exists; refusing to rewrite the frozen experiment")
        clean = validate_experiment(experiment)
        self.directory.mkdir(parents=True, exist_ok=True)
        text = _canonical(clean)
        self.experiment_path.write_text(text + "\n", encoding="utf-8")
        self.hash_path.write_text(_sha256(clean) + "\n", encoding="utf-8")
        self.forecast_path.touch()
        self.outcome_path.touch()
        return {"status": "created", "experiment_sha256": _sha256(clean)}

    def load_experiment(self) -> Dict[str, Any]:
        raw = _read_json(self.experiment_path)
        clean = validate_experiment(raw)
        expected = self.hash_path.read_text(encoding="utf-8").strip() if self.hash_path.exists() else ""
        actual = _sha256(clean)
        if expected != actual:
            raise LedgerError("experiment.json does not match the frozen hash; scoring is refused")
        return clean

    def record_snapshot(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        experiment = self.load_experiment()
        row = normalize_snapshot(raw, experiment)
        existing = {_identity(item): item for item in _read_jsonl(self.forecast_path)}
        key = _identity(row)
        if key in existing:
            if existing[key].get("record_sha256") == row["record_sha256"]:
                return {"status": "duplicate", "record_sha256": row["record_sha256"]}
            raise LedgerError("forecast identity already recorded with different contents")
        outcome = _outcomes_by_market(_read_jsonl(self.outcome_path)).get(row["market_id"])
        if outcome is not None:
            resolved = _parse_time(outcome["resolved_at"], "resolved_at")
            forecast_at = _parse_time(row["forecast_time"], "forecast_time")
            if forecast_at >= resolved:
                raise LedgerError("forecast_time is not before the stored official resolve time")
        _append_jsonl(self.forecast_path, row)
        return {"status": "recorded", "record_sha256": row["record_sha256"]}

    def record_outcome(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        self.load_experiment()
        row = normalize_outcome(raw)
        existing = _outcomes_by_market(_read_jsonl(self.outcome_path))
        prior = existing.get(row["market_id"])
        if prior is not None:
            if prior.get("record_sha256") == row["record_sha256"]:
                return {"status": "duplicate", "record_sha256": row["record_sha256"]}
            raise LedgerError("outcome already recorded with different contents")
        resolved = _parse_time(row["resolved_at"], "resolved_at")
        for forecast in _read_jsonl(self.forecast_path):
            if forecast.get("market_id") != row["market_id"]:
                continue
            try:
                forecast_at = _parse_time(forecast.get("forecast_time"), "forecast_time")
            except LedgerError:
                continue
            if forecast_at >= resolved:
                raise LedgerError("outcome time is not after every stored forecast for this market")
        _append_jsonl(self.outcome_path, row)
        return {"status": "recorded", "record_sha256": row["record_sha256"]}

    def record_outcomes(self, raws: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validate every outcome before appending any of them."""
        self.load_experiment()
        rows = [normalize_outcome(raw) for raw in raws]
        existing = _outcomes_by_market(_read_jsonl(self.outcome_path))
        forecasts = _read_jsonl(self.forecast_path)
        staged = dict(existing)
        plan: List[Dict[str, Any]] = []
        for row in rows:
            prior = staged.get(row["market_id"])
            if prior is not None:
                if prior.get("record_sha256") == row["record_sha256"]:
                    plan.append({"status": "duplicate", "record_sha256": row["record_sha256"]})
                    continue
                raise LedgerError("outcome already recorded with different contents")
            resolved = _parse_time(row["resolved_at"], "resolved_at")
            for forecast in forecasts:
                if forecast.get("market_id") != row["market_id"]:
                    continue
                try:
                    forecast_at = _parse_time(forecast.get("forecast_time"), "forecast_time")
                except LedgerError:
                    continue
                if forecast_at >= resolved:
                    raise LedgerError("outcome time is not after every stored forecast for this market")
            staged[row["market_id"]] = row
            plan.append({"status": "recorded", "record_sha256": row["record_sha256"], "_row": row})
        results = []
        for item in plan:
            row = item.get("_row")
            if row is not None:
                _append_jsonl(self.outcome_path, row)
            results.append({k: v for k, v in item.items() if k != "_row"})
        return results

    def score(self) -> Dict[str, Any]:
        experiment = self.load_experiment()
        forecasts = _read_jsonl(self.forecast_path)
        outcomes = _outcomes_by_market(_read_jsonl(self.outcome_path))
        usable, counts = _join(forecasts, outcomes, experiment)
        cutoff = _parse_time(experiment["score_not_before"], "score_not_before")
        exploratory = [row for row in usable if _parse_time(row["forecast_time"], "forecast_time") < cutoff]
        official = [row for row in usable if _parse_time(row["forecast_time"], "forecast_time") >= cutoff]
        report = {
            "experiment_id": experiment["experiment_id"],
            "experiment_sha256": _sha256(experiment),
            "label": "offline_research_scorecard_not_live_trading",
            "counts": counts,
            "exploratory_not_the_result": _metrics(exploratory, experiment),
            "pre_registered_scorecard": _metrics(official, experiment),
        }
        return report


def _identity(row: Dict[str, Any]) -> Tuple[str, str, str]:
    return (row["market_id"], row["forecast_time"], row["model_id"])


def _outcomes_by_market(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        market_id = str(row.get("market_id") or "")
        if market_id in found and found[market_id].get("record_sha256") != row.get("record_sha256"):
            raise LedgerError(f"conflicting outcomes stored for {market_id}")
        found[market_id] = row
    return found


def _join(
    forecasts: List[Dict[str, Any]],
    outcomes: Dict[str, Dict[str, Any]],
    experiment: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    counts = {
        "forecast_rows": len(forecasts),
        "malformed": 0,
        "missing_outcome": 0,
        "leakage_excluded": 0,
        "stale_excluded": 0,
        "joined": 0,
    }
    usable = []
    for raw in forecasts:
        try:
            row = normalize_snapshot(raw, experiment)
        except LedgerError:
            counts["malformed"] += 1
            continue
        stored_hash = raw.get("record_sha256")
        if stored_hash != row["record_sha256"]:
            counts["malformed"] += 1
            continue
        outcome = outcomes.get(row["market_id"])
        if outcome is None:
            counts["missing_outcome"] += 1
            continue
        try:
            resolved = _parse_time(outcome.get("resolved_at"), "resolved_at")
            result = str(outcome.get("result") or "").lower()
            if result not in ("yes", "no"):
                raise LedgerError("bad result")
        except LedgerError:
            counts["malformed"] += 1
            continue
        forecast_at = _parse_time(row["forecast_time"], "forecast_time")
        quote_at = _parse_time(row["quote_time"], "quote_time")
        if (forecast_at - quote_at).total_seconds() > experiment["max_quote_age_seconds"]:
            counts["stale_excluded"] += 1
            continue
        if forecast_at >= resolved:
            counts["leakage_excluded"] += 1
            continue
        yes_mid = (row["yes_bid"] + row["yes_ask"]) / 2.0
        usable.append({**row, "result_yes": 1.0 if result == "yes" else 0.0, "market_yes_prob": yes_mid})
        counts["joined"] += 1
    return usable, counts


def _metrics(rows: List[Dict[str, Any]], experiment: Dict[str, Any]) -> Dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "brier_model": None,
            "brier_market_mid": None,
            "logloss_model": None,
            "logloss_market_mid": None,
            "accuracy_model": None,
            "accuracy_market_mid": None,
            "calibration_model": [],
            "calibration_market_mid": [],
            "hypothetical": {"n_trades": 0, "side_win_rate": None, "total_pnl_usd": None},
            "event_bootstrap": {"status": "undefined", "reason": "no joined forecasts in this window"},
        }
    model_pairs = [(row["ai_probability_yes"], row["result_yes"]) for row in rows]
    market_pairs = [(row["market_yes_prob"], row["result_yes"]) for row in rows]
    trades = [_hypothetical_trade(row, experiment) for row in rows]
    trades = [trade for trade in trades if trade is not None]
    return {
        "n": len(rows),
        "brier_model": _brier(model_pairs),
        "brier_market_mid": _brier(market_pairs),
        "logloss_model": _logloss(model_pairs),
        "logloss_market_mid": _logloss(market_pairs),
        "accuracy_model": _accuracy(model_pairs),
        "accuracy_market_mid": _accuracy(market_pairs),
        "calibration_model": _calibration(model_pairs),
        "calibration_market_mid": _calibration(market_pairs),
        "hypothetical": _trade_summary(trades),
        "event_bootstrap": _bootstrap(rows, experiment),
    }


def _hypothetical_trade(row: Dict[str, Any], experiment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One-contract what-if. This does not call the live sizer and does not send an order."""
    probability = float(row["ai_probability_yes"])
    if probability == 0.5:
        return None
    side = "YES" if probability > 0.5 else "NO"
    ask = float(row["yes_ask"] if side == "YES" else row["no_ask"])
    side_prob = probability if side == "YES" else 1.0 - probability
    edge_points = 100.0 * side_prob - 100.0 * ask
    minimum = float(experiment["hypothetical"]["min_edge_points"])
    if edge_points + 1e-12 < minimum:
        return None
    fee = float(experiment["hypothetical"]["illustrative_fee_per_contract_usd"])
    won = (side == "YES" and row["result_yes"] == 1.0) or (side == "NO" and row["result_yes"] == 0.0)
    pnl = (1.0 if won else 0.0) - ask - fee
    return {"event_ticker": row["event_ticker"], "won": won, "pnl": pnl}


def _trade_summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"n_trades": 0, "side_win_rate": None, "total_pnl_usd": None, "mean_pnl_usd": None}
    wins = sum(1 for trade in trades if trade["won"])
    total = sum(trade["pnl"] for trade in trades)
    return {
        "n_trades": len(trades),
        "side_win_rate": wins / len(trades),
        "total_pnl_usd": total,
        "mean_pnl_usd": total / len(trades),
        "label": "hypothetical_one_contract_if_edge_clears_frozen_threshold",
    }


def _bootstrap(rows: List[Dict[str, Any]], experiment: Dict[str, Any]) -> Dict[str, Any]:
    by_event: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_event.setdefault(row["event_ticker"], []).append(row)
    events = sorted(by_event)
    draws = int(experiment["bootstrap_draws"])
    if len(events) < 2 or draws < 2:
        return {
            "status": "undefined",
            "reason": "need at least 2 event groups and 2 bootstrap draws",
            "n_events": len(events),
        }
    rng = random.Random(int(experiment["bootstrap_seed"]))
    brier_gaps = []
    pnls = []
    for _ in range(draws):
        chosen = [rng.choice(events) for _ in events]
        sample: List[Dict[str, Any]] = []
        for event_id in chosen:
            sample.extend(by_event[event_id])
        model_brier = _brier((row["ai_probability_yes"], row["result_yes"]) for row in sample)
        market_brier = _brier((row["market_yes_prob"], row["result_yes"]) for row in sample)
        trades = [trade for trade in (_hypothetical_trade(row, experiment) for row in sample) if trade]
        brier_gaps.append(model_brier - market_brier)
        pnls.append(sum(trade["pnl"] for trade in trades))
    return {
        "status": "ok",
        "n_events": len(events),
        "draws": draws,
        "sampling": "resample event groups with replacement; forecasts inside an event stay together",
        "brier_model_minus_market_p05": _percentile(brier_gaps, 0.05),
        "brier_model_minus_market_p50": _percentile(brier_gaps, 0.50),
        "brier_model_minus_market_p95": _percentile(brier_gaps, 0.95),
        "hypothetical_pnl_p05": _percentile(pnls, 0.05),
        "hypothetical_pnl_p50": _percentile(pnls, 0.50),
        "hypothetical_pnl_p95": _percentile(pnls, 0.95),
    }


def _brier(pairs: Iterable[Tuple[float, float]]) -> float:
    rows = list(pairs)
    return sum((p - y) ** 2 for p, y in rows) / len(rows)


def _logloss(pairs: Iterable[Tuple[float, float]]) -> float:
    total = 0.0
    rows = list(pairs)
    for probability, outcome in rows:
        clipped = min(1.0 - P_CLIP, max(P_CLIP, probability))
        total += -(outcome * math.log(clipped) + (1.0 - outcome) * math.log(1.0 - clipped))
    return total / len(rows)


def _accuracy(pairs: Iterable[Tuple[float, float]]) -> float:
    rows = list(pairs)
    good = 0
    for probability, outcome in rows:
        if probability > 0.5:
            good += int(outcome == 1.0)
        elif probability < 0.5:
            good += int(outcome == 0.0)
    return good / len(rows)


def _calibration(pairs: Iterable[Tuple[float, float]]) -> List[Dict[str, float]]:
    rows = list(pairs)
    bins = []
    edges = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0000001)
    for lo, hi in zip(edges, edges[1:]):
        group = [(p, y) for p, y in rows if lo <= p < hi]
        if not group:
            continue
        bins.append(
            {
                "bin_low": lo,
                "bin_high": min(hi, 1.0),
                "n": len(group),
                "mean_predicted": sum(p for p, _ in group) / len(group),
                "observed_yes_rate": sum(y for _, y in group) / len(group),
            }
        )
    return bins


def _percentile(values: List[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def render_markdown(report: Dict[str, Any]) -> str:
    score = report["pre_registered_scorecard"]
    counts = report["counts"]
    lines = [
        "# Research scorecard",
        "",
        "This is an offline join of supplied forecasts and supplied outcomes. It is not a live trading result.",
        "",
        f"Experiment `{report['experiment_id']}` hash `{report['experiment_sha256'][:12]}`.",
        "",
        "## Rows",
        "",
        f"- Forecast rows: {counts['forecast_rows']}",
        f"- Joined: {counts['joined']}",
        f"- Missing outcome: {counts['missing_outcome']}",
        f"- Excluded because resolve time was not after the forecast: {counts['leakage_excluded']}",
        f"- Excluded as stale or malformed: {counts['stale_excluded'] + counts['malformed']}",
        "",
        "## Pre-registered window",
        "",
    ]
    if score["n"] == 0:
        lines.append("No joined forecasts fall in the pre-registered window. Accuracy, Brier, and hypothetical P&L are omitted.")
    else:
        lines.extend(
            [
                f"- N: {score['n']}",
                f"- Brier model: {score['brier_model']:.4f}",
                f"- Brier market mid: {score['brier_market_mid']:.4f}",
                f"- Log loss model: {score['logloss_model']:.4f}",
                f"- Log loss market mid: {score['logloss_market_mid']:.4f}",
                f"- Accuracy model: {score['accuracy_model']:.4f}",
                f"- Accuracy market mid: {score['accuracy_market_mid']:.4f}",
            ]
        )
        hypo = score["hypothetical"]
        if hypo["n_trades"] == 0:
            lines.append("- Hypothetical trades: 0. Win rate and P&L are omitted.")
        else:
            lines.append(
                f"- Hypothetical one-contract trades: {hypo['n_trades']}, "
                f"side win rate {hypo['side_win_rate']:.4f}, total P&L ${hypo['total_pnl_usd']:.4f}."
            )
            lines.append("- That P&L uses the frozen illustrative fee. It was not sent to an exchange.")
        boot = score["event_bootstrap"]
        if boot.get("status") != "ok":
            lines.append(f"- Event-block uncertainty: {boot.get('reason')}.")
        else:
            lines.append(
                "- Event-block bootstrap of model Brier minus market Brier, "
                f"5th to 95th: {boot['brier_model_minus_market_p05']:.4f} to {boot['brier_model_minus_market_p95']:.4f}."
            )
    lines.extend(["", "Exploratory rows before the cutoff are stored in the JSON scorecard and are not the result.", ""])
    return "\n".join(lines)


def _load_input(path: Path) -> Any:
    raw = _read_json(path)
    return raw


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Observation-only forecast ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    init_p = sub.add_parser("init")
    init_p.add_argument("--dir", type=Path, required=True)
    init_p.add_argument("--config", type=Path, required=True)
    rec_p = sub.add_parser("record")
    rec_p.add_argument("--dir", type=Path, required=True)
    rec_p.add_argument("--snapshot", type=Path, required=True)
    res_p = sub.add_parser("resolve")
    res_p.add_argument("--dir", type=Path, required=True)
    res_p.add_argument("--outcome", type=Path, required=True)
    score_p = sub.add_parser("score")
    score_p.add_argument("--dir", type=Path, required=True)
    score_p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        ledger = ResearchLedger(args.dir)
        if args.command == "init":
            result = ledger.init(_load_input(args.config))
        elif args.command == "record":
            result = ledger.record_snapshot(_load_input(args.snapshot))
        elif args.command == "resolve":
            payload = _load_input(args.outcome)
            items = payload if isinstance(payload, list) else [payload]
            result = ledger.record_outcomes(items)
        else:
            report = ledger.score()
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "scorecard.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            (args.out / "scorecard.md").write_text(render_markdown(report), encoding="utf-8")
            result = {"status": "scored", "out": str(args.out)}
    except LedgerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
