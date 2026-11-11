"""Durable, secret-free trace of decisions, order intents, fills, marks, and settlements.

Order submission must call :func:`persist_order_intent` (or :func:`persist_client_order_id`)
and refuse to send when that write fails. Logging uses its own DB session so a later
trading rollback cannot erase the intent row.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.config import settings
from src.database.models import ActivityEvent, get_session_local
from src.logger import setup_logging
from src.reconcile.open_positions import normalize_market_id
from src.util.datetimes import utc_iso_z, utc_now
from src.version import APP_VERSION

logger = setup_logging("activity_log")

# A locked writer should wait briefly, not for the process-wide 60s timeout.
ACTIVITY_BUSY_TIMEOUT_MS = 1000
_LOCK_ATTEMPTS = 4
_LOCK_WAITS = (0.05, 0.15, 0.3)

# Keep the table from growing without bound on a long-running local bot.
_MAX_ROWS = 4000
_PRUNE_EVERY = 25
_HOLD_MINUTES = 30
UNRESOLVED_ORDER_STATUSES = ("pending", "ambiguous", "open_at_exchange")

_writes = 0
_seq = 0

_SECRET_HINTS = (
    "api_key",
    "api-key",
    "private_key",
    "private key",
    "authorization",
    "bearer ",
    "begin private",
    "begin rsa",
    "x-vibe-bot-token",
    "kalshi-access-key",
    "kalshi-access-signature",
    ".pem",
)

_SAFE_SETTING_ATTRS = (
    "min_edge_to_buy_pct",
    "min_ai_win_prob_buy_side_pct",
    "stop_loss_drawdown_pct",
    "stop_loss_selling_enabled",
    "bot_max_open_positions",
    "bot_max_spread",
    "bot_min_top_size",
    "bot_min_volume",
    "bot_max_hours",
    "local_min_residual_payoff",
    "reentry_cooldown_minutes",
    "exit_grace_minutes",
    "xai_model",
    "ai_temperature",
    "host",
)

_RUN_ID = str(uuid.uuid4())


def _load_git_sha() -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        sha = out.decode("ascii", errors="ignore").strip()
        if sha and len(sha) <= 40 and all(c in "0123456789abcdef" for c in sha):
            return sha
    except Exception:
        return None
    return None


_GIT_SHA = _load_git_sha()


def scrub_text(value: object, limit: int = 400) -> str:
    """Short single-line text. Drops anything that looks like a credential."""
    text = " ".join(str(value or "").split())
    if any(hint in text.lower() for hint in _SECRET_HINTS):
        return "redacted"
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def nonsecret_settings_snapshot(source: Any = None) -> str:
    src = source if source is not None else settings
    snap: Dict[str, Any] = {}
    for name in _SAFE_SETTING_ATTRS:
        if not hasattr(src, name):
            continue
        val = getattr(src, name)
        if isinstance(val, (bool, int, float, str)) or val is None:
            snap[name] = val
    raw = json.dumps(snap, separators=(",", ":"), sort_keys=True)
    return raw if len(raw) <= 2000 else raw[:1999] + "…"


def quote_snapshot(market: Optional[dict]) -> Dict[str, Any]:
    """Numeric book fields plus age. Ignores any other keys on the market dict."""
    if not isinstance(market, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        try:
            val = market.get(key)
            out[key] = None if val is None else float(val)
        except (TypeError, ValueError):
            out[key] = None
    captured = market.get("_quote_captured_at")
    if captured is not None and hasattr(captured, "tzinfo"):
        out["quote_captured_at"] = utc_iso_z(captured)
        try:
            age = (utc_now() - captured).total_seconds() * 1000.0
            out["quote_age_ms"] = int(max(0, age))
        except Exception:
            out["quote_age_ms"] = None
    return out


def _apply(row: ActivityEvent, payload: Dict[str, Any]) -> None:
    row.run_id = _RUN_ID
    row.app_version = APP_VERSION
    row.git_sha = _GIT_SHA
    row.event_type = scrub_text(payload.get("event_type") or "note", limit=40)
    mid = payload.get("market_id")
    row.market_id = normalize_market_id(str(mid)) if mid else None
    et = payload.get("event_ticker")
    row.event_ticker = scrub_text(et, limit=80) if et else None
    row.side = scrub_text(payload.get("side") or "", limit=8) or None
    row.action = scrub_text(payload.get("action") or "", limit=8) or None
    row.model_id = scrub_text(payload.get("model_id") or "", limit=80) or None
    for int_name in ("ai_probability_yes_pct", "market_implied_pct", "kelly_contracts", "quantity", "quote_age_ms"):
        val = payload.get(int_name)
        try:
            setattr(row, int_name, None if val is None else int(val))
        except (TypeError, ValueError):
            setattr(row, int_name, None)
    for float_name in ("edge_pct", "limit_price", "fill_price", "fill_qty", "fees_dollars", "yes_bid", "yes_ask", "no_bid", "no_ask"):
        val = payload.get(float_name)
        try:
            setattr(row, float_name, None if val is None else float(val))
        except (TypeError, ValueError):
            setattr(row, float_name, None)
    cid = payload.get("client_order_id")
    row.client_order_id = scrub_text(cid, limit=80) if cid else None
    ex_id = payload.get("exchange_order_id")
    row.exchange_order_id = scrub_text(ex_id, limit=80) if ex_id else None
    row.fee_kind = scrub_text(payload.get("fee_kind") or "", limit=32) or None
    row.fill_count_fp = scrub_text(payload.get("fill_count_fp") or "", limit=32) or None
    row.order_status = scrub_text(payload.get("order_status") or "", limit=32) or None
    row.quote_captured_at = scrub_text(payload.get("quote_captured_at") or "", limit=40) or None
    row.sizing_tag = scrub_text(payload.get("sizing_tag") or "", limit=40) or None
    row.settings_json = payload.get("settings_json") if isinstance(payload.get("settings_json"), str) else nonsecret_settings_snapshot()
    if row.settings_json and any(hint in row.settings_json.lower() for hint in _SECRET_HINTS):
        row.settings_json = "{}"
    row.detail = scrub_text(payload.get("detail") or "", limit=500) or None
    err = payload.get("error")
    row.error = scrub_text(err, limit=120) or None


def _prune(db) -> None:
    cutoff = (
        db.query(ActivityEvent.ts_utc)
        .order_by(ActivityEvent.ts_utc.desc(), ActivityEvent.seq.desc())
        .offset(_MAX_ROWS)
        .limit(1)
        .first()
    )
    if not cutoff or cutoff[0] is None:
        return
    db.query(ActivityEvent).filter(ActivityEvent.ts_utc < cutoff[0]).delete(synchronize_session=False)


_USAGE_KEYS = (
    "prompt_version",
    "input_tokens",
    "output_tokens",
    "estimated_usd",
    "actual_usd",
    "duration_sec",
    "cost_label",
    "estimate_is_not_a_bill",
)


def decision_model_usage(analysis: Optional[dict]) -> Optional[dict]:
    """Numeric model accounting only. Prompt text is not copied."""
    if not isinstance(analysis, dict):
        return None
    usage = analysis.get("usage") if isinstance(analysis.get("usage"), dict) else {}
    out = {
        "prompt_version": analysis.get("prompt_version"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "estimated_usd": usage.get("estimated_usd"),
        "actual_usd": usage.get("actual_usd"),
        "duration_sec": usage.get("duration_sec"),
        "cost_label": usage.get("cost_label"),
        "estimate_is_not_a_bill": usage.get("estimate_is_not_a_bill"),
    }
    if not any(val is not None for val in out.values()):
        return None
    return out


def _public_model_usage(payload: Dict[str, Any]) -> Optional[dict]:
    raw = payload.get("model_usage")
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in _USAGE_KEYS:
        val = raw.get(key)
        if isinstance(val, (str, int, float, bool)) or val is None:
            out[key] = val
    return out or None


def _append_durable_event(row, model_usage: Optional[dict] = None) -> None:
    """Append one scrubbed line outside the pruned activity table. Failures are ignored."""
    try:
        day = (row.ts_utc or utc_now()).strftime("%Y%m%d")
        directory = Path(__file__).resolve().parents[1] / "logs" / "events"
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "id": row.id,
                "run_id": row.run_id,
                "ts_utc": row.ts_utc.isoformat() if getattr(row.ts_utc, "isoformat", None) else str(row.ts_utc or ""),
                "event_type": row.event_type,
                "market_id": row.market_id,
                "event_ticker": row.event_ticker,
                "side": row.side,
                "action": row.action,
                "quantity": row.quantity,
                "limit_price": row.limit_price,
                "fill_qty": row.fill_qty,
                "fill_price": row.fill_price,
                "client_order_id": row.client_order_id,
                "exchange_order_id": row.exchange_order_id,
                "order_status": row.order_status,
                "fees_dollars": row.fees_dollars,
                "fee_kind": row.fee_kind,
                "model_usage": model_usage,
                "detail": row.detail,
            },
            default=str,
        )
        with (directory / f"events-{day}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        return


def _sqlite_locked(exc: BaseException) -> bool:
    text = f"{exc} {getattr(exc, 'orig', '')}".lower()
    return "locked" in text or "database is busy" in text or "sqlite_busy" in text


def _prepare_activity_sqlite(db) -> None:
    """WAL plus a short busy wait on the connection that writes this row."""
    try:
        bind = db.get_bind()
    except Exception:
        return
    if bind is None or getattr(getattr(bind, "dialect", None), "name", "") != "sqlite":
        return
    raw = bind.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
        raw.commit()
    finally:
        raw.close()
    db.connection().exec_driver_sql(f"PRAGMA busy_timeout={int(ACTIVITY_BUSY_TIMEOUT_MS)}")


def record_activity(payload: Dict[str, Any], *, session_factory: Optional[Callable] = None) -> bool:
    """Insert one trace row. Returns False when the write fails. Never raises."""
    global _writes, _seq
    factory = session_factory or get_session_local()
    last_exc: Optional[BaseException] = None
    for attempt in range(_LOCK_ATTEMPTS):
        db = factory()
        try:
            _prepare_activity_sqlite(db)
            _seq += 1
            row = ActivityEvent(id=str(uuid.uuid4()), seq=_seq)
            _apply(row, payload)
            db.add(row)
            db.commit()
            _append_durable_event(row, _public_model_usage(payload))
            _writes += 1
            if _writes % _PRUNE_EVERY == 0:
                _prune(db)
                db.commit()
            return True
        except Exception as exc:
            last_exc = exc
            try:
                db.rollback()
            except Exception:
                pass
            if _sqlite_locked(exc) and attempt + 1 < _LOCK_ATTEMPTS:
                logger.debug(
                    "activity log write locked; retry %s/%s: %s",
                    attempt + 1,
                    _LOCK_ATTEMPTS,
                    exc,
                )
                time.sleep(_LOCK_WAITS[attempt])
                continue
            logger.error("activity log write failed (%s): %s", type(exc).__name__, exc)
            logger.debug("activity log write failed", exc_info=exc)
            return False
        finally:
            db.close()
    if last_exc is not None:
        logger.error("activity log write failed (%s): %s", type(last_exc).__name__, last_exc)
        logger.debug("activity log write failed", exc_info=last_exc)
    return False


def persist_order_intent(payload: Dict[str, Any], *, session_factory: Optional[Callable] = None) -> Optional[str]:
    """Save a pending intent and return its ``client_order_id``, or None if it was not saved."""
    cid = str(uuid.uuid4())
    body = dict(payload)
    body["event_type"] = "order_intent"
    body["order_status"] = "pending"
    body["client_order_id"] = cid
    if record_activity(body, session_factory=session_factory):
        return cid
    logger.error(
        "Refusing %s order for %s — activity log did not persist the intent",
        scrub_text(payload.get("action") or "order", limit=8),
        scrub_text(payload.get("market_id") or "", limit=80),
    )
    return None


def persist_client_order_id(
    client_order_id: str,
    payload: Dict[str, Any],
    *,
    session_factory: Optional[Callable] = None,
) -> bool:
    """Persist a caller-supplied id before that exact id is sent. False means do not send."""
    cid = (client_order_id or "").strip()
    if not cid:
        return False
    body = dict(payload)
    body["event_type"] = "order_intent"
    body["order_status"] = "pending"
    body["client_order_id"] = cid
    ok = record_activity(body, session_factory=session_factory)
    if not ok:
        logger.error(
            "Refusing order %s — activity log did not persist the intent",
            scrub_text(cid, limit=80),
        )
    return ok


def make_order_submit_gate(payload: Dict[str, Any], *, session_factory: Optional[Callable] = None):
    """Return ``before_submit(client_order_id) -> bool`` that persists that id before HTTP."""

    def _gate(client_order_id: str) -> bool:
        return persist_client_order_id(client_order_id, dict(payload), session_factory=session_factory)

    return _gate


def fresh_unresolved_order_hold(
    market_id: str,
    action: str,
    *,
    session_factory: Optional[Callable] = None,
    within_minutes: int = _HOLD_MINUTES,
    exclude_client_order_id: Optional[str] = None,
) -> Optional[str]:
    """Block a repeat send while any intent for this market is still unresolved.

    ``within_minutes`` is ignored. Age is not proof that the exchange never
    accepted the order. The row stays in SQLite across a process restart.
    """
    del within_minutes
    factory = session_factory or get_session_local()
    db = factory()
    try:
        mid = normalize_market_id(str(market_id or ""))
        act = (action or "").strip().lower()
        skip = (exclude_client_order_id or "").strip()
        rows = (
            db.query(ActivityEvent)
            .filter(
                ActivityEvent.market_id == mid,
                ActivityEvent.action == act,
                ActivityEvent.event_type.in_(("order_intent", "order_ack")),
            )
            .all()
        )
        from src.intent_lifecycle import select_intent_lifecycle

        selected = select_intent_lifecycle(rows)
        for row in selected["current"]:
            cid = (row.client_order_id or "").strip()
            if skip and cid == skip:
                continue
            status = (row.order_status or "").strip().lower()
            return (
                f"unresolved {act} ({status}) client_order_id={row.client_order_id}; "
                "not sending another order until that id is reconciled"
            )
        if selected["legacy_without_client_order_id"]:
            return (
                f"unresolved {act} has no client order id; "
                "not sending another order until that history is reconciled"
            )
        return None
    except Exception as exc:
        logger.error("activity log hold check failed (%s)", type(exc).__name__)
        return "activity log could not be read; not sending an order that might duplicate an in-flight attempt"
    finally:
        db.close()


def unresolved_intent(
    market_id: str,
    action: str,
    *,
    session_factory: Optional[Callable] = None,
) -> Optional[dict]:
    """Newest unresolved intent, including rows written by a previous process."""
    factory = session_factory or get_session_local()
    db = factory()
    try:
        mid = normalize_market_id(str(market_id or ""))
        act = (action or "").strip().lower()
        row = (
            db.query(ActivityEvent)
            .filter(
                ActivityEvent.market_id == mid,
                ActivityEvent.action == act,
                ActivityEvent.event_type.in_(("order_intent", "order_ack")),
                ActivityEvent.order_status.in_(("pending", "ambiguous")),
            )
            .order_by(ActivityEvent.ts_utc.desc(), ActivityEvent.seq.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "client_order_id": row.client_order_id,
            "exchange_order_id": row.exchange_order_id,
            "quantity": row.quantity,
            "limit_price": row.limit_price,
            "order_status": row.order_status,
            "event_ticker": row.event_ticker,
            "ts_utc": row.ts_utc.isoformat() if getattr(row.ts_utc, "isoformat", None) else None,
        }
    except Exception:
        return None
    finally:
        db.close()


def list_unresolved_intents(
    *,
    session_factory: Optional[Callable] = None,
    market_id: Optional[str] = None,
    action: Optional[str] = None,
) -> list[dict]:
    """Newest unresolved intent for each client order id. Age does not drop a row."""
    factory = session_factory or get_session_local()
    db = factory()
    try:
        query = db.query(ActivityEvent).filter(
            ActivityEvent.event_type.in_(("order_intent", "order_ack")),
        )
        if market_id:
            query = query.filter(ActivityEvent.market_id == normalize_market_id(str(market_id)))
        if action:
            query = query.filter(ActivityEvent.action == str(action).strip().lower())
        from src.intent_lifecycle import select_intent_lifecycle

        selected = select_intent_lifecycle(query.all())
        out = []
        for row in selected["current"]:
            out.append(
                {
                    "client_order_id": (row.client_order_id or "").strip(),
                    "exchange_order_id": row.exchange_order_id,
                    "market_id": row.market_id,
                    "action": row.action,
                    "side": row.side,
                    "quantity": row.quantity,
                    "limit_price": row.limit_price,
                    "event_ticker": row.event_ticker,
                    "order_status": (row.order_status or "").strip().lower(),
                    "ts_utc": row.ts_utc,
                }
            )
        return out
    except Exception:
        return None
    finally:
        db.close()


def mark_intent_reconciled(
    client_order_id: str,
    *,
    market_id: str,
    action: str,
    order_status: str,
    session_factory: Optional[Callable] = None,
    detail: str = "",
) -> bool:
    """Record a terminal reconcile result for the same market and action."""
    return record_activity(
        {
            "event_type": "order_ack",
            "order_status": order_status,
            "market_id": market_id,
            "action": action,
            "client_order_id": client_order_id,
            "detail": detail,
        },
        session_factory=session_factory,
    )


def record_order_ack(
    result: Optional[dict],
    payload: Dict[str, Any],
    *,
    session_factory: Optional[Callable] = None,
) -> bool:
    body = dict(payload)
    body["event_type"] = "order_ack"
    body["order_status"] = classify_order_result(result)
    if isinstance(result, dict):
        if result.get("client_order_id"):
            body["client_order_id"] = result.get("client_order_id")
        exchange_id = result.get("order_id") or result.get("id")
        if exchange_id:
            body["exchange_order_id"] = exchange_id
        if "taker_fees_dollars" in result or "maker_fees_dollars" in result:
            from src.clients.kalshi_client import kalshi_order_fees_dollars

            body["fees_dollars"] = float(kalshi_order_fees_dollars(result))
            body["fee_kind"] = "exchange_fill"
        elif body.get("fee_kind") is None:
            body["fee_kind"] = "unavailable"
        fill = result.get("fill_count_fp")
        if fill is not None:
            try:
                body["fill_qty"] = float(fill)
            except (TypeError, ValueError):
                pass
        avg = result.get("average_fill_price")
        if avg is not None:
            try:
                body["fill_price"] = float(avg)
            except (TypeError, ValueError):
                pass
        if result.get("error"):
            body["error"] = scrub_text(result.get("error"), limit=200)
    return record_activity(body, session_factory=session_factory)


def intent_status_summary(*, session_factory: Optional[Callable] = None) -> Optional[dict]:
    """Current unresolved orders versus historical pending rows.

    None means the log could not be read. Historical pending rows are kept
    and are not counted as orders that are still open.
    """
    from src.intent_lifecycle import select_intent_lifecycle

    db = None
    try:
        factory = session_factory or get_session_local()
        db = factory()
        rows = (
            db.query(ActivityEvent)
            .filter(ActivityEvent.event_type.in_(("order_intent", "order_ack")))
            .all()
        )
        selected = select_intent_lifecycle(rows)
        return {
            "current_unresolved_intent_count": len(selected["current"]),
            "historical_pending_row_count": selected["historical_pending_rows"],
            "legacy_unresolved_without_client_order_id": len(selected["legacy_without_client_order_id"]),
            "current": selected["current"],
        }
    except Exception:
        return None
    finally:
        if db is not None:
            db.close()


def pending_intent_count(*, session_factory: Optional[Callable] = None) -> Optional[int]:
    """How many orders are still unconfirmed at their latest status.

    None means the log could not be read. This is not a count of every
    historical pending row.
    """
    summary = intent_status_summary(session_factory=session_factory)
    if summary is None:
        return None
    return int(summary["current_unresolved_intent_count"])


def classify_order_result(result: Optional[dict]) -> str:
    """Map a Kalshi client return value to a trace status. Timeouts stay ambiguous."""
    if not isinstance(result, dict):
        return "ambiguous"
    if result.get("order_not_sent"):
        return "not_sent"
    if result.get("skipped_dead_book"):
        return "skipped"
    err = result.get("error")
    if not err:
        return "acknowledged"
    text = str(err).lower()
    code = result.get("status_code")
    if code in (408, 504) or any(
        token in text for token in ("timeout", "timed out", "connect", "network", "reset by peer", "read error")
    ):
        return "ambiguous"
    if isinstance(code, int) or text.startswith("http "):
        return "rejected"
    return "ambiguous"
