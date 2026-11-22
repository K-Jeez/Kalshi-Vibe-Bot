from pydantic import BaseModel, model_validator


class BotStateRequest(BaseModel):
    state: str


class StopLossSellingRequest(BaseModel):
    enabled: bool


class StrategyKnobsRequest(BaseModel):
    """Minimum edge and/or model confidence on the bought side (at least one required)."""

    min_edge_to_buy_pct: float | int | None = None
    min_ai_win_prob_buy_side_pct: int | None = None

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.min_edge_to_buy_pct is None and self.min_ai_win_prob_buy_side_pct is None:
            raise ValueError("At least one of min_edge_to_buy_pct or min_ai_win_prob_buy_side_pct is required")
        return self
