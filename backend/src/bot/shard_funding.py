"""Move only the cash a buy needs onto that market's exchange shard.

Get Balance (no ``exchange_index``) is the account total. ``balance_breakdown``
is the per-shard cash. Sells are not funded here; settlement cash stays on the
shard that paid it until a later buy needs it.
"""

from __future__ import annotations

import math
import uuid
from typing import Optional

from src.activity_log import record_activity
from src.decision_engine.strategy_gates import MAX_KELLY_BANKROLL_PCT

from src.logger import setup_logging

logger = setup_logging("bot.shard_funding")

# One or two cents above the estimated all-in cost, so a fee rounding step
# does not leave the shard short.
SHARD_TRANSFER_BUFFER_DOLLARS = 0.02


def _cents_up(dollars: float) -> int:
    return int(math.ceil(float(dollars) * 100.0 - 1e-9))


def _cents_down(dollars: float) -> int:
    return int(math.floor(float(dollars) * 100.0 + 1e-9))


def plan_shard_funding(
    *,
    shard_cash: dict,
    target_shard: int,
    order_all_in: float,
    total_cash: float,
    bankroll: float,
) -> dict:
    """Exact transfers for one buy, or a reason to skip without moving cash.

    The transfer total cannot exceed the 2.5% entry cap plus the two-cent
    buffer. Nothing is planned when the account total cannot cover the order.
    """
    target = int(target_shard)
    required_cents = _cents_up(float(order_all_in) + SHARD_TRANSFER_BUFFER_DOLLARS)
    on_shard = _cents_down(float(shard_cash.get(target, 0.0) or 0.0))
    if on_shard >= required_cents:
        return {"ok": True, "transfers": [], "required_cents": required_cents}
    shortfall = required_cents - on_shard
    cap_cents = _cents_up(float(bankroll) * float(MAX_KELLY_BANKROLL_PCT))
    buffer_cents = _cents_up(SHARD_TRANSFER_BUFFER_DOLLARS)
    if shortfall > cap_cents + buffer_cents:
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
    return {"ok": True, "transfers": transfers, "required_cents": required_cents, "shortfall_cents": shortfall}


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


async def fund_buy_shard(
    client,
    *,
    exchange_index: int,
    order_all_in: float,
    bankroll: float,
    ticker: str,
    session_factory=None,
) -> dict:
    """Fund shard ``exchange_index`` just enough for this buy, then stop.

    A failed or ambiguous transfer is not posted again. Balances are read
    once more instead.
    """
    target = int(exchange_index)
    try:
        try:
            portfolio = await client.get_portfolio(force_refresh=True)
        except TypeError:
            portfolio = await client.get_portfolio()
    except Exception:
        return {
            "ok": False,
            "reason": f"shard {target} funding failed: balance could not be read",
            "cooldown_minutes": 30,
        }
    if not isinstance(portfolio, dict):
        portfolio = {}
    shard_cash = portfolio.get("shard_cash")
    if not isinstance(shard_cash, dict):
        shard_cash = {}
    if not shard_cash:
        if target == 0:
            return {"ok": True, "transfers": [], "reason": ""}
        return {
            "ok": False,
            "reason": f"shard {target} funding failed: balance breakdown omitted",
            "cooldown_minutes": 30,
        }
    total_cash = float(portfolio.get("cash") or 0.0)
    plan = plan_shard_funding(
        shard_cash={int(k): float(v) for k, v in shard_cash.items()},
        target_shard=target,
        order_all_in=float(order_all_in),
        total_cash=total_cash,
        bankroll=float(bankroll),
    )
    if not plan.get("ok"):
        return plan
    if not plan.get("transfers"):
        return {"ok": True, "transfers": []}

    accepted = []
    for row in plan["transfers"]:
        client_transfer_id = str(uuid.uuid4())
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
        status = "accepted" if result.get("ok") and not result.get("ambiguous") else (
            "ambiguous" if result.get("ambiguous") or result.get("already_accepted") else "failed"
        )
        transfer_id = str(result.get("transfer_id") or "")
        _record_transfer(
            ticker,
            row,
            status=status,
            transfer_id=transfer_id,
            client_transfer_id=client_transfer_id,
            session_factory=session_factory,
        )
        if status == "accepted":
            logger.info(
                "Shard transfer %s -> %s amount=$%.2f for %s",
                row["source_shard"],
                row["destination_shard"],
                row["amount_dollars"],
                ticker,
            )
            accepted.append(row)
            continue
        # Do not post this leg again. Read the shards once.
        refreshed = await _reread(client)
        if _shard_has_cents(refreshed, target, int(plan["required_cents"])):
            logger.info(
                "Shard %s already covers the buy for %s after an unclear transfer",
                target,
                ticker,
            )
            return {"ok": True, "transfers": accepted}
        detail = str(result.get("error") or status)
        minutes = 2 if status == "ambiguous" else 30
        return {
            "ok": False,
            "transfers": accepted,
            "reason": f"shard {target} funding failed: {detail}",
            "cooldown_minutes": minutes,
        }

    refreshed = await _reread(client)
    if refreshed is None or _shard_has_cents(refreshed, target, int(plan["required_cents"])):
        # A missing re-read after a 200 is not proof of failure; the transfer
        # was accepted. Still require the balance when the read succeeds.
        if refreshed is None:
            return {
                "ok": False,
                "transfers": accepted,
                "reason": f"shard {target} funding failed: balance could not be re-read after the transfer",
                "cooldown_minutes": 2,
            }
        return {"ok": True, "transfers": accepted}
    return {
        "ok": False,
        "transfers": accepted,
        "reason": f"shard {target} funding failed: transfer accepted but the shard balance has not updated",
        "cooldown_minutes": 2,
    }


async def _reread(client) -> Optional[dict]:
    try:
        try:
            portfolio = await client.get_portfolio(force_refresh=True)
        except TypeError:
            portfolio = await client.get_portfolio()
    except Exception:
        return None
    return portfolio if isinstance(portfolio, dict) else None


def _shard_has_cents(portfolio: dict, target: int, required_cents: int) -> bool:
    shard_cash = portfolio.get("shard_cash")
    if not isinstance(shard_cash, dict) or not shard_cash:
        return target == 0 and _cents_down(float(portfolio.get("cash") or 0.0)) >= required_cents
    try:
        have = _cents_down(float(shard_cash.get(target, shard_cash.get(str(target), 0.0)) or 0.0))
    except (TypeError, ValueError):
        return False
    return have >= required_cents
