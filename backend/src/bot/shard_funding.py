"""Move only the cash a buy needs onto that market's exchange shard.

Get Balance (no ``exchange_index``) is the account total. ``balance_breakdown``
is the per-shard cash. Sells are not funded here; settlement cash stays on the
shard that paid it until a later buy needs it.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Optional

from src.activity_log import record_activity
from src.decision_engine.strategy_gates import MAX_KELLY_BANKROLL_PCT

from src.logger import setup_logging

logger = setup_logging("bot.shard_funding")

# Two cents of fee rounding, taken inside the 2.5% cap. A transfer is never larger than the cap.
SHARD_TRANSFER_BUFFER_DOLLARS = 0.02
# Get Balance is 1 request/second with no burst.
BALANCE_READ_MIN_INTERVAL_SEC = 1.0
# After a 200, wait this long for balance_breakdown to show the cash.
BALANCE_POLL_READS = 3
PENDING_TTL_SEC = 120.0
TRANSFER_WINDOW_SEC = 600.0
# Total sent to one shard in that window, in entry-cap units.
TRANSFER_WINDOW_CAP_MULTIPLE = 3

# In-flight transfers whose cash may not be in balance_breakdown yet.
_pending: list[dict] = []
_posted: list[tuple[float, int, int]] = []
_last_read_mono = 0.0
_last_portfolio: Optional[dict] = None


def _cents_up(dollars: float) -> int:
    return int(math.ceil(float(dollars) * 100.0 - 1e-9))


def _cents_down(dollars: float) -> int:
    return int(math.floor(float(dollars) * 100.0 + 1e-9))


def reset_shard_funding_memory() -> None:
    """Drop in-flight transfer memory. Tests use this between cases."""
    global _last_read_mono, _last_portfolio
    _pending.clear()
    _posted.clear()
    _last_read_mono = 0.0
    _last_portfolio = None


def _cap_cents(bankroll: float, cap_fraction: Optional[float] = None) -> int:
    fraction = MAX_KELLY_BANKROLL_PCT if cap_fraction is None else float(cap_fraction)
    fraction = max(0.0, min(0.05, fraction))
    return max(0, _cents_down(float(bankroll) * fraction))


def transfer_client_id(*, client_order_id: str, source_shard: int, destination_shard: int, amount_cents: int) -> str:
    """Same id for the same intent, route, and amount. Kalshi returns 409 instead of a second transfer."""
    name = f"{client_order_id}|{int(source_shard)}|{int(destination_shard)}|{int(amount_cents)}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def plan_shard_funding(
    *,
    shard_cash: dict,
    target_shard: int,
    order_all_in: float,
    total_cash: float,
    bankroll: float,
    cap_fraction: Optional[float] = None,
) -> dict:
    """Exact transfers for one buy, or a reason to skip without moving cash.

    The two-cent buffer sits inside the 2.5% cap. A transfer larger than that
    cap is not planned. Nothing is planned when the account total cannot cover
    the order.
    """
    target = int(target_shard)
    cap_cents = _cap_cents(bankroll, cap_fraction)
    order_cents = _cents_up(float(order_all_in))
    if order_cents > cap_cents:
        return {
            "ok": False,
            "transfers": [],
            "reason": (
                f"shard {target} funding failed: shortfall ${order_cents / 100:.2f} "
                f"exceeds the 2.5% entry cap ${cap_cents / 100:.2f}"
            ),
            "cooldown_minutes": 30,
        }
    buffered = _cents_up(float(order_all_in) + SHARD_TRANSFER_BUFFER_DOLLARS)
    required_cents = min(buffered, cap_cents)
    on_shard = _cents_down(float(shard_cash.get(target, 0.0) or 0.0))
    if on_shard >= required_cents:
        return {"ok": True, "transfers": [], "required_cents": required_cents, "cap_cents": cap_cents}
    shortfall = required_cents - on_shard
    if shortfall > cap_cents:
        return {
            "ok": False,
            "transfers": [],
            "reason": (
                f"shard {target} funding failed: shortfall ${shortfall / 100:.2f} "
                f"exceeds the 2.5% entry cap ${cap_cents / 100:.2f}"
            ),
            "cooldown_minutes": 30,
        }
    if _cents_down(float(total_cash)) < required_cents:
        return {
            "ok": False,
            "transfers": [],
            "reason": (
                f"shard {target} funding failed: total cash ${float(total_cash):.2f} "
                f"is below ${required_cents / 100:.2f}"
            ),
            "cooldown_minutes": 30,
        }
    sources = []
    if 0 != target and _cents_down(float(shard_cash.get(0, 0.0) or 0.0)) > 0:
        sources.append(0)
    rest = [idx for idx in shard_cash if int(idx) not in (target, 0)]
    rest.sort(key=lambda idx: (-_cents_down(float(shard_cash.get(idx, 0.0) or 0.0)), int(idx)))
    sources.extend(int(idx) for idx in rest)
    transfers = []
    remaining = shortfall
    for source in sources:
        available = _cents_down(float(shard_cash.get(source, 0.0) or 0.0))
        take = min(available, remaining)
        if take < 1:
            continue
        transfers.append({
            "source_shard": int(source),
            "destination_shard": target,
            "amount_cents": int(take),
            "amount_dollars": take / 100.0,
        })
        remaining -= take
        if remaining < 1:
            break
    if remaining > 0:
        return {
            "ok": False,
            "transfers": [],
            "reason": (
                f"shard {target} funding failed: other shards cannot cover "
                f"${shortfall / 100:.2f}"
            ),
            "cooldown_minutes": 30,
        }
    moved = sum(row["amount_cents"] for row in transfers)
    if moved != shortfall:
        return {
            "ok": False,
            "transfers": [],
            "reason": f"shard {target} funding failed: transfer plan did not match the shortfall",
            "cooldown_minutes": 30,
        }
    return {
        "ok": True,
        "transfers": transfers,
        "required_cents": required_cents,
        "shortfall_cents": shortfall,
        "cap_cents": cap_cents,
    }


def _record_transfer(ticker: str, row: dict, *, status: str, transfer_id: str, client_transfer_id: str, session_factory) -> None:
    record_activity(
        {
            "event_type": "shard_transfer",
            "market_id": ticker,
            "action": "transfer",
            "limit_price": float(row["amount_dollars"]),
            "quantity": int(row["amount_cents"]),
            "client_order_id": client_transfer_id,
            "exchange_order_id": transfer_id or None,
            "order_status": status,
            "detail": (
                f"from shard {row['source_shard']} to shard {row['destination_shard']} "
                f"${float(row['amount_dollars']):.2f} for {ticker}; not P&L"
            ),
        },
        session_factory=session_factory,
    )


def _authoritative(portfolio: Optional[dict]) -> bool:
    if not isinstance(portfolio, dict):
        return False
    # Callers that are not Get Balance omit the flag. An explicit false is a stale or zero fallback.
    return portfolio.get("balance_authoritative") is not False


def _shard_map(portfolio: dict) -> dict[int, float]:
    raw = portfolio.get("shard_cash")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        try:
            out[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _visible_cents(portfolio: Optional[dict], target: int) -> int:
    if not isinstance(portfolio, dict):
        return 0
    return _cents_down(float(_shard_map(portfolio).get(target, 0.0) or 0.0))


def _expire_pending(shard_cash: dict[int, float], now: float) -> None:
    kept = []
    for row in _pending:
        if now - float(row["at"]) > PENDING_TTL_SEC:
            continue
        visible = _cents_down(float(shard_cash.get(int(row["dest"]), 0.0) or 0.0))
        if visible + 1 >= int(row["visible_dest_cents"]) + int(row["cents"]):
            continue
        kept.append(row)
    _pending[:] = kept


def _effective_cash(shard_cash: dict[int, float]) -> dict[int, float]:
    """Reported cash plus in-flight inbound, minus in-flight outbound."""
    cents = {idx: _cents_down(val) for idx, val in shard_cash.items()}
    for row in _pending:
        dest = int(row["dest"])
        source = int(row["source"])
        amount = int(row["cents"])
        cents[dest] = cents.get(dest, 0) + amount
        cents[source] = cents.get(source, 0) - amount
    return {idx: max(0, value) / 100.0 for idx, value in cents.items()}


def _window_cents(dest: int, now: float) -> int:
    return sum(amount for at, shard, amount in _posted if shard == dest and now - at <= TRANSFER_WINDOW_SEC)


def _remember(row: dict, *, visible_dest_cents: int, client_transfer_id: str) -> None:
    if any(item["client_transfer_id"] == client_transfer_id for item in _pending):
        return
    now = time.monotonic()
    _pending.append({
        "source": int(row["source_shard"]),
        "dest": int(row["destination_shard"]),
        "cents": int(row["amount_cents"]),
        "at": now,
        "visible_dest_cents": int(visible_dest_cents),
        "client_transfer_id": client_transfer_id,
    })
    _posted.append((now, int(row["destination_shard"]), int(row["amount_cents"])))


async def _read_balance(client, *, reuse: bool) -> Optional[dict]:
    """At most one Get Balance per second. A stale or zero fallback is not usable."""
    global _last_read_mono, _last_portfolio
    now = time.monotonic()
    if (
        reuse
        and isinstance(_last_portfolio, dict)
        and _authoritative(_last_portfolio)
        and now - _last_read_mono < BALANCE_READ_MIN_INTERVAL_SEC
    ):
        return _last_portfolio
    wait = BALANCE_READ_MIN_INTERVAL_SEC - (now - _last_read_mono)
    if _last_read_mono and wait > 0:
        await asyncio.sleep(wait)
    try:
        try:
            portfolio = await client.get_portfolio(force_refresh=True)
        except TypeError:
            portfolio = await client.get_portfolio()
    except Exception:
        return None
    _last_read_mono = time.monotonic()
    if not isinstance(portfolio, dict) or not _authoritative(portfolio):
        _last_portfolio = None
        return {"balance_authoritative": False}
    _last_portfolio = portfolio
    return portfolio


async def _poll_for_balance(client, target: int, required_cents: int) -> Optional[dict]:
    latest = None
    for _ in range(BALANCE_POLL_READS):
        latest = await _read_balance(client, reuse=False)
        if not _authoritative(latest):
            return latest
        if _visible_cents(latest, target) >= required_cents:
            return latest
    return latest


async def fund_buy_shard(
    client,
    *,
    exchange_index: Optional[int],
    order_all_in: float,
    bankroll: float,
    ticker: str,
    client_order_id: str = "",
    session_factory=None,
    cap_fraction: Optional[float] = None,
) -> dict:
    """Fund one known shard just enough for this buy.

    A 200 from Kalshi means the transfer was accepted, not that
    ``balance_breakdown`` already shows it. That amount stays reserved on the
    destination until a later read confirms it or the TTL ends, so the next
    ticker on the same shard does not send it again.
    """
    if exchange_index is None:
        return {"ok": True, "transfers": [], "reason": ""}
    target = int(exchange_index)
    portfolio = await _read_balance(client, reuse=True)
    if not _authoritative(portfolio):
        return {
            "ok": False,
            "transfers": [],
            "reason": f"shard {target} funding failed: balance read was not authoritative",
            "cooldown_minutes": 2,
        }
    shard_cash = _shard_map(portfolio)
    if not shard_cash:
        if target == 0:
            return {"ok": True, "transfers": []}
        return {
            "ok": False,
            "reason": f"shard {target} funding failed: balance breakdown omitted",
            "cooldown_minutes": 30,
        }
    now = time.monotonic()
    _expire_pending(shard_cash, now)
    effective = _effective_cash(shard_cash)
    plan = plan_shard_funding(
        shard_cash=effective,
        target_shard=target,
        order_all_in=float(order_all_in),
        total_cash=float(portfolio.get("cash") or 0.0),
        bankroll=float(bankroll),
        cap_fraction=cap_fraction,
    )
    if not plan.get("ok"):
        return plan
    if not plan.get("transfers"):
        if _visible_cents(portfolio, target) >= int(plan["required_cents"]):
            return {"ok": True, "transfers": []}
        return {
            "ok": False,
            "transfers": [],
            "reason": f"shard {target} funding failed: a transfer to this shard is still unconfirmed",
            "cooldown_minutes": 2,
        }

    cap_cents = int(plan.get("cap_cents") or _cap_cents(bankroll))
    window_limit = cap_cents * TRANSFER_WINDOW_CAP_MULTIPLE
    shortfall = sum(int(row["amount_cents"]) for row in plan["transfers"])
    if _window_cents(target, now) + shortfall > window_limit:
        return {
            "ok": False,
            "transfers": [],
            "reason": (
                f"shard {target} funding failed: transfers to this shard in the last "
                f"10 minutes would exceed {TRANSFER_WINDOW_CAP_MULTIPLE} entry caps"
            ),
            "cooldown_minutes": 10,
        }
    if any(int(row["dest"]) == target for row in _pending):
        return {
            "ok": False,
            "transfers": [],
            "reason": (
                f"shard {target} funding failed: a transfer to this shard is still unconfirmed"
            ),
            "cooldown_minutes": 2,
        }

    identity = (client_order_id or ticker or "buy").strip()
    accepted = []
    visible_before = _visible_cents(portfolio, target)
    for row in plan["transfers"]:
        client_transfer_id = transfer_client_id(
            client_order_id=identity,
            source_shard=row["source_shard"],
            destination_shard=row["destination_shard"],
            amount_cents=row["amount_cents"],
        )
        try:
            result = await client.transfer_event_contract_shards(
                source_shard=row["source_shard"],
                destination_shard=row["destination_shard"],
                amount_dollars=row["amount_dollars"],
                client_transfer_id=client_transfer_id,
            )
        except Exception as exc:
            result = {"ok": False, "ambiguous": True, "error": type(exc).__name__}
        if not isinstance(result, dict):
            result = {"ok": False, "ambiguous": True, "error": "transfer result was not readable"}
        accepted_or_replay = bool(result.get("ok") or result.get("already_accepted") or result.get("ambiguous"))
        status = "accepted" if result.get("ok") and not result.get("ambiguous") else (
            "ambiguous" if accepted_or_replay else "failed"
        )
        _record_transfer(
            ticker,
            row,
            status=status,
            transfer_id=str(result.get("transfer_id") or ""),
            client_transfer_id=client_transfer_id,
            session_factory=session_factory,
        )
        if not accepted_or_replay:
            return {
                "ok": False,
                "transfers": accepted,
                "reason": f"shard {target} funding failed: {result.get('error') or status}",
                "cooldown_minutes": 30,
            }
        _remember(row, visible_dest_cents=visible_before, client_transfer_id=client_transfer_id)
        if status == "accepted":
            logger.info(
                "Shard transfer %s -> %s amount=$%.2f for %s",
                row["source_shard"],
                row["destination_shard"],
                row["amount_dollars"],
                ticker,
            )
        accepted.append(row)
        visible_before += int(row["amount_cents"])

    refreshed = await _poll_for_balance(client, target, int(plan["required_cents"]))
    if _authoritative(refreshed):
        _expire_pending(_shard_map(refreshed), time.monotonic())
        if _visible_cents(refreshed, target) >= int(plan["required_cents"]):
            return {"ok": True, "transfers": accepted}
    return {
        "ok": False,
        "transfers": accepted,
        "reason": f"shard {target} funding failed: transfer accepted but the shard balance has not updated",
        "cooldown_minutes": 2,
    }
