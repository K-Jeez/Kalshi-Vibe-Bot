"""Market scan, analysis, execution, and position monitoring (live: IOC entries, Kalshi-aligned exits)."""

import asyncio
import json

from src.json_safe import dumps_payload
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

from src.clients.kalshi_client import (
    buy_side_liquidity_skip_summary,
    executable_buy_best_ask_dollars,
    live_best_bid_dollars,
    open_position_estimated_mark_dollars,
    open_position_mark_dollars,
)
from src.activity_log import (
    decision_model_usage,
    fresh_unresolved_order_hold,
    make_order_submit_gate,
    persist_order_intent,
    quote_snapshot,
    record_activity,
    record_order_ack,
    scrub_text,
)
from src.analysis_payload import enrich_analysis_ai_provider
from src.app_state import kalshi_ui_reconcile_lock
from src.config import (
    DEFAULT_MIN_AI_WIN_PROB_BUY_SIDE_PCT,
    DEFAULT_MIN_EDGE_TO_BUY_PCT,
    TRADE_MODE,
)
from src.logger import setup_logging
from src.util.datetimes import ensure_utc, utc_iso_z, utc_now
from src.reconcile.ledger_fifo import fifo_cost_for_next_sell
from src.reconcile.kalshi_positions import (
    mark_position_kalshi_flat_reconcile_pending,
    pick_display_expected_expiration_iso,
    sync_open_position_qty_cost_from_kalshi,
    sync_position_expiry_from_market,
)
from src.reconcile.open_positions import (
    closed_leg_realized_pnl_kalshi_dollars,
    normalize_market_id,
    open_position_cash_basis_dollars,
    resolution_intrinsic_mark_dollars,
)
from src.decision_engine.market_resolution_context import enrich_ai_market_description
from src.decision_engine.strategy_gates import (
    autonomous_buy_gate_failure,
    effective_min_edge_for_market,
    effective_scan_min_volume,
    all_in_entry_dollars,
    premodel_entry_block,
    fee_inclusive_ev_dollars,
    fee_schedule_from_mapping,
)
from src.decision_engine.strategy_math import (
    ai_win_prob_pct_on_buy_side,
    edge_pct_for_side,
    effective_buy_gate_thresholds,
)
from src.ledger_money import whole_contracts
from src.bot.order_outcome import buy_fill_status, classify_routing_error
from src.bot.routing_cooldown import note_untradeable, untradeable_reason
from src.bot.fill_bookkeeping import apply_cumulative_fill, ensure_fill_trade
from src.bot.order_reconcile import reconcile_open_intents
from src.bot.supervision import market_execution_lock, run_parallel_cycles, submission_reservation_lock
from src.bot.event_batch_partition import (
    LINE_LADDER_MAX_LEGS_FOR_XAI,
    group_markets_by_event_batch_partition,
    shortlist_line_ladder_members_for_xai,
)

logger = setup_logging("bot.loop")

# Throttle Kalshi ``GET /markets`` backfill for closed rows missing ``kalshi_market_result``.
_CLOSED_RESOLUTION_REFRESH_LAST_MONO = 0.0


async def _maybe_auto_refresh_closed_resolution(
    kalshi_client,
    db_factory,
    broadcast_fn: Callable[[dict], Coroutine],
    settings,
) -> None:
    """Poll Kalshi for outcomes on recent closes that still lack ``yes``/``no`` (stop-loss friendly)."""
    global _CLOSED_RESOLUTION_REFRESH_LAST_MONO
    iv = int(getattr(settings, "closed_resolution_refresh_interval_sec", 0) or 0)
    if iv <= 0 or kalshi_client is None:
        return
    now = time.monotonic()
    if now - _CLOSED_RESOLUTION_REFRESH_LAST_MONO < float(iv):
        return

    # Avoid overlapping UI ``GET /portfolio`` reconcile (same lock): parallel closed-resolution
    # ``get_market`` bursts were tying up Kalshi + SQLite alongside dashboard polls.
    try:
        ui_busy = kalshi_ui_reconcile_lock().locked()
    except AttributeError:
        ui_busy = False
    if ui_busy:
        logger.debug("Closed resolution auto-refresh deferred (UI portfolio reconcile lock held)")
        return

    batch = int(getattr(settings, "closed_resolution_refresh_batch", 25) or 25)

    db = db_factory()
    try:
        from src.reconcile.kalshi_positions import refresh_closed_positions_resolution_from_kalshi

        out = await refresh_closed_positions_resolution_from_kalshi(
            db,
            trade_mode=TRADE_MODE,
            kalshi_client=kalshi_client,
            limit=batch,
            only_missing_result=True,
        )
        try:
            from src.shadow_buys import resolve_shadow_markets

            scored = await resolve_shadow_markets(db, kalshi_client, limit=10)
            if scored:
                logger.info("Shadow buys scored: %s", scored)
        except Exception as shadow_exc:
            logger.debug("Shadow resolution skipped: %s", shadow_exc)
        if int(out.get("updated", 0) or 0) > 0:
            logger.info(
                "Closed resolution auto-refresh: examined=%s updated=%s fetch_failed=%s",
                out.get("examined"),
                out.get("updated"),
                out.get("market_fetch_failed"),
            )
            updates = list(out.get("updates") or [])
            if updates:
                await broadcast_fn({"type": "closed_positions_resolution", "data": {"updates": updates}})
    except Exception as e:
        logger.warning("Closed resolution auto-refresh skipped: %s", e)
    finally:
        db.close()
        _CLOSED_RESOLUTION_REFRESH_LAST_MONO = time.monotonic()


# ── Market pre-filter ──────────────────────────────────────────────────────────

_CONTAINER_PATTERNS = ("KXMVE", "CROSSCATEGORY", "MULTIGAME")

def _max_leg_volume_scan_unit(members: List[dict]) -> float:
    """Priority score for a scan unit: max ``volume`` across member markets."""
    best = 0.0
    for m in members:
        try:
            best = max(best, float(m.get("volume") or 0.0))
        except (TypeError, ValueError):
            continue
    return best


def _cap_scan_queue_units_by_volume(
    scan_queue: List[Tuple[str, Optional[str], List[dict]]],
    cap: int,
) -> List[Tuple[str, Optional[str], List[dict]]]:
    """When ``cap`` > 0 and queue is longer, keep the top ``cap`` units by :func:`_max_leg_volume_scan_unit`."""
    if cap <= 0 or len(scan_queue) <= cap:
        return scan_queue
    ranked = sorted(scan_queue, key=lambda u: _max_leg_volume_scan_unit(u[2]), reverse=True)
    return ranked[:cap]


def _tradeable_scan_queue(tradeable: List[dict]) -> List[Tuple[str, Optional[str], List[dict]]]:
    """Queue scan units: single market, or a **partition** of an event (true siblings only).

    Kalshi ``event_ticker`` can hold unrelated props (e.g. different pitchers' K lines). Those are
    split into separate batch groups via :func:`group_markets_by_event_batch_partition`.
    """
    by_et: Dict[str, List[dict]] = {}
    for m in tradeable:
        et = (m.get("event_ticker") or "").strip().upper()
        if not et:
            continue
        by_et.setdefault(et, []).append(m)

    handled_et: Set[str] = set()
    out: List[Tuple[str, Optional[str], List[dict]]] = []
    for m in tradeable:
        et = (m.get("event_ticker") or "").strip().upper()
        if not et:
            out.append(("single", None, [m]))
            continue
        if et in handled_et:
            continue
        handled_et.add(et)
        grp = list(by_et.get(et) or [])
        grp.sort(key=lambda x: normalize_market_id(str(x.get("id") or "")))
        parts = group_markets_by_event_batch_partition(grp)
        for pkey in sorted(parts.keys()):
            sub = list(parts[pkey])
            sub.sort(key=lambda x: normalize_market_id(str(x.get("id") or "")))
            if len(sub) > 1:
                out.append(("batch", et, sub))
            else:
                out.append(("single", et, sub))
    return out


def _promote_fast_close_markets(
    scan_queue: List[Tuple[str, Optional[str], List[dict]]],
) -> List[Tuple[str, Optional[str], List[dict]]]:
    """Markets closing inside an hour are single units at the front of the queue.

    Their order is placed as soon as that analysis returns, ahead of slower batches.
    """
    now = datetime.now(timezone.utc)
    fast: List[Tuple[float, Tuple[str, Optional[str], List[dict]]]] = []
    rest: List[Tuple[str, Optional[str], List[dict]]] = []
    for kind, et, members in scan_queue:
        fast_members = []
        slow_members = []
        for market in members:
            seconds, _reason = _seconds_until_buy_horizon(market, now=now)
            if seconds is not None and 0 < seconds < 3600:
                fast_members.append((seconds, market))
            else:
                slow_members.append(market)
        for seconds, market in fast_members:
            fast.append((seconds, ("single", et, [market])))
        if len(slow_members) > 1 and kind == "batch":
            rest.append(("batch", et, slow_members))
        elif len(slow_members) == 1:
            rest.append(("single", et, slow_members))
        elif len(slow_members) > 1:
            rest.append((kind, et, slow_members))
    fast.sort(key=lambda item: item[0])
    return [unit for _seconds, unit in fast] + rest


def _cooldown_market_ids_from_event_batch_xai_jsons(
    xai_analysis_texts: List[Optional[str]],
) -> Tuple[Set[str], Set[str]]:
    """Parse batch ``xai_analysis`` blobs into cooldown targets.

    Returns ``(explicit_market_ids, legacy_event_tickers)``. New rows list
    ``event_batch_market_ids`` (only those legs are debounced). Older rows only had
    ``event_ticker`` — caller expands that to **all** tradeable ids under the event.
    """
    explicit: Set[str] = set()
    legacy_events: Set[str] = set()
    for xa_text in xai_analysis_texts:
        if not xa_text:
            continue
        try:
            xa = json.loads(xa_text)
        except Exception:
            continue
        if not xa.get("event_batch"):
            continue
        raw_ids = xa.get("event_batch_market_ids")
        if isinstance(raw_ids, list) and len(raw_ids) > 0:
            for lid in raw_ids:
                nk = normalize_market_id(str(lid or "")).strip().upper()
                if nk:
                    explicit.add(nk)
        else:
            et = str(xa.get("event_ticker") or "").strip().upper()
            if et:
                legacy_events.add(et)
    return explicit, legacy_events


def _event_tickers_from_event_batch_xai_json(xai_analysis_texts: List[Optional[str]]) -> Set[str]:
    """Backward-compat: event tickers from batch rows that lack ``event_batch_market_ids``."""
    _e, legacy = _cooldown_market_ids_from_event_batch_xai_jsons(xai_analysis_texts)
    return legacy


def _tradeable_market_ids_for_event_tickers(
    tradeable: List[dict], event_tickers: Set[str]
) -> Set[str]:
    """All normalized market ids in ``tradeable`` whose ``event_ticker`` is in the set."""
    out: Set[str] = set()
    if not event_tickers:
        return out
    for m in tradeable:
        et = (m.get("event_ticker") or "").strip().upper()
        if not et or et not in event_tickers:
            continue
        tid = str(m.get("id") or "")
        if tid:
            out.add(normalize_market_id(tid).upper())
    return out


def _apply_event_series_locks(
    tradeable: List[dict],
    *,
    locked_event_tickers: Set[str],
    allowed_market_id_by_event: Dict[str, str],
) -> List[dict]:
    """Filter tradeable markets so locked event series only allow the chosen market id.

    This prevents contradictory sibling strikes (and therefore contradictory YES/NO exposures) within a single event series.
    """
    if not locked_event_tickers:
        return tradeable
    out: List[dict] = []
    for m in tradeable:
        et = (m.get("event_ticker") or "").strip().upper()
        if not et or et not in locked_event_tickers:
            out.append(m)
            continue
        allow = allowed_market_id_by_event.get(et)
        if not allow:
            continue
        mid = normalize_market_id(str(m.get("id") or "")).upper()
        if mid and mid == allow:
            out.append(m)
    return out


