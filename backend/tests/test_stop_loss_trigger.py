import unittest

from src.reconcile.open_positions import (
    stop_loss_triggered_from_position,
    stop_loss_value_drawdown_triggered,
)
from src.database.models import Position
from src.util.datetimes import utc_now


class TestStopLossValueDrawdown(unittest.TestCase):
    def test_triggers_when_est_value_falls_enough(self):
        self.assertTrue(
            stop_loss_value_drawdown_triggered(
                cash_basis_total=10.0,
                quantity=10,
                estimated_price_per_contract=0.70,
                stop_loss_drawdown_pct=0.25,
            )
        )

    def test_no_trigger_below_threshold(self):
        self.assertFalse(
            stop_loss_value_drawdown_triggered(
                cash_basis_total=10.0,
                quantity=10,
                estimated_price_per_contract=0.80,
                stop_loss_drawdown_pct=0.25,
            )
        )

    def test_unknown_est_never_triggers(self):
        self.assertFalse(
            stop_loss_value_drawdown_triggered(
                cash_basis_total=10.0,
                quantity=10,
                estimated_price_per_contract=None,
                stop_loss_drawdown_pct=0.80,
            )
        )

    def test_no_baseline_never_triggers(self):
        self.assertFalse(
            stop_loss_value_drawdown_triggered(
                cash_basis_total=0.0,
                quantity=10,
                estimated_price_per_contract=0.5,
                stop_loss_drawdown_pct=0.25,
            )
        )

    def test_from_position_matches_basis_formula(self):
        p = Position(
            id="p1",
            market_id="KX",
            market_title="t",
            side="YES",
            quantity=10,
            entry_price=1.0,
            entry_cost=10.0,
            current_price=0.5,
            unrealized_pnl=0.0,
            status="open",
            trade_mode="paper",
            opened_at=utc_now(),
            fees_paid=0.0,
            estimated_price=0.70,
            bid_price=0.10,
        )
        self.assertTrue(stop_loss_triggered_from_position(p, stop_loss_drawdown_pct=0.25))


if __name__ == "__main__":
    unittest.main()
