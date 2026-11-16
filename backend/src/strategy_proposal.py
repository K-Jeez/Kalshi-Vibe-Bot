"""Re-exports the live sizing constants. The numbers live in strategy_gates."""

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
KELLY_MULTIPLIER = KELLY_FRACTION
