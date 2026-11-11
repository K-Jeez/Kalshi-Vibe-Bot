"""Review fixes: fill cashflows, blend gate, cooldown, residue, and caps."""

from __future__ import annotations

import csv
import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from src.bot.reanalysis import should_skip_unchanged_quote
from src.bot.exit_backoff import clear_exit_backoff, exit_is_backed_off, note_empty_exit, reset_for_tests
from src.config import Settings
from src.decision_engine.blend import quantity_after_blend
from src.decision_engine.strategy_gates import premodel_entry_block
from src.ops_status import alerts, clear_unresolved, note_unresolved, reset_for_tests as reset_ops
from src.reconcile.realized_cashflow import fill_yes_price, fills_to_cashflows
from src.shadow_buys import rotate_ids
from tools.repair_realized_pnl import plan_repairs, running_block_reason

FIXTURES = Path(__file__).parent / "fixtures"


def _closed_rows():
    with (FIXTURES / "positions_lifetime.csv").open(encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row["app_status"] == "closed"]


def test_mapped_fills_match_the_snapshot_totals_and_run3():
    fills = json.loads((FIXTURES / "fills_mapped.json").read_text(encoding="utf-8"))["fills"]
    positions = []
    for row in _closed_rows():
        positions.append(
            {
                "id": row["ticker"],
                "market_id": row["ticker"],
                "side": row["side"],
                "status": "closed",
                "quantity": float(row["contracts"]),
                "realized_pnl": float(row["app_realized_pnl"] or 0),
                "fees_paid": float(row["app_fees_paid"] or 0),
                "exit_reason": row["app_exit_reason"],
            }
        )
    settlements = [
        {"ticker": row["ticker"], "market_result": row["market_result"], "side": row["side"]}
        for row in _closed_rows()
    ]
    plans = plan_repairs(positions, fills, settlements)
    by_ticker = {plan["ticker"]: plan for plan in plans}
    run3 = by_ticker["KXATPGSPREAD-26NOV11KECRUN-RUN3"]
    assert abs(run3["new_realized_pnl"] - (-1.4557)) < 0.01
    lost = by_ticker["KXLALIGA1HTOTAL-26NOV08ATMBAR-2"]
    assert lost["side"] == "NO"
    assert abs(lost["new_realized_pnl"] - (-3.6925)) < 0.02
    assert lost["new_exit_reason"] == "settlement"
    totals = {"R1 Nov8": 0.0, "R2 Nov11": 0.0}
    rounds = {row["ticker"]: row["round"] for row in _closed_rows()}
    for plan in plans:
        totals[rounds[plan["ticker"]]] += plan["new_realized_pnl"]
    assert abs(totals["R1 Nov8"] - 1.77) < 0.05
    assert abs(totals["R2 Nov11"] - (-6.24)) < 0.05
    assert abs(sum(totals.values()) - (-4.48)) < 0.05


def test_api_field_names_are_not_read_as_zero():
    raw = {
        "ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count_fp": "2.00",
        "yes_price_dollars": "0.4000",
        "no_price_dollars": "0.6000",
        "fee_cost": "0.0400",
    }
    assert fill_yes_price(raw) == 0.4
    cash = fills_to_cashflows([raw], {"market_result": "yes", "side": "YES"}, held_side="YES")
    assert abs(cash["realized_pnl"] - (2.0 - 0.8 - 0.04)) < 1e-9


def test_two_positions_on_one_ticker_are_not_both_given_the_full_pnl():
    fills = [
        {"ticker": "KX", "action": "buy", "side": "yes", "count_fp": "4", "yes_price": "0.50", "fee": "0.08"},
    ]
    positions = [
        {"id": "a", "market_id": "KX", "side": "YES", "status": "closed", "quantity": 1, "realized_pnl": 0, "fees_paid": 0, "exit_reason": "settlement"},
        {"id": "b", "market_id": "KX", "side": "YES", "status": "closed", "quantity": 3, "realized_pnl": 0, "fees_paid": 0, "exit_reason": "settlement"},
    ]
    plans = plan_repairs(positions, fills, [{"ticker": "KX", "market_result": "yes"}])
    assert abs(sum(plan["new_realized_pnl"] for plan in plans) - (4 - 2 - 0.08)) < 1e-9
    assert plans[0]["new_realized_pnl"] != plans[1]["new_realized_pnl"]


def test_blend_gate_off_floors_at_one_and_on_can_skip():
    assert Settings().vibe_blend_ev_gate is False
    floor, tag = quantity_after_blend(
        blend_ev_gate=False,
        kelly_quantity=0,
        blended_ev=-0.05,
        cap_contracts=2,
        one_contract_fits_cash=True,
    )
    assert (floor, tag) == (1, "blend_floor")
    blocked, blocked_tag = quantity_after_blend(
        blend_ev_gate=True,
        kelly_quantity=3,
        blended_ev=-0.05,
        cap_contracts=2,
        one_contract_fits_cash=True,
    )
    assert (blocked, blocked_tag) == (0, "blend_ev")
    kept, kept_tag = quantity_after_blend(
        blend_ev_gate=False,
        kelly_quantity=4,
        blended_ev=-0.02,
        cap_contracts=2,
        one_contract_fits_cash=True,
    )
    assert (kept, kept_tag) == (2, "quarter_kelly")


def test_cooldown_skips_even_when_price_moved_and_a_quiet_market_stays_quiet():
    assert should_skip_unchanged_quote(
        analyzed_recently=True,
        previous_yes=0.40,
        previous_no=0.60,
        yes_price=0.55,
        no_price=0.45,
    )
    assert should_skip_unchanged_quote(
        analyzed_recently=False,
        previous_yes=0.40,
        previous_no=0.60,
        yes_price=0.404,
        no_price=0.596,
    )
    assert not should_skip_unchanged_quote(
        analyzed_recently=False,
        previous_yes=0.40,
        previous_no=0.60,
        yes_price=0.42,
        no_price=0.58,
    )


def test_cash_below_one_contract_skips_the_model():
    market = {
        "id": "KX",
        "series_ticker": "KX",
        "yes_ask": 0.80,
        "no_ask": 0.80,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    block = premodel_entry_block(market, 0.50)
    assert block is not None
    assert block["kind"] == "cash_short"
    assert "cannot buy one contract" in block["message"]


def test_unresolved_alert_clears_when_nothing_is_pending():
    reset_ops()
    note_unresolved("Unconfirmed buy KX stays reserved.")
    assert any("reserved" in line for line in alerts())
    clear_unresolved()
    assert not any("reserved" in line for line in alerts())


def test_shadow_rotation_does_not_starve_later_tickers():
    first, cursor = rotate_ids(["a", "b", "c", "d"], "", 2)
    assert first == ["a", "b"]
    second, _cursor = rotate_ids(["a", "b", "c", "d"], cursor, 2)
    assert second == ["c", "d"]


def test_exit_backoff_clears_when_asked():
    reset_for_tests()
    note_empty_exit("KX", now=0)
    assert exit_is_backed_off("KX", now=10)
    clear_exit_backoff("KX")
    assert not exit_is_backed_off("KX", now=10)


def test_fractional_residue_is_only_the_part_past_the_whole_contracts():
    filled = Decimal("5.01")
    whole = int(filled.to_integral_value(rounding=ROUND_DOWN))
    assert whole == 5
    assert filled - whole == Decimal("0.01")


def test_apply_is_refused_while_a_port_is_open():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        reason = running_block_reason(Path("/tmp/does-not-matter.db"), port=port)
    finally:
        sock.close()
    assert reason is not None
    assert "listening" in reason
