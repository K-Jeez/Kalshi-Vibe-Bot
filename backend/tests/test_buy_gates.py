"""Buy-side probability and the Settings floors."""

import unittest

from src.decision_engine.strategy_math import ai_win_prob_pct_on_buy_side, effective_buy_gate_thresholds


class TestBuyGates(unittest.TestCase):
    def test_ai_win_prob_on_buy_side(self):
        self.assertEqual(ai_win_prob_pct_on_buy_side("YES", 45), 45)
        self.assertEqual(ai_win_prob_pct_on_buy_side("NO", 45), 55)

    def test_effective_thresholds_use_the_settings(self):
        eff_e, eff_ai, tier = effective_buy_gate_thresholds(
            side="YES",
            ai_yes_pct=40,
            yes_ask=0.15,
            no_ask=0.86,
            yes_mid=0.15,
            no_mid=0.86,
            min_edge_base=4.0,
            min_ai_win_prob_base=70,
        )
        self.assertFalse(tier)
        self.assertEqual(eff_e, 4.0)
        self.assertEqual(eff_ai, 70)


if __name__ == "__main__":
    unittest.main()
