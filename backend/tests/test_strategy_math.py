"""Edge and stake helpers for binary contracts."""

import unittest

from src.decision_engine.strategy_math import edge_pct_for_side, stake_dollars_for_edge, whole_contracts_for_stake


class TestStrategyMath(unittest.TestCase):
    def test_edge_yes_favorable(self):
        e = edge_pct_for_side("YES", 70, 0.50, 0.50, 0.50, 0.50)
        self.assertAlmostEqual(e, 20.0, places=5)

    def test_stake_is_edge_percent_of_cash(self):
        self.assertAlmostEqual(stake_dollars_for_edge(100, 10), 10.0)
        self.assertEqual(whole_contracts_for_stake(10, 0.40, 100), 25)
        self.assertEqual(whole_contracts_for_stake(0.30, 0.40, 100), 0)


if __name__ == "__main__":
    unittest.main()
