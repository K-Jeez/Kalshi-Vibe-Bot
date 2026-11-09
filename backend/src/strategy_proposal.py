"""Proposed sizing rules. They are not used by the live order path.

Kevin has not approved activating these. Live code still uses full Kelly
and the existing 5% cash cap in strategy_gates.MAX_KELLY_BANKROLL_PCT.
"""

PROPOSAL_ACTIVE = False

PROPOSED_KELLY = "quarter"
PROPOSED_ENTRY_CAP_OF_CASH = 0.025
PROPOSED_EVENT_COST_BASIS_CAP_OF_EQUITY = 0.05
PROPOSED_MAX_OPEN_POSITIONS = 12
