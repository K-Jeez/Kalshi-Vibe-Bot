"""Approved sizing bundle, now used by the live order path.

Activated after Kevin's approval on 2026-11-09. The numbers below match
strategy_gates. Edge stays 5 points, win probability stays 60 percent,
and stop-loss stays 80 percent. Those three are not changed here.
"""

from src.decision_engine.strategy_gates import (
    EVENT_EXPOSURE_CAP_FRACTION,
    KELLY_FRACTION,
    MAX_KELLY_BANKROLL_PCT,
)

PROPOSAL_ACTIVE = True
POLICY_ID = "approved_bundle_2026_11_09_v1"
PROPOSED_KELLY = "quarter"
PROPOSED_ENTRY_CAP_OF_CASH = MAX_KELLY_BANKROLL_PCT
PROPOSED_EVENT_COST_BASIS_CAP_OF_EQUITY = EVENT_EXPOSURE_CAP_FRACTION
PROPOSED_MAX_OPEN_POSITIONS = 12
KELLY_MULTIPLIER = KELLY_FRACTION