def _event_series_lock_blocks_market(
    market_id: object,
    event_ticker: object,
    *,
    locked_event_tickers: Set[str],
    allowed_market_id_by_event: Dict[str, str],
) -> bool:
    """True if a series lock applies and this market id is not the allowed sibling."""
    et = str(event_ticker or "").strip().upper()
    if not et or et not in locked_event_tickers:
        return False
    allow = str(allowed_market_id_by_event.get(et) or "").strip().upper()
    mid = normalize_market_id(str(market_id or "")).upper()
    return not (bool(allow) and mid == allow)


def _vetting_anchor_datetime(val: object, *, now: datetime) -> Optional[datetime]:
    """Parse occurrence / expected_expiration for stale checks; ignore garbage extremes."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except Exception:
        return None
    if dt.year < 2020 or dt > now + timedelta(days=3650):
        return None
    return dt


def _buy_leg_passes_liquidity(
    bid: float,
    ask: float,
    spread: float,
    ask_size: float,
    max_spread: float,
    min_top_size: float,
) -> bool:
    """Same gates as scan_and_trade uses before BUY_YES / BUY_NO execution."""
    if bid <= 0.0 and ask > 0.0 and ask < 0.95:
        return False
    if bid > 0.0 and ask > 0.0 and spread > max_spread:
        return False
    if ask > 0.0 and ask_size < min_top_size:
        return False
    return True


def _buy_horizon_deltas_seconds(market: dict, *, now: datetime) -> List[float]:
    """Seconds-until each distinct anchor among ``vetting_horizon_time`` and contractual ``close_time``.

    May include non-positive values when an anchor is in the past (caller treats as expired).
    Used only when ``expected_expiration_time`` is missing or already past — see
    ``_seconds_until_buy_horizon``.
    """
    seen: Set[str] = set()
    out: List[float] = []
    for key in ("vetting_horizon_time", "close_time"):
        raw = market.get(key)
        if not raw:
            continue
        iso = str(raw).strip()
        if not iso or iso in seen:
            continue
        seen.add(iso)
        try:
            ct = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            ct = ct.astimezone(timezone.utc)
            sec = (ct - now).total_seconds()
            if math.isfinite(sec):
                out.append(float(sec))
        except Exception:
            continue
    return out


def _seconds_until_buy_horizon(market: dict, *, now: datetime) -> Tuple[Optional[float], str]:
    """Wall time until the market's **event end** for ``BOT_MAX_HOURS`` vetting.

    Prefer Kalshi ``expected_expiration_time`` when present and still in the future so
    contractual ``close_time`` (often days after the game for sports props) does not block
    buys pegged to the actual event. When expected expiration is missing or already past,
    fall back to the earliest positive delta among ``vetting_horizon_time`` and ``close_time``.
    """
    exp_dt = _vetting_anchor_datetime(market.get("expected_expiration_time"), now=now)
    if exp_dt is not None and exp_dt > now:
        return float((exp_dt - now).total_seconds()), ""

    deltas_sec = _buy_horizon_deltas_seconds(market, now=now)
    if not deltas_sec:
        return None, "no_event_horizon"
    positive = [d for d in deltas_sec if d > 0.0]
    if not positive:
        return None, "already_expired"
    return min(positive), ""


def is_tradeable_market(
    market: dict,
    max_hours: int = 4,
    min_volume: float = 1.0,
    max_spread: float = 0.15,
    min_top_size: float = 1.0,
    min_residual_payoff: float = 0.0,
) -> Tuple[bool, str]:
    """Return (pass, reason_if_failed).

    Volume uses Kalshi's volume_24h_fp (24h contract count).

    Time window ``max_hours``: prefer ``expected_expiration_time`` when parseable and still in
    the future (event expected to end within the window). If it is absent or already past, use
    the soonest future instant among ``vetting_horizon_time`` and contractual ``close_time``.

    Liquidity: at least one contract leg (YES or NO) must pass the same spread / depth
    checks used at execution time, so BUY_NO opportunities are not filtered out solely
    because the YES book is thin.

    A side is buyable only when it is liquid and its ask is inside the same window
    the order uses: ask from 50¢ to 85¢, and gross upside ``1 − ask`` at least
    ``min_residual_payoff`` when that floor is on. Markets outside that window
    are not sent to the model.
    """
    ticker = market.get("id", "").upper()
    if any(pat in ticker for pat in _CONTAINER_PATTERNS):
        return False, "mve_container"
    if market.get("market_type") != "binary":
        return False, "not_binary"
    if market.get("status") not in ("open", "active"):
        return False, f"not_open({market.get('status')})"
    if (market.get("volume") or 0) < min_volume:
        return False, f"low_volume({market.get('volume', 0):.1f})"
    yes_price = float(market.get("yes_price") or 0.0)
    if yes_price == 0.0:
        return False, "no_price_data"

    # ── Liquidity / spread: YES leg OR NO leg (matches execution paths) ─────
    yes_bid = float(market.get("yes_bid") or 0.0)
    yes_ask = float(market.get("yes_ask") or 0.0)
    yes_spread = float(market.get("yes_spread") or 1.0)
    yes_ask_size = float(market.get("yes_ask_size") or 0.0)

    no_bid = float(market.get("no_bid") or 0.0)
    no_ask = float(market.get("no_ask") or 0.0)
    no_spread = float(market.get("no_spread") or 1.0)
    no_ask_size = float(market.get("no_ask_size") or 0.0)

    yes_ok = _buy_leg_passes_liquidity(
        yes_bid, yes_ask, yes_spread, yes_ask_size, max_spread, min_top_size,
    )
    no_ok = _buy_leg_passes_liquidity(
        no_bid, no_ask, no_spread, no_ask_size, max_spread, min_top_size,
    )
    if not yes_ok and not no_ok:
        return False, "illiquid_yes_and_no"

    from src.vetting.screen import buyable_leg

    if not buyable_leg(
        market,
        min_upside=float(min_residual_payoff or 0.0),
        max_spread=float(max_spread),
        min_top_size=float(min_top_size),
    ):
        return False, "no_buyable_leg"

    now = datetime.now(timezone.utc)
    sec_until, fail = _seconds_until_buy_horizon(market, now=now)
    if sec_until is None:
        return False, fail
    hours_out = float(sec_until) / 3600.0
    if hours_out > float(max_hours):
        return False, "too_far_out"
    return True, ""


# ── Bot state helper ───────────────────────────────────────────────────────────

def get_bot_state_str(db_factory) -> str:
    from src.database.models import BotState
    db = db_factory()
    try:
        row = db.query(BotState).filter(BotState.id == 1).first()
        return row.state if row else "stop"
    finally:
        db.close()


def is_bot_playing(db_factory) -> bool:
    """True only when dashboard mode is ``play`` (market scan + AI analysis + new trades)."""
    return get_bot_state_str(db_factory) == "play"


# Detail lines for the same market or series are not repeated every scan.
_PREMODEL_SKIP_LOG_SEC = 15 * 60
_premodel_skip_logged_at: Dict[str, float] = {}


def series_ticker_for_market(market: dict) -> str:
    """Series key for ``GET /series/{series_ticker}``.

    The market payload's ``series_ticker`` wins. Otherwise the leading token of
    the event ticker or market id is used, which is how Kalshi names the series.
    """
    explicit = str(market.get("series_ticker") or "").strip()
    if explicit:
        return explicit
    for raw in (market.get("event_ticker"), market.get("id")):
        text = str(raw or "").strip()
        if "-" in text:
            head = text.split("-", 1)[0].strip()
            if head:
                return head
    return ""


def apply_series_fee_schedule(market: dict, schedule: dict) -> None:
    """Stamp a series read onto the market dict the entry gate already reads."""
    if not isinstance(schedule, dict) or not schedule.get("known"):
        market.pop("fee_type", None)
        market.pop("fee_multiplier", None)
        if isinstance(schedule, dict) and schedule.get("reason"):
            market["fee_schedule_reason"] = str(schedule.get("reason"))
        return
    market["fee_type"] = schedule.get("fee_type")
    market["fee_multiplier"] = schedule.get("fee_multiplier")
    market.pop("fee_schedule_reason", None)


async def attach_series_fee_schedules(kalshi_client, markets: List[dict]) -> None:
    """Fetch each series at most once per cache TTL and copy it onto its markets.

    A failed read stays unknown. It does not raise and it does not invent a multiplier.
    """
    groups: Dict[str, List[dict]] = {}
    for market in markets:
        if not isinstance(market, dict):
            continue
        key = series_ticker_for_market(market)
        if key and not str(market.get("series_ticker") or "").strip():
            market["series_ticker"] = key
        groups.setdefault(key, []).append(market)
    for key, group in groups.items():
        if not key or not hasattr(kalshi_client, "get_series_fee"):
            schedule = {"known": False, "reason": "no series ticker"}
        else:
            try:
                loaded = await kalshi_client.get_series_fee(key)
            except Exception:
                logger.exception("Series fee read failed for %s", key)
                loaded = {"known": False, "reason": "series fee read failed"}
            schedule = loaded if isinstance(loaded, dict) else {"known": False, "reason": "series fee read failed"}
        for market in group:
            apply_series_fee_schedule(market, schedule)


async def ensure_market_fee_schedule(kalshi_client, market: dict) -> dict:
    """Reuse a schedule already attached to the market, otherwise the series cache."""
    current = fee_schedule_from_mapping(market)
    if current.get("known"):
        return current
    await attach_series_fee_schedules(kalshi_client, [market])
    return fee_schedule_from_mapping(market)


def _note_premodel_skip(counts: Dict[str, int], block: dict) -> None:
    kind = str(block.get("kind") or "fee_unknown")
    counts[kind] = counts.get(kind, 0) + 1
    if kind == "fee_unknown":
        log_key = f"fee_unknown:{block.get('series') or block.get('ticker')}"
    else:
        log_key = f"cash_cap:{block.get('ticker')}"
    now = time.monotonic()
    if len(_premodel_skip_logged_at) > 4000:
        stale = [key for key, seen in _premodel_skip_logged_at.items() if now - seen >= _PREMODEL_SKIP_LOG_SEC]
        for key in stale:
            _premodel_skip_logged_at.pop(key, None)
    seen = _premodel_skip_logged_at.get(log_key)
    if seen is not None and now - seen < _PREMODEL_SKIP_LOG_SEC:
        return
    _premodel_skip_logged_at[log_key] = now
    logger.info("%s", block.get("message") or kind)


def reentry_cooldown_minutes_from_settings(settings) -> int:
    """Read ``reentry_cooldown_minutes``. A leftover hours attribute is ignored."""
    try:
        return max(0, int(getattr(settings, "reentry_cooldown_minutes", 0) or 0))
    except (TypeError, ValueError):
        return 0


def losing_exit_cooldown_market_ids(rows, *, now: datetime, cooldown_minutes: int) -> Set[str]:
    """Tickers whose latest close inside the window has negative realized P&L.

    SQLite returns ``closed_at`` without a timezone. ``now`` is UTC-aware.
    Both sides are normalized to UTC before they are compared. The instant
    does not change.
    """
    if cooldown_minutes <= 0:
        return set()
    now_utc = ensure_utc(now)
    if now_utc is None:
        return set()
    cutoff = now_utc - timedelta(minutes=int(cooldown_minutes))
    latest: Dict[str, Tuple[datetime, float]] = {}
    for mid, closed_at, realized in rows:
        closed_utc = ensure_utc(closed_at) if isinstance(closed_at, datetime) else None
        if not mid or closed_utc is None or closed_utc < cutoff:
            continue
        key = normalize_market_id(str(mid)).upper()
        prev = latest.get(key)
        if prev is None or closed_utc > prev[0]:
            latest[key] = (closed_utc, float(realized or 0.0))
    return {key for key, (_, pnl) in latest.items() if pnl < 0}


def _position_age_minutes_utc(opened_at: Optional[datetime]) -> float:
    """Minutes since ``opened_at`` (UTC)."""
    if opened_at is None:
        return float("inf")
    op = ensure_utc(opened_at)
    if op is None:
        return float("inf")
    return max(0.0, (utc_now() - op).total_seconds() / 60.0)


# ── Position monitor ───────────────────────────────────────────────────────────

async def monitor_positions(
    kalshi_client,
    db_factory,
    broadcast_fn,
    settings,
    allow_exits: bool = True,
    *,
    bot_state_label: str = "",
):
    """Refresh open positions; optionally exit those hitting an exit condition.

    ``allow_exits`` is False only when ``bot_state`` is ``stop`` (no auto sells); ``bot_state_label``
    is logged when a would-be exit is suppressed so logs match the DB/UI.
    """
    from src.database.models import Position, Trade
    from src.reconcile.open_positions import dedupe_open_positions

    db = db_factory()
    closed_notices: List[dict] = []

    try:
        positions = db.query(Position).filter(
            Position.status == "open",
            Position.trade_mode == TRADE_MODE,
        ).all()
        if not positions:
            logger.debug(
                "monitor_positions: no open rows for trade_mode=%s (skipping Kalshi reconcile)",
                TRADE_MODE,
            )
            return
        prior_marks = {
            pos.id: (float(pos.current_price or 0.0), None if pos.bid_price is None else float(pos.bid_price))
            for pos in positions
        }

        # Live: Kalshi ``GET /portfolio/positions`` → DB (qty, cost, avg, fees) + settlement + flat-row patches.
        live_reconcile_ran_ok = False
        try:
            from src.reconcile.kalshi_live_sync import reconcile_live_positions_from_kalshi

            _n_open, n_imp, n_port, n_hist, n_flat, n_fin, _n_ent, _n_un, n_exfin = (
                await reconcile_live_positions_from_kalshi(
                    db,
                    trade_mode=TRADE_MODE,
                    kalshi_client=kalshi_client,
                    settlements=True,
                    broadcast_fn=broadcast_fn,
                )
            )
            live_reconcile_ran_ok = True
            if n_imp:
                logger.info("Kalshi portfolio import created %d new open position row(s)", n_imp)
            if n_port:
                logger.info("Kalshi settlement sync closed %d position(s)", n_port)
            if n_hist:
                logger.info(
                    "Kalshi settlements API closed %d position(s) (archived/404 markets)",
                    n_hist,
                )
            if n_exfin:
                logger.info(
                    "Kalshi exchange-finalized metadata closed %d open position(s)",
                    n_exfin,
                )
            if n_flat:
                logger.info("Kalshi flat portfolio rows patched %d closed position(s)", n_flat)
            if n_fin:
                logger.info("Kalshi closed-position finalizations (API) applied: %d", n_fin)
            if _n_ent or _n_un:
                logger.info(
                    "Kalshi open live refined from GET orders / marks: entry_updates=%d unrealized_updates=%d",
                    _n_ent,
                    _n_un,
                )
        except Exception as e:
            logger.warning("Live positions sync skipped: %s", e)

        dedupe_open_positions(db, TRADE_MODE)
        positions = db.query(Position).filter(
            Position.status == "open",
            Position.trade_mode == TRADE_MODE,
        ).all()
        if not positions:
            return

        for pos in positions:
            prev_last, prev_bid = prior_marks.get(pos.id, (float(pos.current_price or 0.0), pos.bid_price))
            prev_mark_cents = int(round(float(prev_last or 0.0) * 100))
            prev_bid_cents = None if prev_bid is None else int(round(float(prev_bid) * 100))
            # ``reconcile_live_positions_from_kalshi`` already called ``get_market`` per open row via
            # ``recompute_open_live_position_unrealized_pnl``. Skip duplicate fetches when safe so each scan
            # does not pay 2× Kalshi market latency (still fetch when we need the live book).
            market: Optional[Dict[str, Any]] = None
            mark_last = float(pos.current_price or 0.0)

            trust_reconcile_marks = live_reconcile_ran_ok
            need_market_fetch = not trust_reconcile_marks
            if trust_reconcile_marks:
                if bool(getattr(pos, "awaiting_settlement", False)) or bool(
                    getattr(pos, "dead_market", False)
                ):
                    need_market_fetch = True

            if need_market_fetch:
                mid_lookup = normalize_market_id(pos.market_id)
                market = await kalshi_client.get_market(mid_lookup)
                if not market:
                    raw_mid = (pos.market_id or "").strip()
                    if raw_mid and raw_mid != mid_lookup:
                        market = await kalshi_client.get_market(raw_mid)

                if market:
                    mp = open_position_mark_dollars(market, pos.side)
                    if mp <= 0:
                        t_ob = (
                            (market.get("ticker") or market.get("id") or mid_lookup or "")
                            .strip()
                            or mid_lookup
                        )
                        ob = await kalshi_client.get_market_orderbook_fp(t_ob)
                        mp = open_position_mark_dollars(market, pos.side, ob)
                    mark_last = float(mp)
                    est_opt = open_position_estimated_mark_dollars(market, pos.side)
                    pos.estimated_price = float(est_opt if est_opt is not None else mark_last)
                    kst = str(market.get("kalshi_api_status") or "").strip().lower()
                    rr = str(market.get("resolution_result") or "").strip().lower()
                    pos.kalshi_market_status = kst if kst else None
                    pos.kalshi_market_result = rr if rr in ("yes", "no") else None

                    # Refresh display title to match Kalshi UI (event title often contains city).
                    try:
                        et = (market.get("event_ticker") or "").strip()
                        if et:
                            ev_title = (await kalshi_client.get_event_title(et)) or ""
                            if ev_title:
                                tail = ((market.get("subtitle") or "") or (market.get("title") or "")).strip()
                                new_title = f"{ev_title} — {tail}" if tail else ev_title
                                if (new_title or "").strip() and (pos.market_title or "").strip() != new_title:
                                    pos.market_title = new_title
                    except Exception:
                        pass
                    try:
                        sync_position_expiry_from_market(pos, market)
                    except Exception:
                        pass
                else:
                    mark_last = float(pos.current_price or 0.0)
                    logger.debug(
                        "No market book for %s — using stored mark %.4f (expiration/settlement path)",
                        pos.market_id,
                        mark_last,
                    )
            marks_from_reconcile_only = trust_reconcile_marks and not need_market_fetch
            book_ok_for_exits = market is not None or marks_from_reconcile_only

            current_price = float(mark_last)
            current_value = mark_last * pos.quantity
            cash_basis = open_position_cash_basis_dollars(pos)

            intrinsic_resolved_mark = resolution_intrinsic_mark_dollars(pos)
            from src.reconcile.open_positions import market_trading_closed

            trading_closed = market_trading_closed(pos)
            if market is not None:
                pos.bid_price = float(mark_last)
            elif pos.bid_price is None:
                pos.bid_price = float(mark_last)
            pos.current_price = float(mark_last)
            pos.unrealized_pnl = current_value - cash_basis
            new_mark_cents = int(round(float(mark_last) * 100))
            new_bid_cents = int(round(float(pos.bid_price or 0.0) * 100))
            if new_mark_cents != prev_mark_cents or new_bid_cents != prev_bid_cents:
                record_activity(
                    {
                        "event_type": "mark",
                        "market_id": pos.market_id,
                        "event_ticker": getattr(pos, "event_ticker", None),
                        "side": pos.side,
                        "quantity": int(pos.quantity or 0),
                        "fill_price": float(pos.bid_price or 0.0),
                        "detail": f"bid {new_bid_cents}c last {new_mark_cents}c",
                    }
                )

            # Align flag clears with **strict liquidation mark** (``mark_last`` / ``open_position_mark_*``)
            # or an official resolved intrinsic (winning YES legs often have **no bids** while ``finalized``).
            # ``live_best_bid_dollars`` may still read ``yes_price`` composite and wrongly clear dead-market
            # state while the dashboard bid column shows 0¢.
            if trading_closed:
                pos.awaiting_settlement = True
                continue
            if mark_last > 0:
                from src.bot.exit_backoff import clear_exit_backoff

                clear_exit_backoff(str(pos.market_id or ""))
            if mark_last > 0 or intrinsic_resolved_mark is not None:
                had_flag = bool(getattr(pos, "awaiting_settlement", False)) or bool(
                    getattr(pos, "dead_market", False)
                )
                if bool(getattr(pos, "awaiting_settlement", False)):
                    pos.awaiting_settlement = False
                if bool(getattr(pos, "dead_market", False)):
                    pos.dead_market = False
                if had_flag:
                    logger.info(
                        "Cleared settlement/dead-market flags %s — mark=%.4f intrinsic=%s",
                        pos.market_id,
                        mark_last,
                        "set" if intrinsic_resolved_mark is not None else "none",
                    )

        db.commit()

    except Exception as e:
        closed_notices.clear()
        logger.error("Error in monitor_positions: %s", scrub_text(e))
        record_activity(
            {
                "event_type": "error",
                "detail": "monitor_positions failed",
                "error": type(e).__name__,
            }
        )
        db.rollback()
    finally:
        db.close()

    for notice in closed_notices:
        try:
            await broadcast_fn({"type": "position_closed", "data": notice})
        except Exception as exc:
            logger.error(
                "position_closed broadcast failed for %s: %s",
                notice.get("market_id"),
                type(exc).__name__,
            )


# ── Scan and trade ─────────────────────────────────────────────────────────────

async def scan_and_trade(
    kalshi_client, decision_engine, db_factory, broadcast_fn, settings
):
    """Fetch markets, apply local filter, escalate to the configured AI provider, execute qualifying trades."""
    from src.ai_provider import ai_provider_log_label
    from src.api.portfolio import get_xai_prepaid_balance_usd_cached
    from src.bot.scan_eligibility import refresh_order_search_scan_ui
    from src.database.models import DecisionLog, EventSeriesLock, Position, Trade
    from src.reconcile.open_positions import get_open_position

    db = db_factory()
    ai_log = ai_provider_log_label()
    from src.clients.xai_client import begin_live_lookup_scan
    from src.event_live_lookup import reset_lookup_sweep

    begin_live_lookup_scan()
    reset_lookup_sweep()
    try:
        from src.database.models import get_vault_balance

        positions_open = db.query(Position).filter(
            Position.status == "open",
            Position.trade_mode == TRADE_MODE,
        ).all()
        num_open = len(positions_open)

        from src.clients.kalshi_client import resting_buy_collateral_estimate_usd

        portfolio = await kalshi_client.get_portfolio()
        api_cash = float(portfolio.get("cash", 0.0))
        resting_orders = await kalshi_client.list_orders(status="resting")
        resting_buy_reserve = resting_buy_collateral_estimate_usd(resting_orders)
        uninvested_cash = max(0.0, api_cash - resting_buy_reserve)
        logger.info(
            "Live cash: api=%.2f resting_orders=%d resting_buy_collateral~=%.2f risk_balance_after_reserve=%.2f",
            api_cash,
            len(resting_orders),
            resting_buy_reserve,
            uninvested_cash,
        )

        vault_balance = min(max(0.0, get_vault_balance(db, trade_mode=TRADE_MODE)), float(uninvested_cash))
        balance = max(0.0, float(uninvested_cash) - float(vault_balance))

        total_portfolio_value = float(portfolio.get("portfolio_value", api_cash))

        xai_prepaid = await get_xai_prepaid_balance_usd_cached()
        active_scan, scan_label = refresh_order_search_scan_ui(
            db,
            settings,
            balance,
            total_portfolio_value_usd=total_portfolio_value,
            xai_prepaid_balance_usd=xai_prepaid,
            open_position_count=num_open,
        )
        if not active_scan:
            logger.info(
                "Order search off (%s) balance=%.2f total_value=%.2f open_positions=%d",
                scan_label,
                balance,
                total_portfolio_value,
                num_open,
            )
            return

        # Use Kalshi-side close-time filtering to reduce wasted pagination.
        # Kalshi expects Unix timestamps in seconds.
        now_ts = int(datetime.now(timezone.utc).timestamp())
        fetch_hours = int(getattr(settings, "bot_markets_fetch_max_close_hours", settings.bot_max_hours))
        max_close_ts = now_ts + fetch_hours * 3600
        markets = await kalshi_client.get_markets({"status": "open", "mve_filter": "exclude", "max_close_ts": max_close_ts})
        quote_captured_at = utc_now()
        for _m in markets:
            if isinstance(_m, dict):
                _m["_quote_captured_at"] = quote_captured_at
        tradeable: List[dict] = []
        fail_counts: Dict[str, int] = {}
        for m in markets:
            scan_min_vol = effective_scan_min_volume(
                float(settings.bot_min_volume),
                str(m.get("title") or m.get("market_title") or ""),
                str(m.get("event_ticker") or ""),
            )
            ok, reason = is_tradeable_market(
                m,
                settings.bot_max_hours,
                scan_min_vol,
                settings.bot_max_spread,
                settings.bot_min_top_size,
                settings.local_min_residual_payoff,
            )
            if ok:
                tradeable.append(m)
            else:
                key = reason.split("(")[0]
                fail_counts[key] = fail_counts.get(key, 0) + 1

        # ── Persistent event-series lock ─────────────────────────────────────
        # If we've ever executed a trade in an event series, never enter any other sibling strike again.
        # (Prevents contradictory YES/NO exposures across siblings; unlike AI debounce this is "indefinite".)
        locked_event_tickers: Set[str] = set()
        allowed_market_id_by_event: Dict[str, str] = {}
        for et, mid in (
            db.query(EventSeriesLock.event_ticker, EventSeriesLock.chosen_market_id)
            .filter(EventSeriesLock.trade_mode == TRADE_MODE)
            .all()
        ):
            etu = str(et or "").strip().upper()
            midu = normalize_market_id(str(mid or "")).upper()
            if etu and midu:
                locked_event_tickers.add(etu)
                allowed_market_id_by_event[etu] = midu

        if locked_event_tickers:
            before = len(tradeable)
            tradeable = _apply_event_series_locks(
                tradeable,
                locked_event_tickers=locked_event_tickers,
                allowed_market_id_by_event=allowed_market_id_by_event,
            )
            after = len(tradeable)
            if before != after:
                logger.info(
                    "Event series locks: tradeable filtered %d → %d (locked_events=%d)",
                    before,
                    after,
                    len(locked_event_tickers),
                )

        logger.info(
            "Scan: %d markets total | %d pass basic filter | failures: %s",
            len(markets), len(tradeable),
            " ".join(f"{k}={v}" for k, v in sorted(fail_counts.items())),
        )

        held_tickers = {
            (normalize_market_id(p.market_id) or "").strip().upper()
            for p in positions_open
            if (normalize_market_id(p.market_id) or "").strip()
        }
        from src.bot.match_exposure import match_exposure_key

        held_match_keys = {
            match_exposure_key(p.market_id)
            for p in positions_open
            if (p.market_id or "").strip()
        }
        try:
            from src.activity_log import list_unresolved_intents

            for intent in list_unresolved_intents(action="buy") or []:
                held_match_keys.add(match_exposure_key(str(intent.get("market_id") or "")))
        except Exception:
            logger.debug("Unconfirmed buys were not added to the match cap", exc_info=True)

        # Tiered re-analysis cooldown:
        # - Markets that escalated to the AI provider: long debounce (API cost + stale signal window).
        # - Local-only outcomes (no LLM call): short debounce so one full sweep doesn't freeze
        #   the entire universe for 15 minutes — that made capacity look broken.
        now = utc_now()
        xai_cutoff = now - timedelta(minutes=15)
        local_debounce_sec = max(90, int(getattr(settings, "bot_scan_interval", 30)) * 2)
        local_cutoff = now - timedelta(seconds=local_debounce_sec)

        recent_xai_rows = (
            db.query(DecisionLog.market_id, DecisionLog.xai_analysis)
            .filter(
                DecisionLog.timestamp >= xai_cutoff,
                DecisionLog.escalated_to_xai.is_(True),
            )
            .all()
        )
        recent_xai = {
            normalize_market_id(str(mid or "")).upper() for mid, _xa in recent_xai_rows
        }
        # Event batch logs only the *chosen* market_id, but the model compared the full sibling set.
        # Without sibling cooldown, the next sweep re-queues the same event and can buy more strikes.
        explicit_batch_legs, legacy_batch_events = _cooldown_market_ids_from_event_batch_xai_jsons(
            [xa for _mid, xa in recent_xai_rows]
        )
        xai_batch_sibling_cooldown = explicit_batch_legs | _tradeable_market_ids_for_event_tickers(
            tradeable, legacy_batch_events
        )

        recent_local_only = {
            normalize_market_id(str(row.market_id or "")).upper()
            for row in db.query(DecisionLog)
            .filter(
                DecisionLog.timestamp >= local_cutoff,
                DecisionLog.escalated_to_xai.is_(False),
            )
            .with_entities(DecisionLog.market_id)
            .all()
        }
        recently_analyzed = recent_xai | recent_local_only | xai_batch_sibling_cooldown
        from src.bot.reanalysis import should_skip_unchanged_quote

        previous_quotes: Dict[str, Tuple[float, float]] = {}
        for mid, yes_px, no_px in (
            db.query(DecisionLog.market_id, DecisionLog.snapshot_yes_price, DecisionLog.snapshot_no_price)
            .filter(DecisionLog.snapshot_yes_price.isnot(None))
            .order_by(DecisionLog.timestamp.desc())
            .limit(2000)
            .all()
        ):
            key = normalize_market_id(str(mid or "")).upper()
            if key and yes_px is not None and no_px is not None and key not in previous_quotes:
                previous_quotes[key] = (float(yes_px), float(no_px))

        logger.debug(
            "Scan cooldown: ai_escalation_window=%d local_only_window=%d batch_event_sibling=%d (local_debounce=%ds)",
            len(recent_xai),
            len(recent_local_only),
            len(xai_batch_sibling_cooldown),
            local_debounce_sec,
        )

        reentry_cd_min = reentry_cooldown_minutes_from_settings(settings)
        loss_exit_cooldown_tickers: Set[str] = set()
        if reentry_cd_min > 0:
            cd_cutoff = utc_now() - timedelta(minutes=reentry_cd_min)
            # Per ticker: only the **latest** close in the window matters — a later winning exit
            # clears cooldown from an earlier loss on the same market.
            recent_closes = (
                db.query(Position.market_id, Position.closed_at, Position.realized_pnl)
                .filter(
                    Position.status == "closed",
                    Position.trade_mode == TRADE_MODE,
                    Position.closed_at.isnot(None),
                    Position.closed_at >= cd_cutoff,
                )
                .all()
            )
            loss_exit_cooldown_tickers = losing_exit_cooldown_market_ids(
                recent_closes,
                now=utc_now(),
                cooldown_minutes=reentry_cd_min,
            )

        batch_completed_this_sweep: Set[str] = set()
        scan_queue = _promote_fast_close_markets(_tradeable_scan_queue(tradeable))
        full_sq = len(scan_queue)
        cap_u = int(getattr(settings, "bot_max_scan_queue_units_per_sweep", 0) or 0)
        if cap_u > 0:
            scan_queue = _cap_scan_queue_units_by_volume(scan_queue, cap_u)
            if len(scan_queue) < full_sq:
                logger.info(
                    "Scan queue volume-priority cap: %d -> %d unit(s) (bot_max_scan_queue_units_per_sweep=%d)",
                    full_sq,
                    len(scan_queue),
                    cap_u,
                )
        logger.info(
            "Scan sweep: %d queue unit(s) from %d tradeable market(s) (mode=%s)",
            len(scan_queue),
            len(tradeable),
            TRADE_MODE,
        )
        queued_markets = [market for _kind, _event, members in scan_queue for market in members]
        try:
            await attach_series_fee_schedules(kalshi_client, queued_markets)
        except Exception:
            logger.exception("Series fee prefetch failed; markets with no schedule stay closed")
        premodel_skip_counts: Dict[str, int] = {"fee_unknown": 0, "cash_cap": 0}

        # Event titles are fetched lazily per batch/single unit (``get_event_title`` caches ~TTL).
        # Do **not** prefetch all unique events here — that is O(events)×HTTP and can stall the loop for minutes.

        _sweep_started = time.monotonic()
        _last_prog_log = _sweep_started
        for i_u, (unit_kind, et_key, members) in enumerate(scan_queue):
            from src.daily_budget import budget_status

            if budget_status(db)["reached"]:
                logger.info("Daily AI budget reached. No more model calls until tomorrow.")
                break
            now_mono = time.monotonic()
            if now_mono - _last_prog_log >= 60.0:
                logger.info(
                    "Scan sweep still running: unit %d/%d (~%.0fs elapsed)",
                    i_u + 1,
                    len(scan_queue),
                    now_mono - _sweep_started,
                )
                _last_prog_log = now_mono

            # Stop scanning when play/total balance/deployable/prepaid gates no longer allow new entries.
            xai_prepaid_now = await get_xai_prepaid_balance_usd_cached()
            active_now, scan_label_now = refresh_order_search_scan_ui(
                db,
                settings,
                balance,
                total_portfolio_value_usd=total_portfolio_value,
                xai_prepaid_balance_usd=xai_prepaid_now,
                open_position_count=num_open,
            )
            if not active_now:
                logger.info(
                    "Order search off mid-sweep (%s) balance=%.2f total_value=%.2f open_positions=%d",
                    scan_label_now,
                    balance,
                    total_portfolio_value,
                    num_open,
                )
                break

            try:
                from src.api.tuning import sync_runtime_from_db

                sync_runtime_from_db(db)
            except Exception as exc:
                logger.error("Settings sync before unit failed (%s)", type(exc).__name__)

            if not is_bot_playing(db_factory):
                logger.info(
                    "Scan aborted — bot left play mode (pause/stop); skipping remaining markets "
                    "(no further %s escalation this sweep)",
                    ai_log,
                )
                break

            if unit_kind == "batch" and et_key:
                fresh_members: List[dict] = []
                for m in members:
                    tid = m.get("id", "")
                    nt = normalize_market_id(str(tid)).upper()
                    from src.bot.buy_retry import miss_is_watched

                    prev_quote = previous_quotes.get(nt)
                    if (
                        nt in held_tickers
                        or tid in batch_completed_this_sweep
                        or match_exposure_key(str(tid)) in held_match_keys
                        or (
                            not miss_is_watched(str(tid))
                            and should_skip_unchanged_quote(
                                analyzed_recently=nt in recently_analyzed,
                                previous_yes=None if prev_quote is None else prev_quote[0],
                                previous_no=None if prev_quote is None else prev_quote[1],
                                yes_price=float(m.get("yes_price") or 0),
                                no_price=float(m.get("no_price") or 0),
                            )
                        )
                    ):
                        continue
                    if reentry_cd_min > 0 and nt in loss_exit_cooldown_tickers:
                        continue
                    et_m = str(m.get("event_ticker") or "").strip().upper()
                    if _event_series_lock_blocks_market(
                        tid,
                        et_m,
                        locked_event_tickers=locked_event_tickers,
                        allowed_market_id_by_event=allowed_market_id_by_event,
                    ):
                        continue
                    fresh_members.append(m)
                if len(fresh_members) < 2:
                    for xm in members:
                        batch_completed_this_sweep.add(str(xm.get("id") or ""))
                    if len(fresh_members) == 1:
                        members = fresh_members
                        unit_kind = "single"
                        et_key = None
                    else:
                        continue
                else:
                    members = fresh_members

                if unit_kind == "batch" and et_key:
                    before_l = len(members)
                    members, n_trim, dropped_raw = shortlist_line_ladder_members_for_xai(
                        members, et_key, LINE_LADDER_MAX_LEGS_FOR_XAI
                    )
                    for dr in dropped_raw:
                        if dr:
                            batch_completed_this_sweep.add(dr)
                    if n_trim > 0:
                        logger.info(
                            "Ladder %s shortlist: %d -> %d leg(s) for event %s "
                            "(top %d by local volume/depth/spread rank)",
                            ai_log,
                            before_l,
                            len(members),
                            et_key,
                            LINE_LADDER_MAX_LEGS_FOR_XAI,
                        )
                    if len(members) < 2:
                        for xm in members:
                            batch_completed_this_sweep.add(str(xm.get("id") or ""))
                        if len(members) == 1:
                            unit_kind = "single"
                            et_key = None
                        else:
                            continue

            if unit_kind == "batch" and et_key:
                still_open = []
                for m in list(members):
                    mid = str(m.get("id") or "")
                    if untradeable_reason(mid):
                        logger.info(
                            "Skip leg %s — recent routing rejection; sibling legs stay eligible",
                            mid,
                        )
                        batch_completed_this_sweep.add(mid)
                        continue
                    still_open.append(m)
                if len(still_open) < 2:
                    if len(still_open) == 1:
                        members = still_open
                        unit_kind = "single"
                        et_key = None
                    else:
                        continue
                else:
                    members = still_open

            market: dict
            decision: Dict[str, Any]

            if unit_kind == "batch" and et_key:
                entry_blocks = [premodel_entry_block(m, balance) for m in members]
                if all(block is not None for block in entry_blocks):
                    for block in entry_blocks:
                        _note_premodel_skip(premodel_skip_counts, block)
                    for xm in members:
                        batch_completed_this_sweep.add(str(xm.get("id") or ""))
                    continue
                ev_display = ""
                try:
                    ev_display = (await kalshi_client.get_event_title(et_key)) or ""
                except Exception:
                    pass
                legs: List[Dict[str, Any]] = []
                for m in members:
                    tid = str(m.get("id") or "")
                    ypr = m.get("yes_price", 0.5)
                    npr = m.get("no_price", 0.5)
                    yaf = float(m.get("yes_ask") or 0.0)
                    naf = float(m.get("no_ask") or 0.0)
                    ypx = float(yaf) if yaf > 0 else float(ypr)
                    npx = float(naf) if naf > 0 else float(npr)
                    ctit = m.get("title", tid)
                    sub = m.get("subtitle") or ""
                    m_title = ctit
                    if ev_display:
                        tail = (sub or ctit).strip()
                        m_title = f"{ev_display} — {tail}" if tail else ev_display
                    legs.append(
                        {
                            "market_id": tid,
                            "market_title": m_title,
                            "market_description": enrich_ai_market_description(
                                (sub or ctit).strip(), m
                            ),
                            "event_ticker": (m.get("event_ticker") or "").strip(),
                            "current_prices": {
                                "yes": ypx,
                                "no": npx,
                                "yes_bid": m.get("yes_bid"),
                                "yes_ask": m.get("yes_ask"),
                                "no_bid": m.get("no_bid"),
                                "no_ask": m.get("no_ask"),
                                "yes_ask_size": m.get("yes_ask_size"),
                                "no_ask_size": m.get("no_ask_size"),
                                "local_vetting_notes": "Passed local vetting (liq/spread/volume/time).",
                            },
                            "yes_spread": m.get("yes_spread"),
                            "no_spread": m.get("no_spread"),
                            "volume": float(m.get("volume") or 0.0),
                            "expires_in_days": m.get("expires_in_days") or 1,
                            "close_time": m.get("close_time"),
                            "expected_expiration_time": m.get("expected_expiration_time"),
                            "vetting_horizon_time": m.get("vetting_horizon_time"),
                        }
                    )
                from src.event_live_lookup import context_for_market
                from src.live_facts import live_fact_for_market, note_live_fact_skip, series_of

                usable = False
                skip_reason = ""
                for member, leg in zip(members, legs):
                    fact = await live_fact_for_market(member)
                    seconds_left, _src = _seconds_until_buy_horizon(member, now=utc_now())
                    ctx, reason = await context_for_market(
                        market=member,
                        fact=fact,
                        client=getattr(decision_engine, "xai", None),
                        seconds_until=seconds_left,
                        title=str(leg.get("market_title") or ""),
                    )
                    if ctx:
                        leg["market_description"] = (
                            str(leg.get("market_description") or "") + "\n\n" + ctx
                        ).strip()
                        usable = True
                    elif not reason:
                        usable = True
                    else:
                        skip_reason = reason
                if not usable:
                    note_live_fact_skip(series_of(members[0]), skip_reason or "no live facts")
                    for xm in members:
                        batch_completed_this_sweep.add(str(xm.get("id") or ""))
                    continue
                try:
                    decision = await decision_engine.analyze_event_batch(
                        event_ticker=et_key,
                        event_title=ev_display or et_key,
                        legs=legs,
                        min_24h_volume_contracts=float(settings.bot_min_volume),
                        min_top_ask_contracts=float(settings.bot_min_top_size),
                        max_spread=float(settings.bot_max_spread),
                        deployable_balance=float(balance),
                    )
                except Exception as e:
                    logger.error("Decision engine batch error for event %s: %s", et_key, e)
                    for xm in members:
                        batch_completed_this_sweep.add(str(xm.get("id") or ""))
                    continue

                mid_chosen = str(decision.get("market_id") or "").strip()
                market = None
                for m in members:
                    if normalize_market_id(str(m.get("id") or "")).upper() == normalize_market_id(
                        mid_chosen
                    ).upper():
                        market = m
                        break
                if market is None:
                    market = members[0]
                for xm in members:
                    batch_completed_this_sweep.add(str(xm.get("id") or ""))
            else:
                market = members[0]
                ticker = market.get("id", "")
                nt0 = normalize_market_id(str(ticker)).upper()
                from src.bot.buy_retry import miss_is_watched

                prev_quote = previous_quotes.get(nt0)
                if (
                    nt0 in held_tickers
                    or ticker in batch_completed_this_sweep
                    or match_exposure_key(str(ticker)) in held_match_keys
                    or (
                        not miss_is_watched(str(ticker))
                        and should_skip_unchanged_quote(
                            analyzed_recently=nt0 in recently_analyzed,
                            previous_yes=None if prev_quote is None else prev_quote[0],
                            previous_no=None if prev_quote is None else prev_quote[1],
                            yes_price=float(market.get("yes_price") or 0),
                            no_price=float(market.get("no_price") or 0),
                        )
                    )
                ):
                    continue
                if reentry_cd_min > 0 and nt0 in loss_exit_cooldown_tickers:
                    logger.info(
                        "Skip %s — reentry cooldown (losing exit within %d min on this ticker)",
                        ticker,
                        reentry_cd_min,
                    )
                    continue
                et_single = str(market.get("event_ticker") or "").strip().upper()
                if _event_series_lock_blocks_market(
                    ticker,
                    et_single,
                    locked_event_tickers=locked_event_tickers,
                    allowed_market_id_by_event=allowed_market_id_by_event,
                ):
                    logger.info(
                        "Skip %s — event series locked to %s",
                        ticker,
                        allowed_market_id_by_event.get(et_single),
                    )
                    continue
                entry_block = premodel_entry_block(market, balance)
                if entry_block is not None:
                    _note_premodel_skip(premodel_skip_counts, entry_block)
                    continue
                route_hold = untradeable_reason(str(ticker))
                if route_hold:
                    logger.info("Skip model %s — recent routing rejection (%s)", ticker, route_hold)
                    continue

                try:
                    yes_price_raw = market.get("yes_price", 0.5)
                    no_price_raw = market.get("no_price", 0.5)
                    _yes_ask_f = float(market.get("yes_ask") or 0.0)
                    _no_ask_f = float(market.get("no_ask") or 0.0)
                    yes_price = float(_yes_ask_f) if _yes_ask_f > 0 else float(yes_price_raw)
                    no_price = float(_no_ask_f) if _no_ask_f > 0 else float(no_price_raw)
                    volume = market.get("volume", 0.0)
                    expires_days = market.get("expires_in_days") or 1
                    contract_title = market.get("title", ticker)
                    subtitle = market.get("subtitle") or ""
                    title = contract_title
                    try:
                        et_one = (market.get("event_ticker") or "").strip()
                        if et_one:
                            ev_title = (await kalshi_client.get_event_title(et_one)) or ""
                            if ev_title:
                                tail = (subtitle or contract_title).strip()
                                title = f"{ev_title} — {tail}" if tail else ev_title
                    except Exception:
                        pass
                    description = enrich_ai_market_description(
                        (subtitle or contract_title).strip(), market
                    )
                    from src.event_live_lookup import context_for_market
                    from src.live_facts import live_fact_for_market, note_live_fact_skip, series_of

                    fact = await live_fact_for_market(market)
                    seconds_left, _src = _seconds_until_buy_horizon(market, now=utc_now())
                    ctx, reason = await context_for_market(
                        market=market,
                        fact=fact,
                        client=getattr(decision_engine, "xai", None),
                        seconds_until=seconds_left,
                        title=title,
                    )
                    if reason and not ctx:
                        note_live_fact_skip(series_of(market), reason)
                        continue
                    if ctx:
                        description = (description + "\n\n" + ctx).strip()
                    yes_bid = market.get("yes_bid")
                    yes_ask = market.get("yes_ask")
                    no_bid = market.get("no_bid")
                    no_ask = market.get("no_ask")
                    yes_ask_size = market.get("yes_ask_size")
                    no_ask_size = market.get("no_ask_size")

                    decision = await decision_engine.analyze_market(
                        market_id=ticker,
                        market_title=title,
                        market_description=description,
                        current_prices={
                            "yes": yes_price,
                            "no": no_price,
                            "yes_bid": yes_bid,
                            "yes_ask": yes_ask,
                            "no_bid": no_bid,
                            "no_ask": no_ask,
                            "yes_ask_size": yes_ask_size,
                            "no_ask_size": no_ask_size,
                            "local_vetting_notes": f"Passed local vetting (liq/spread/volume/time).",
                        },
                        volume=volume,
                        expires_in_days=float(expires_days or 1.0),
                        close_time=market.get("close_time"),
                        expected_expiration_time=market.get("expected_expiration_time"),
                        vetting_horizon_time=market.get("vetting_horizon_time"),
                        market_timing=market,
                        deployable_balance=float(balance),
                    )
                except Exception as e:
                    logger.error("Decision engine error for %s: %s", ticker, e)
                    continue
                batch_completed_this_sweep.add(str(ticker))

            ticker = str(market.get("id") or "")
            ticker_clean = normalize_market_id(ticker)
            yes_price_raw = market.get("yes_price", 0.5)
            no_price_raw = market.get("no_price", 0.5)
            _yes_ask_f = float(market.get("yes_ask") or 0.0)
            _no_ask_f = float(market.get("no_ask") or 0.0)
            yes_price = float(_yes_ask_f) if _yes_ask_f > 0 else float(yes_price_raw)
            no_price = float(_no_ask_f) if _no_ask_f > 0 else float(no_price_raw)
            volume = market.get("volume", 0.0)
            expires_days = market.get("expires_in_days") or 1
            contract_title = market.get("title", ticker)
            subtitle = market.get("subtitle") or ""
            title = contract_title
            try:
                et = (market.get("event_ticker") or "").strip()
                if et:
                    ev_title = (await kalshi_client.get_event_title(et)) or ""
                    if ev_title:
                        tail = (subtitle or contract_title).strip()
                        title = f"{ev_title} — {tail}" if tail else ev_title
            except Exception:
                pass
            description = (subtitle or contract_title).strip()
            contractual_close = market.get("close_time")
            expected_expiration_iso = pick_display_expected_expiration_iso(market)
            _pos_et = (market.get("event_ticker") or "").strip()
            _pos_et_val = _pos_et if _pos_et else None

            decision_log_row_id = str(uuid.uuid4())
            signal = decision.get("decision", "SKIP")
            action_taken: Dict[str, Any]
            executed_trade = False
            stop_scan_after = False
            sizing_tag = ""
            trade_side = ""
            trade_quantity = 0
            trade_price = 0.0
            trade_cost = 0.0
            order_id_out = ""

            if signal == "SKIP":
                action_taken = {
                    "status": "skipped",
                    "summary": decision.get("action_summary") or "Skipped.",
                }

            else:
                trade_side = "YES" if signal == "BUY_YES" else "NO"

                max_spread = float(getattr(settings, "bot_max_spread", 0.15))
                min_top = float(getattr(settings, "bot_min_top_size", 1.0))

                fail_reason = None
                if not is_bot_playing(db_factory):
                    logger.info(
                        "Skip execution %s — bot not in play after analysis (signal=%s)",
                        ticker,
                        signal,
                    )
                    fail_reason = "Skipped — bot paused/stopped before execution"
                if fail_reason is None:
                    fail_reason = buy_side_liquidity_skip_summary(
                        market,
                        trade_side,
                        max_spread=max_spread,
                        min_top_size=min_top,
                    )
                    if fail_reason:
                        logger.info("Skip %s %s — %s", ticker, trade_side, fail_reason)

                if fail_reason:
                    action_taken = {"status": "no_trade", "summary": fail_reason, "signal": signal}
                else:
                    exec_ask = executable_buy_best_ask_dollars(market, trade_side)
                    min_res = float(getattr(settings, "local_min_residual_payoff", 0.0))
                    gross = (1.0 - float(exec_ask)) if exec_ask is not None else None
                    entry_fail: Optional[str] = None
                    if exec_ask is None:
                        entry_fail = "Skipped — no executable buy ask"
                    elif gross is not None and gross <= 1e-9:
                        entry_fail = "Skipped — buy ask at or above $1 (no gross upside)"
                    elif (
                        gross is not None
                        and min_res > 1e-12
                        and gross + 1e-12 < min_res
                    ):
                        pay_cents = int(round(float(gross) * 100))
                        floor_cents = int(round(float(min_res) * 100))
                        entry_fail = (
                            f"Skipped — at this price a win would pay only {pay_cents}¢, "
                            f"below the {floor_cents}¢ minimum"
                        )
                    if gross is not None and 0.10 - 1e-9 <= gross < 0.12 - 1e-9:
                        decision["upside_band"] = "tight_10c"
                        xa_band = decision.get("xai_analysis")
                        if isinstance(xa_band, dict):
                            xa_band["upside_band"] = "tight_10c"
                    if entry_fail:
                        action_taken = {
                            "status": "no_trade",
                            "summary": entry_fail,
                            "signal": signal,
                        }
                    else:
                        trade_price = float(exec_ask)

                        y_ask_raw = float(market.get("yes_ask") or 0.0)
                        n_ask_raw = float(market.get("no_ask") or 0.0)
                        y_ask_f = y_ask_raw if y_ask_raw > 0 else None
                        n_ask_f = n_ask_raw if n_ask_raw > 0 else None
                        ai_yes = int(decision.get("ai_probability_yes_pct", 50) or 50)
                        edge_now = edge_pct_for_side(
                            trade_side, ai_yes, y_ask_f, n_ask_f, float(yes_price), float(no_price)
                        )
                        m_title = str(market.get("title") or decision.get("market_title") or "")
                        m_event = str(market.get("event_ticker") or "")
                        min_edge_base = effective_min_edge_for_market(
                            float(getattr(settings, "min_edge_to_buy_pct", DEFAULT_MIN_EDGE_TO_BUY_PCT)),
                            m_title,
                            m_event,
                        )
                        min_ai_base = int(
                            getattr(
                                settings,
                                "min_ai_win_prob_buy_side_pct",
                                DEFAULT_MIN_AI_WIN_PROB_BUY_SIDE_PCT,
                            )
                        )
                        eff_min_edge, eff_min_ai, risk_tier = effective_buy_gate_thresholds(
                            side=trade_side,
                            ai_yes_pct=ai_yes,
                            yes_ask=y_ask_f,
                            no_ask=n_ask_f,
                            yes_mid=float(yes_price),
                            no_mid=float(no_price),
                            min_edge_base=min_edge_base,
                            min_ai_win_prob_base=min_ai_base,
                        )
                        ai_buy = ai_win_prob_pct_on_buy_side(trade_side, ai_yes)
                        del risk_tier
                        gate_fail = autonomous_buy_gate_failure(
                            side=trade_side,
                            ai_yes_pct=ai_yes,
                            edge_pct=edge_now,
                            entry_price_dollars=float(trade_price),
                        )
                        if gate_fail:
                            action_taken = {
                                "status": "no_trade",
                                "summary": gate_fail,
                                "signal": signal,
                            }
                        elif ai_buy < eff_min_ai:
                            action_taken = {
                                "status": "no_trade",
                                "summary": (
                                    f"Skipped — the model is only {ai_buy}% confident on this side "
                                    f"(minimum is {eff_min_ai}%)"
                                ),
                                "signal": signal,
                            }
                        elif edge_now + 1e-9 < float(eff_min_edge):
                            action_taken = {
                                "status": "no_trade",
                                "summary": (
                                    f"Skipped — edge {edge_now:.1f} is below the "
                                    f"{float(eff_min_edge):.0f}-point minimum"
                                ),
                                "signal": signal,
                            }
                        else:
                            _fee = await ensure_market_fee_schedule(kalshi_client, market)
                            from src.decision_engine.strategy_math import (
                                stake_dollars_for_edge,
                                whole_contracts_for_stake,
                            )
                            from src.decision_engine.strategy_gates import all_in_entry_dollars as _all_in

                            stake = min(
                                float(balance),
                                stake_dollars_for_edge(float(balance), float(edge_now)),
                            )
                            quantity = whole_contracts_for_stake(stake, float(trade_price), float(balance))
                            while quantity > 0:
                                _cost = _all_in(
                                    quantity,
                                    float(trade_price),
                                    fee_multiplier=_fee.get("fee_multiplier"),
                                    fee_type=_fee.get("fee_type"),
                                )
                                if _cost is None or _cost <= stake + 1e-9:
                                    break
                                quantity -= 1
                            sizing_tag = "edge_pct_of_cash"
                            if quantity < 1:
                                flat = float(getattr(settings, "flat_stake_dollars", 0) or 0)
                                if flat > 0:
                                    summary = (
                                        f"Skipped — a ${stake:.2f} stake cannot buy one whole contract at this price"
                                    )
                                else:
                                    summary = (
                                        f"Skipped — ${stake:.2f} ({edge_now:.1f}% of cash) "
                                        "cannot buy one whole contract at this price"
                                    )
                                action_taken = {
                                    "status": "no_trade",
                                    "summary": summary,
                                    "signal": signal,
                                }
                            else:
                                decision["kelly_contracts"] = int(quantity)
                                traded_at = datetime.now(timezone.utc)
                                from src.clients.kalshi_client import (
                                    kalshi_order_avg_contract_price_and_cost_for_held_side,
                                    kalshi_order_fees_dollars,
                                    kalshi_order_fill_cost_dollars,
                                    kalshi_order_filled_contracts,
                                    live_ioc_buy_cap_dollars,
                                )

                                ask_px = float(trade_price)
                                ioc_limit_dollars = live_ioc_buy_cap_dollars(market, trade_side)

                                if ask_px is None or ioc_limit_dollars is None:
                                    logger.info(
                                        "Skip IOC buy %s %s — no usable ask (ask=%s cap=%s)",
                                        ticker,
                                        trade_side,
                                        ask_px,
                                        ioc_limit_dollars,
                                    )
                                    action_taken = {
                                        "status": "no_trade",
                                        "summary": "Skipped — no actionable ask for IOC buy",
                                        "signal": signal,
                                    }
                                else:
                                    buy_hold = fresh_unresolved_order_hold(ticker_clean, "buy")
                                    order_intent_id = None
                                    fill_client_order_id = None
                                    if buy_hold:
                                        await reconcile_open_intents(
                                            kalshi_client,
                                            db_factory,
                                            market_id=ticker_clean,
                                            action="buy",
                                        )
                                        buy_hold = fresh_unresolved_order_hold(ticker_clean, "buy")
                                    if buy_hold:
                                        from src.ops_status import note_unresolved

                                        note_unresolved(buy_hold)
                                        action_taken = {
                                            "status": "no_trade",
                                            "summary": f"Skipped — {buy_hold}",
                                            "signal": signal,
                                        }
                                    else:
                                        from src.bot.book_walk import (
                                            ask_levels_for_side,
                                            largest_qualifying_buy,
                                            quote_is_stale,
                                        )

                                        captured = market.get("_quote_captured_at")
                                        age_sec = None
                                        cap_dt = ensure_utc(captured) if captured is not None else None
                                        if cap_dt is not None:
                                            age_sec = (utc_now() - cap_dt).total_seconds()
                                        seconds_left, _why = _seconds_until_buy_horizon(market, now=utc_now())
                                        quote_blocked = False
                                        if quote_is_stale(age_sec, seconds_left):
                                            from src.bot.book_walk import size_still_fits

                                            fresh_market = await kalshi_client.get_market(ticker_clean)
                                            book = await kalshi_client.get_market_orderbook_fp(ticker_clean)
                                            if isinstance(fresh_market, dict):
                                                market = {**market, **fresh_market, "_quote_captured_at": utc_now()}
                                            levels = ask_levels_for_side(book, trade_side)

                                            def _qualifies(qty, limit):
                                                yes_now = float(limit) if trade_side == "YES" else float(market.get("yes_ask") or 0)
                                                no_now = float(limit) if trade_side == "NO" else float(market.get("no_ask") or 0)
                                                return size_still_fits(
                                                    quantity=int(qty),
                                                    limit=float(limit),
                                                    side=trade_side,
                                                    ai_yes_pct=ai_yes,
                                                    yes_ask=yes_now,
                                                    no_ask=no_now,
                                                    min_edge=float(eff_min_edge),
                                                    min_ai=int(eff_min_ai),
                                                    fee_multiplier=_fee.get("fee_multiplier"),
                                                    fee_type=_fee.get("fee_type"),
                                                    balance=float(balance),
                                                    min_gross=float(getattr(settings, "local_min_residual_payoff", 0) or 0),
                                                )

                                            new_qty, new_limit = largest_qualifying_buy(levels, int(quantity), _qualifies)
                                            if new_qty < 1 or new_limit is None:
                                                from src.bot.book_walk import fresh_quote_skip_reasons

                                                reasons = fresh_quote_skip_reasons(
                                                    levels,
                                                    original_ask=float(trade_price),
                                                    side=trade_side,
                                                    ai_yes_pct=ai_yes,
                                                    yes_ask=float(market.get("yes_ask") or 0),
                                                    no_ask=float(market.get("no_ask") or 0),
                                                    min_edge=float(eff_min_edge),
                                                    min_ai=int(eff_min_ai),
                                                    fee_multiplier=_fee.get("fee_multiplier"),
                                                    fee_type=_fee.get("fee_type"),
                                                    balance=float(balance),
                                                    min_gross=float(getattr(settings, "local_min_residual_payoff", 0) or 0),
                                                )
                                                quote_blocked = True
                                                action_taken = {
                                                    "status": "no_trade",
                                                    "summary": "Skipped — fresh quote: " + ", ".join(reasons),
                                                    "signal": signal,
                                                }
                                            else:
                                                quantity = int(new_qty)
                                                ioc_limit_dollars = float(new_limit)
                                                trade_price = float(new_limit)
                                        order_intent_id = None if quote_blocked else persist_order_intent(
                                            {
                                                "market_id": ticker_clean,
                                                "event_ticker": _pos_et_val,
                                                "side": trade_side,
                                                "action": "buy",
                                                "quantity": int(quantity),
                                                "limit_price": float(ioc_limit_dollars),
                                                "model_id": str(
                                                    (decision.get("xai_analysis") or {}).get("model")
                                                    or getattr(settings, "xai_model", "")
                                                    or ""
                                                ),
                                                "ai_probability_yes_pct": int(
                                                    decision.get("ai_probability_yes_pct") or 0
                                                ),
                                                "edge_pct": float(decision.get("edge_pct") or 0.0),
                                                "sizing_tag": sizing_tag or None,
                                                "kelly_contracts": int(quantity),
                                                "detail": "FOK buy intent",
                                                **quote_snapshot(market),
                                            }
                                        )
                                        if not order_intent_id and not quote_blocked:
                                            action_taken = {
                                                "status": "no_trade",
                                                "summary": (
                                                    "Skipped — order intent could not be saved, "
                                                    "so no order was sent"
                                                ),
                                                "signal": signal,
                                            }
                                    if order_intent_id:
                                        async with submission_reservation_lock():
                                            async with market_execution_lock(str(_pos_et_val or ticker_clean)):
                                                async with market_execution_lock(ticker_clean):
                                                    if not is_bot_playing(db_factory):
                                                        result = {
                                                            "error": "order not sent",
                                                            "order_not_sent": True,
                                                        }
                                                        action_taken = {
                                                            "status": "no_trade",
                                                            "summary": "Skipped — bot paused/stopped before the order",
                                                            "signal": signal,
                                                        }
                                                    elif fresh_unresolved_order_hold(
                                                        ticker_clean,
                                                        "buy",
                                                        exclude_client_order_id=order_intent_id,
                                                    ):
                                                        result = {
                                                            "error": "order not sent",
                                                            "order_not_sent": True,
                                                        }
                                                    else:
                                                        open_basis = 0.0
                                                        for open_row in db.query(Position).filter(
                                                            Position.status == "open",
                                                            Position.trade_mode == TRADE_MODE,
                                                            Position.event_ticker == _pos_et_val,
                                                        ).all():
                                                            open_basis += float(open_row.entry_cost or 0)
                                                            open_basis += float(getattr(open_row, "fees_paid", 0) or 0)
                                                        added = all_in_entry_dollars(
                                                            int(quantity),
                                                            float(trade_price),
                                                            fee_multiplier=_fee.get("fee_multiplier"),
                                                            fee_type=_fee.get("fee_type"),
                                                        )
                                                        if added is None:
                                                            result = {
                                                                "error": "order not sent",
                                                                "order_not_sent": True,
                                                            }
                                                            action_taken = {
                                                                "status": "no_trade",
                                                                "summary": (
                                                                    "Skipped — series fee schedule is unknown, "
                                                                    "so the 2.5% all-in cap is not proven"
                                                                ),
                                                                "signal": signal,
                                                            }
                                                        else:
                                                            from src.bot.exposure_reserve import unconfirmed_buy_exposure
                                                            from src.bot.match_exposure import match_exposure_key
                                                            from src.database.models import ActivityEvent

                                                            try:
                                                                local_intents = (
                                                                    db.query(ActivityEvent)
                                                                    .filter(
                                                                        ActivityEvent.action == "buy",
                                                                        ActivityEvent.event_type.in_(("order_intent", "order_ack")),
                                                                    )
                                                                    .order_by(ActivityEvent.ts_utc.desc(), ActivityEvent.seq.desc())
                                                                    .all()
                                                                )
                                                            except Exception:
                                                                local_intents = None
                                                            try:
                                                                resting_now = await kalshi_client.list_orders(
                                                                    status="resting", strict=True
                                                                )
                                                                resting_known = True
                                                            except Exception as exc:
                                                                from src.ops_status import note_api_error

                                                                note_api_error(type(exc).__name__)
                                                                resting_now = None
                                                                resting_known = False
                                                            exposure = unconfirmed_buy_exposure(
                                                                local_rows=local_intents,
                                                                resting_orders=resting_now,
                                                                resting_known=resting_known,
                                                                event_ticker=str(_pos_et_val or ""),
                                                                market_id=ticker_clean,
                                                                exclude_client_order_id=order_intent_id,
                                                                fee_type=_fee.get("fee_type"),
                                                                fee_multiplier=_fee.get("fee_multiplier"),
                                                            )
                                                            if exposure.get("reason"):
                                                                result = {
                                                                    "error": "order not sent",
                                                                    "order_not_sent": True,
                                                                }
                                                                action_taken = {
                                                                    "status": "no_trade",
                                                                    "summary": f"Skipped — {exposure['reason']}",
                                                                    "signal": signal,
                                                                }
                                                            elif match_exposure_key(ticker_clean) in held_match_keys:
                                                                result = {
                                                                    "error": "order not sent",
                                                                    "order_not_sent": True,
                                                                }
                                                                action_taken = {
                                                                    "status": "no_trade",
                                                                    "summary": (
                                                                        "Skipped — one open position already covers this match"
                                                                    ),
                                                                    "signal": signal,
                                                                }
                                                            else:
                                                                from src.bot.shard_funding import fund_buy_shard
                                                                from src.clients.kalshi_client import known_exchange_index

                                                                shard_index = known_exchange_index(market.get("exchange_index"))
                                                                if shard_index is None:
                                                                    funding = {"ok": True}
                                                                else:
                                                                    funding = await fund_buy_shard(
                                                                        kalshi_client,
                                                                        exchange_index=shard_index,
                                                                        order_all_in=float(added),
                                                                        bankroll=float(balance),
                                                                        ticker=ticker_clean,
                                                                        client_order_id=str(order_intent_id or ""),
                                                                    )
                                                                if not funding.get("ok"):
                                                                    result = {
                                                                        "error": "order not sent",
                                                                        "order_not_sent": True,
                                                                    }
                                                                    action_taken = {
                                                                        "status": "no_trade",
                                                                        "summary": funding.get("reason")
                                                                        or f"shard {shard_index} funding failed",
                                                                        "signal": signal,
                                                                    }
                                                                    note_untradeable(
                                                                        ticker_clean,
                                                                        "shard_funding",
                                                                        minutes=int(funding.get("cooldown_minutes") or 30),
                                                                    )
                                                                    logger.info(
                                                                        "Skip buy %s — %s",
                                                                        ticker,
                                                                        action_taken["summary"],
                                                                    )
                                                                else:
                                                                    result = await kalshi_client.place_buy_ioc_limit(
                                                                        ticker,
                                                                        trade_side,
                                                                        quantity,
                                                                        ioc_limit_dollars,
                                                                        client_order_id=order_intent_id,
                                                                        exchange_index=shard_index,
                                                                    )
                                                record_order_ack(
                                                    result,
                                                    {
                                                        "market_id": ticker_clean,
                                                        "event_ticker": _pos_et_val,
                                                        "side": trade_side,
                                                        "action": "buy",
                                                        "quantity": int(quantity),
                                                        "limit_price": float(ioc_limit_dollars),
                                                        "client_order_id": order_intent_id,
                                                        "model_id": str(
                                                            (decision.get("xai_analysis") or {}).get("model")
                                                            or getattr(settings, "xai_model", "")
                                                            or ""
                                                        ),
                                                        **quote_snapshot(market),
                                                    },
                                                )
                                    else:
                                        result = {"error": "order not sent", "order_not_sent": True}
                                    from src.bot.book_walk import is_insufficient_resting_volume

                                    volume_miss = bool(
                                        result.get("error")
                                        and is_insufficient_resting_volume(
                                            result.get("error"), result.get("status_code")
                                        )
                                    )
                                    if volume_miss:
                                        result = {
                                            "status": "canceled",
                                            "fill_count_fp": "0.00",
                                            "client_order_id": order_intent_id,
                                            "missed_volume": True,
                                        }
                                    if result.get("order_not_sent"):
                                        pass
                                    elif result.get("error"):
                                        logger.warning(
                                            "Live buy failed %s %s: %s",
                                            ticker,
                                            trade_side,
                                            result["error"],
                                        )
                                        route_kind = classify_routing_error(
                                            str(result.get("error") or ""),
                                            result.get("status_code"),
                                        )
                                        if route_kind in ("exchange_shard_user", "exchange_shard_balance"):
                                            note_untradeable(ticker_clean, route_kind)
                                            from src.ops_status import note_rejection

                                            note_rejection()
                                            logger.error(
                                                "Order rejected %s %s class=%s http=%s "
                                                "exchange_index=%s ticker_field=ticker detail=%s",
                                                ticker,
                                                trade_side,
                                                route_kind,
                                                result.get("status_code"),
                                                market.get("exchange_index") if market.get("exchange_index") is not None else "omitted",
                                                result.get("error"),
                                            )
                                        action_taken = {
                                            "status": "rejected",
                                            "summary": f"Kalshi error: {result['error']}",
                                            "signal": signal,
                                            "error_class": route_kind,
                                        }
                                    else:
                                        from src.fixed_point import FixedPointCountError, format_count_fp, is_whole_count

                                        try:
                                            filled = kalshi_order_filled_contracts(result)
                                        except FixedPointCountError as exc:
                                            filled = None
                                            action_taken = {
                                                "status": "fractional_fill",
                                                "summary": f"Exchange fill count was not a fixed-point string ({exc})",
                                                "signal": signal,
                                            }
                                        if filled is not None and not is_whole_count(filled):
                                            from decimal import ROUND_DOWN

                                            whole = int(filled.to_integral_value(rounding=ROUND_DOWN))
                                            frac = filled - type(filled)(whole)
                                            residue = format_count_fp(frac) if frac > 0 else ""
                                            sold_residue = False
                                            if residue:
                                                cleanup = await kalshi_client.sell_fractional_residue(
                                                    ticker, trade_side, residue
                                                )
                                                sold_residue = (
                                                    not cleanup.get("error")
                                                    and float(kalshi_order_filled_contracts(cleanup) or 0) > 0
                                                )
                                                record_activity(
                                                    {
                                                        "event_type": "fill",
                                                        "market_id": ticker_clean,
                                                        "side": trade_side,
                                                        "action": "sell" if sold_residue else "buy",
                                                        "order_status": "fractional_fill",
                                                        "fill_count_fp": residue,
                                                        "detail": f"Sold fractional residue {residue}; kept {whole} whole contracts",
                                                    }
                                                )
                                            qty_int = whole
                                            if whole < 1:
                                                action_taken = {
                                                    "status": "fractional_fill",
                                                    "summary": (
                                                        f"Exchange filled {format_count_fp(filled)}. "
                                                        + (
                                                            f"The {residue} residue was sold."
                                                            if sold_residue
                                                            else "The fractional residue was not sold and was not booked."
                                                        )
                                                    ),
                                                    "signal": signal,
                                                    "filled_fp": format_count_fp(filled),
                                                    "residue_sold": sold_residue,
                                                }
                                        elif filled is not None:
                                            qty_int = int(filled)
                                        else:
                                            qty_int = 0
                                        if filled is not None and qty_int < 1 and is_whole_count(filled):
                                            from src.bot.buy_retry import retry_buy_once

                                            retry_result, gap = await retry_buy_once(
                                                kalshi_client,
                                                market_id=ticker_clean,
                                                side=trade_side,
                                                quantity=int(quantity),
                                                original_ask=float(ioc_limit_dollars),
                                                ai_yes_pct=int(decision.get("ai_probability_yes_pct") or 0),
                                                min_edge=float(eff_min_edge),
                                                min_ai=int(eff_min_ai),
                                                fee_multiplier=_fee.get("fee_multiplier"),
                                                fee_type=_fee.get("fee_type"),
                                                exchange_index=shard_index,
                                                balance=float(balance),
                                                event_equity=float(total_portfolio_value),
                                                event_open_cost=float(open_basis),
                                                persist_intent=lambda limit, qty: persist_order_intent(
                                                    {
                                                        "market_id": ticker_clean,
                                                        "event_ticker": _pos_et_val,
                                                        "side": trade_side,
                                                        "action": "buy",
                                                        "quantity": int(qty),
                                                        "limit_price": float(limit),
                                                        "detail": "retry buy intent",
                                                        **quote_snapshot(market),
                                                    }
                                                ),
                                                fund=(
                                                    None
                                                    if shard_index is None
                                                    else (
                                                        lambda cost: fund_buy_shard(
                                                            kalshi_client,
                                                            exchange_index=shard_index,
                                                            order_all_in=float(cost),
                                                            bankroll=float(balance),
                                                            ticker=ticker_clean,
                                                        )
                                                    )
                                                ),
                                                acknowledge=lambda res, oid: record_order_ack(
                                                    res,
                                                    {
                                                        "market_id": ticker_clean,
                                                        "side": trade_side,
                                                        "action": "buy",
                                                        "client_order_id": oid,
                                                        "detail": "retry buy",
                                                    },
                                                ),
                                            )
                                            fill_client_order_id = order_intent_id
                                            if isinstance(retry_result, dict) and not retry_result.get("error"):
                                                try:
                                                    retry_filled = kalshi_order_filled_contracts(retry_result)
                                                except FixedPointCountError:
                                                    retry_filled = None
                                                if (
                                                    retry_filled is not None
                                                    and is_whole_count(retry_filled)
                                                    and int(retry_filled) >= 1
                                                ):
                                                    result = retry_result
                                                    filled = retry_filled
                                                    qty_int = int(retry_filled)
                                                    fill_client_order_id = str(
                                                        retry_result.get("client_order_id") or order_intent_id
                                                    )
                                            if qty_int < 1:
                                                summary = (
                                                    "Missed - price moved"
                                                    if volume_miss
                                                    else (
                                                        "Missed buy. Price gap "
                                                        f"{gap:+.2f} after one reprice. "
                                                        "The market is on the short watch list."
                                                    )
                                                )
                                                logger.warning(
                                                    "Live buy had no whole-contract fill — %s %s gap=%+.4f status=%s",
                                                    ticker,
                                                    trade_side,
                                                    gap,
                                                    result.get("status"),
                                                )
                                                action_taken = {
                                                    "status": "missed_fill",
                                                    "summary": summary,
                                                    "signal": signal,
                                                    "kalshi_status": result.get("status"),
                                                    "filled_fp": filled,
                                                    "price_gap": gap,
                                                }
                                        if qty_int >= 1:
                                            if qty_int < quantity:
                                                logger.info(
                                                    "Live IOC buy partial fill %s %s filled=%d requested=%d",
                                                    ticker_clean,
                                                    trade_side,
                                                    qty_int,
                                                    quantity,
                                                )
                                            avg_px, full_cost = kalshi_order_avg_contract_price_and_cost_for_held_side(
                                                result,
                                                held_side=trade_side,
                                                filled=filled,
                                                fallback_per_contract_dollars=float(ioc_limit_dollars),
                                            )
                                            scale = qty_int / float(filled) if filled > 1e-9 else 1.0
                                            buy_fees = float(kalshi_order_fees_dollars(result)) * scale
                                            booked = apply_cumulative_fill(
                                                db,
                                                action="buy",
                                                side=trade_side,
                                                market_id=ticker_clean,
                                                cumulative_qty=qty_int,
                                                cumulative_fees=buy_fees,
                                                price=float(avg_px),
                                                client_order_id=str(fill_client_order_id or order_intent_id or ""),
                                                exchange_order_id=str(result.get("order_id") or ""),
                                                event_ticker=_pos_et_val,
                                                market_title=title,
                                                order=result,
                                                entry_decision_log_id=decision_log_row_id,
                                                stop_loss_drawdown_pct_at_entry=None,
                                                close_time=contractual_close,
                                                expected_expiration_time=expected_expiration_iso,
                                            )
                                            if not booked.get("ok"):
                                                action_taken = {
                                                    "status": "no_trade",
                                                    "summary": f"Skipped — {booked.get('reason')}",
                                                    "signal": signal,
                                                }
                                                result = {"error": booked.get("reason"), "order_not_sent": True}
                                            else:
                                                fill_notional = float(qty_int) * float(avg_px)
                                                action_taken = {
                                                "status": "partial" if qty_int < int(quantity) else "executed",
                                                "side": trade_side,
                                                "quantity": qty_int,
                                                "price": avg_px,
                                                "cost": round(fill_notional + buy_fees, 6),
                                                "at": utc_iso_z(traded_at),
                                                "execution": "fok",
                                                "ioc_limit_dollars": round(ioc_limit_dollars, 4),
                                                }
                                                executed_trade = True
                                                trade_quantity = qty_int
                                                trade_price = avg_px
                                                trade_cost = fill_notional + buy_fees
            decision["action_taken"] = action_taken
            if str(decision.get("decision") or "").startswith("BUY"):
                taken = str((action_taken or {}).get("status") or "")
                if taken not in ("executed", "partial"):
                    from src.shadow_buys import record_shadow_buy

                    try:
                        held = str(trade_side or "").upper()
                        blob = decision.get("xai_analysis")
                        record_shadow_buy(
                            db,
                            market_id=ticker_clean,
                            side=held,
                            reason=str((action_taken or {}).get("summary") or taken or "skipped"),
                            edge_pct=float(decision.get("edge_pct") or 0.0),
                            ai_yes_pct=int(decision.get("ai_probability_yes_pct") or 0),
                            ask=float(
                                executable_buy_best_ask_dollars(market, held)
                                or (yes_price if held == "YES" else no_price)
                                or 0.0
                            ),
                            blended_side_pct=blob.get("blended_side_pct") if isinstance(blob, dict) else None,
                        )
                    except Exception:
                        logger.debug("Shadow buy was not recorded", exc_info=True)

            _snap = {
                "yes_price": float(yes_price),
                "no_price": float(no_price),
                "volume": float(volume or 0.0),
                "expires_in_days": float(expires_days) if expires_days is not None else None,
            }
            decision["yes_price"] = _snap["yes_price"]
            decision["no_price"] = _snap["no_price"]
            decision["volume"] = _snap["volume"]
            decision["expires_in_days"] = _snap["expires_in_days"]

            from src.event_live_lookup import attach_lookup_cost

            attach_lookup_cost(decision, str(market.get("event_ticker") or market.get("id") or ""))
            db.add(DecisionLog(
                id=decision_log_row_id,
                market_id=ticker_clean,
                market_title=title,
                decision=decision.get("decision", "SKIP"),
                xai_analysis=dumps_payload(decision.get("xai_analysis", {})),
                confidence=float(decision.get("confidence", 0.0) or 0.0),
                reasoning=decision.get("reasoning", ""),
                real_time_context=decision.get("real_time_context", ""),
                key_factors=dumps_payload(decision.get("key_factors", [])),
                yes_confidence=int(decision.get("yes_confidence", int(yes_price * 100))),
                no_confidence=int(decision.get("no_confidence", int(no_price * 100))),
                escalated_to_xai=decision.get("escalated_to_xai", False),
                edge=float(decision.get("edge_pct", 0.0) or 0.0),
                ai_probability_yes_pct=int(decision.get("ai_probability_yes_pct", 50) or 50),
                market_implied_probability_pct=int(decision.get("market_implied_probability_pct", 0) or 0),
                kelly_contracts=int(decision.get("kelly_contracts", 0) or 0),
                filter_pass=True,
                action_taken=dumps_payload(action_taken),
                market_context=dumps_payload(_snap),
                snapshot_yes_price=_snap["yes_price"],
                snapshot_no_price=_snap["no_price"],
                snapshot_volume=_snap["volume"],
                snapshot_expires_days=_snap["expires_in_days"],
                trade_mode=TRADE_MODE,
            ))
            xa_model = ""
            xa_blob = decision.get("xai_analysis")
            if isinstance(xa_blob, dict):
                xa_model = str(xa_blob.get("model") or "")
            db.commit()
            record_activity(
                {
                    "event_type": "decision",
                    "market_id": ticker_clean,
                    "event_ticker": _pos_et_val,
                    "side": trade_side or decision.get("direction"),
                    "action": "buy" if str(decision.get("decision") or "").startswith("BUY") else None,
                    "model_id": xa_model or str(getattr(settings, "xai_model", "") or ""),
                    "ai_probability_yes_pct": int(decision.get("ai_probability_yes_pct") or 0),
                    "market_implied_pct": int(decision.get("market_implied_probability_pct") or 0),
                    "edge_pct": float(decision.get("edge_pct") or 0.0),
                    "sizing_tag": sizing_tag or None,
                    "kelly_contracts": int(decision.get("kelly_contracts") or 0),
                    "order_status": str((action_taken or {}).get("status") or ""),
                    "detail": scrub_text((action_taken or {}).get("summary") or decision.get("action_summary") or ""),
                    "model_usage": decision_model_usage(xa_blob if isinstance(xa_blob, dict) else None),
                    **quote_snapshot(market),
                }
            )

            decision["trade_mode"] = TRADE_MODE
            decision["market_id"] = ticker_clean
            for _k in ("local_score", "risk_level", "target_price"):
                decision.pop(_k, None)
            _xa = decision.get("xai_analysis")
            if isinstance(_xa, dict):
                for _k in ("risk_level", "target_price"):
                    _xa.pop(_k, None)
            enrich_analysis_ai_provider(decision)
            await broadcast_fn({"type": "analysis", "data": decision})

            if executed_trade:
                held_tickers.add((ticker_clean or "").strip().upper())
                held_match_keys.add(match_exposure_key(ticker_clean))
                balance -= trade_cost
                num_open += 1

                # Lock the full event series so we never buy a contradictory sibling strike later.
                et_lock = (market.get("event_ticker") or "").strip().upper()
                if et_lock:
                    try:
                        mid_lock = normalize_market_id(str(ticker)).upper()
                        existing = (
                            db.query(EventSeriesLock)
                            .filter(
                                EventSeriesLock.trade_mode == TRADE_MODE,
                                EventSeriesLock.event_ticker == et_lock,
                            )
                            .first()
                        )
                        if existing is None:
                            db.add(
                                EventSeriesLock(
                                    id=str(uuid.uuid4()),
                                    trade_mode=TRADE_MODE,
                                    event_ticker=et_lock,
                                    chosen_market_id=mid_lock,
                                    chosen_side=str(trade_side or "").upper() if trade_side else None,
                                )
                            )
                            db.commit()
                            locked_event_tickers.add(et_lock)
                            allowed_market_id_by_event[et_lock] = mid_lock
                            logger.info("Locked event series %s to %s (%s)", et_lock, mid_lock, trade_side)
                    except Exception as e:
                        logger.warning("Failed to lock event series %s: %s", et_lock, e)

                await broadcast_fn({
                    "type": "trade_placed",
                    "data": {
                        "market_id": ticker_clean,
                        "market_title": title,
                        "side": trade_side,
                        "quantity": trade_quantity,
                        "price": trade_price,
                        "mode": TRADE_MODE,
                    },
                })

                await asyncio.sleep(1)

            if stop_scan_after:
                break

        if premodel_skip_counts.get("fee_unknown") or premodel_skip_counts.get("cash_cap"):
            logger.info(
                "Scan pre-model skips this sweep: fee_schedule_unknown=%d cash_cap=%d",
                premodel_skip_counts.get("fee_unknown", 0),
                premodel_skip_counts.get("cash_cap", 0),
            )
        logger.info(
            "Scan sweep finished: processed %d unit(s) in ~%.1fs",
            len(scan_queue),
            time.monotonic() - _sweep_started,
        )
        from src.event_live_lookup import flush_unbilled_lookups

        flush_unbilled_lookups(db)

    except Exception as e:
        logger.exception("Error in scan_and_trade: %s", scrub_text(e))
        record_activity(
            {
                "event_type": "error",
                "detail": "scan_and_trade failed",
                "error": type(e).__name__,
            }
        )
        db.rollback()
        try:
            from src.event_live_lookup import flush_unbilled_lookups

            flush_unbilled_lookups(db)
        except Exception:
            pass
    finally:
        try:
            from src.clients.xai_client import log_live_lookup_summary

            log_live_lookup_summary()
        except Exception:
            pass
        db.close()


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run_bot_loop(
    kalshi_client,
    decision_engine,
    db_factory,
    broadcast_fn: Callable[[dict], Coroutine],
    settings,
):
    """Position checks and the market scan run as two tasks.

    A slow scan no longer delays stop-loss monitoring. Stop still disables
    automatic exits. Pause still allows them. New buys still require Play.
    """
    logger.info("Bot loop started (scan_interval=%ds, monitor separate)", settings.bot_scan_interval)
    from src.api.tuning import sync_runtime_from_db

    stop = asyncio.Event()

    async def _monitor_once() -> None:
        db_tune = db_factory()
        try:
            sync_runtime_from_db(db_tune)
        finally:
            db_tune.close()
        state = get_bot_state_str(db_factory)
        from src.ops_status import stamp_monitor

        stamp_monitor()
        await reconcile_open_intents(kalshi_client, db_factory)
        logger.info(
            "Position monitor — state=%s trade_mode=%s exits=%s",
            state,
            TRADE_MODE,
            "on" if state != "stop" else "off",
        )
        await monitor_positions(
            kalshi_client,
            db_factory,
            broadcast_fn,
            settings,
            allow_exits=(state != "stop"),
            bot_state_label=state,
        )
        await _maybe_auto_refresh_closed_resolution(
            kalshi_client,
            db_factory,
            broadcast_fn,
            settings,
        )

    async def _scan_once() -> None:
        db_tune = db_factory()
        try:
            sync_runtime_from_db(db_tune)
        finally:
            db_tune.close()
        state = get_bot_state_str(db_factory)
        if state != "play":
            return
        scan_timeout = int(getattr(settings, "bot_loop_scan_timeout_sec", 0) or 0)
        try:
            if scan_timeout > 0:
                await asyncio.wait_for(
                    scan_and_trade(
                        kalshi_client, decision_engine, db_factory, broadcast_fn, settings
                    ),
                    timeout=float(scan_timeout),
                )
            else:
                await scan_and_trade(
                    kalshi_client, decision_engine, db_factory, broadcast_fn, settings
                )
        except asyncio.TimeoutError:
            logger.error(
                "scan_and_trade exceeded bot_loop_scan_timeout_sec=%ss — this sweep stopped; "
                "position monitoring continues",
                scan_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Unhandled error in scan task: %s", e)

    try:
        await run_parallel_cycles(
            _monitor_once,
            _scan_once,
            stop,
            float(settings.bot_scan_interval),
        )
    except asyncio.CancelledError:
        stop.set()
        logger.info("Bot loop cancelled")
        raise
