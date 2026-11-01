"""Shared facts and house rules. Settings, not code."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Preset = Literal["conservative", "balanced", "aggressive", "custom"]


class HouseRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: Preset = "balanced"
    min_days_between_budget_changes: int = 3
    max_budget_change_pct: float = 0.20  # 0.20 means ±20%
    protect_learning_phase: bool = True
    min_spend_multiple_before_pause: float = 2.0  # × target cost per result
    daily_budget_cap: float = 60.0  # across all active campaigns
    check_time: str = "09:00"

    @classmethod
    def from_preset(cls, preset: Preset, **overrides) -> HouseRules:
        values = dict(PRESETS.get(preset, {}))
        values.update(overrides)
        return cls(preset=preset if not overrides else "custom", **values)


PRESETS: dict[str, dict] = {
    "conservative": {
        "min_days_between_budget_changes": 5,
        "max_budget_change_pct": 0.10,
        "protect_learning_phase": True,
        "min_spend_multiple_before_pause": 3.0,
    },
    "balanced": {
        "min_days_between_budget_changes": 3,
        "max_budget_change_pct": 0.20,
        "protect_learning_phase": True,
        "min_spend_multiple_before_pause": 2.0,
    },
    "aggressive": {
        "min_days_between_budget_changes": 2,
        "max_budget_change_pct": 0.30,
        "protect_learning_phase": False,
        "min_spend_multiple_before_pause": 1.5,
    },
}


class SharedFacts(BaseModel):
    """One source of truth for the team. Versioned by the fact store."""

    model_config = ConfigDict(extra="forbid")

    product: str
    audience: str
    goal: str  # e.g. "free-trial starts"
    result_name: str  # what a "result" is, e.g. "trial"
    target_cost_per_result: float
    weekly_budget: float
    test_budget_share: float = 0.2  # part of the weekly budget reserved for creative tests
    brand_voice: str
    house_rules: HouseRules = HouseRules()
    constraints: list[str] = []
    long_term_goal: str = ""  # e.g. "reach $12 cost per purchase by the end of the quarter"
