"""Deterministic tool permissions, house rules and model spending limits."""

from __future__ import annotations

import math
from decimal import Decimal

from pgt.harness.events import EventLog
from pgt.schemas import AccountState, Change, GateResult, RuleResult, SharedFacts

ALLOWLIST: dict[str, set[str]] = {
    "analyst": {"get_account_state", "get_insights", "get_evidence", "recall_week",
                "recall_journal", "propose_fact"},
    "media_buyer": {"get_account_state", "get_insights", "get_evidence", "recall_week",
                    "recall_journal"},
    "reviewer": {"get_evidence"},
    "strategist": {"web_search"},
    "growth_lead": {"get_account_state", "get_evidence", "recall_week", "recall_journal",
                    "propose_fact"},
    "producer": {"ask_analyst", "recall_week", "recall_journal"},
}


def check_tool(
    role: str, tool: str, events: EventLog, loop_id: str | None = None,
) -> str | None:
    if tool in ALLOWLIST.get(role, set()):
        return None
    events.append("tool.blocked", role, {"role": role, "tool": tool}, loop_id=loop_id)
    return f"Role {role} is not allowed to call {tool}."


class _Rule:
    name: str
    kinds: frozenset[str]

    def result(self, passed: bool, reason: str) -> RuleResult:
        return RuleResult(rule=self.name, passed=passed, reason=reason)


class TargetExists(_Rule):
    name = "target_exists"
    kinds = frozenset({"set_budget", "pause_ad", "activate_ad"})

    def check(self, change: Change, state: AccountState, facts: SharedFacts) -> RuleResult:
        if change.kind == "set_budget":
            if state.campaign(change.target_id) is None:
                return self.result(False, f"Campaign {change.target_id} does not exist.")
            new = change.new_daily_budget
            if new is None or not math.isfinite(new) or new <= 0:
                return self.result(False, f"Campaign {change.target_id} needs a positive budget.")
        elif state.ad(change.target_id) is None:
            return self.result(False, f"Ad {change.target_id} does not exist.")
        return self.result(True, f"Target {change.target_id} is valid.")


class MinDaysBetweenBudgetChanges(_Rule):
    name = "min_days_between_budget_changes"
    kinds = frozenset({"set_budget"})

    def check(self, change: Change, state: AccountState, facts: SharedFacts) -> RuleResult:
        last = state.campaign(change.target_id).last_budget_change_day
        minimum = facts.house_rules.min_days_between_budget_changes
        if last is None:
            return self.result(True, "No previous budget change.")
        days = state.day - last
        return self.result(days >= minimum, f"{days} days since last change; minimum {minimum} days.")


class MaxBudgetChange(_Rule):
    name = "max_budget_change"
    kinds = frozenset({"set_budget"})

    def check(self, change: Change, state: AccountState, facts: SharedFacts) -> RuleResult:
        old = state.campaign(change.target_id).daily_budget
        limit = facts.house_rules.max_budget_change_pct
        if old <= 0 or not math.isfinite(old):
            return self.result(False, f"Cannot measure budget change from ${old:g}; limit {limit:.0%}.")
        delta = change.new_daily_budget / old - 1
        return self.result(
            abs(delta) <= limit + 1e-9, f"Budget change {delta:+.0%}; limit {limit:.0%}.",
        )


class LearningPhaseProtection(_Rule):
    name = "learning_phase_protection"
    kinds = frozenset({"set_budget"})

    def check(self, change: Change, state: AccountState, facts: SharedFacts) -> RuleResult:
        blocked = facts.house_rules.protect_learning_phase and state.campaign(
            change.target_id
        ).in_learning
        return self.result(
            not blocked, "Campaign is protected during learning." if blocked
            else "Learning phase protection allows this change.",
        )


class MinSpendBeforePause(_Rule):
    name = "min_spend_before_pause"
    kinds = frozenset({"pause_ad"})

    def check(self, change: Change, state: AccountState, facts: SharedFacts) -> RuleResult:
        spend = state.ad(change.target_id).lifetime_spend
        threshold = facts.house_rules.min_spend_multiple_before_pause * facts.target_cost_per_result
        return self.result(
            spend >= threshold, f"Ad spend ${spend:g}; minimum before pausing ${threshold:g}.",
        )


class DailyBudgetCap(_Rule):
    name = "daily_budget_cap"
    kinds = frozenset({"set_budget"})

    def check(self, change: Change, state: AccountState, facts: SharedFacts) -> RuleResult:
        total = math.fsum(
            change.new_daily_budget if campaign.campaign_id == change.target_id
            else campaign.daily_budget
            for campaign in state.campaigns if campaign.status == "ACTIVE"
        )
        cap = facts.house_rules.daily_budget_cap
        return self.result(total <= cap, f"New active daily budget total ${total:g}; cap ${cap:g}.")


RULES = [
    TargetExists(),
    MinDaysBetweenBudgetChanges(),
    MaxBudgetChange(),
    LearningPhaseProtection(),
    MinSpendBeforePause(),
    DailyBudgetCap(),
]


def gate_change_set(
    changes: list[Change], state: AccountState, facts: SharedFacts,
) -> list[GateResult]:
    projected = state.model_copy(deep=True)
    gates = []
    for index, change in enumerate(changes):
        results = []
        for rule in RULES:
            if change.kind not in rule.kinds:
                continue
            result = rule.check(change, projected, facts)
            results.append(result)
            if rule.name == "target_exists" and not result.passed:
                break
        allowed = all(result.passed for result in results)
        gates.append(GateResult(change_index=index, allowed=allowed, results=results))
        if allowed:
            if change.kind == "set_budget":
                campaign = projected.campaign(change.target_id)
                campaign.daily_budget = change.new_daily_budget
                campaign.last_budget_change_day = projected.day
            else:
                projected.ad(change.target_id).status = (
                    "PAUSED" if change.kind == "pause_ad" else "ACTIVE"
                )
    return gates


class SpendCapExceeded(Exception):
    """A recorded model call exceeded at least one configured cap."""


class CostMeter:
    """Per-call, per-role and per-loop caps, plus an optional `total` across the whole workspace.
    `record` runs after a call and raises once a cap is passed; `check` runs before a paid call
    and refuses it once the total is used up, so a live run cannot keep spending."""

    def __init__(
        self, events: EventLog, per_call: float = 0.50,
        per_role: float = 1.00, per_loop: float = 3.00, total: float | None = None,
    ) -> None:
        self._events = events
        self.per_call, self.per_role, self.per_loop = per_call, per_role, per_loop
        self.total = total

    def check(self, role: str, loop_id: str) -> None:
        if self.total is None:
            return
        spent = self.spent()
        if spent >= self.total:
            self._events.append(
                "budget.exceeded", "system",
                {"cap": "total", "caps": ["total"], "role": role, "cost": 0.0,
                 "spent": round(spent, 4), "limit": self.total},
                loop_id=loop_id,
            )
            raise SpendCapExceeded(
                f"The ${self.total:g} spend cap for this workspace is used up (${spent:.2f})"
            )

    def remaining(self) -> float | None:
        return None if self.total is None else max(self.total - self.spent(), 0.0)

    def record(self, role: str, loop_id: str, cost: float, **details) -> None:
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("Model cost must be finite and nonnegative")
        exceeded = []
        with self._events._transaction():
            self._events.append(
                "model.cost", role, {"role": role, "cost": cost, "details": details},
                loop_id=loop_id,
            )
            totals = [
                ("per_call", cost, self.per_call),
                ("per_role", self.spent(role, loop_id), self.per_role),
                ("per_loop", self.spent(loop_id=loop_id), self.per_loop),
            ]
            if self.total is not None:
                totals.append(("total", self.spent(), self.total))
            exceeded = [name for name, total, limit in totals if total > limit]
            if exceeded:
                self._events.append(
                    "budget.exceeded", "system",
                    {"cap": exceeded[0], "caps": exceeded, "role": role, "cost": cost},
                    loop_id=loop_id,
                )
        if exceeded:
            raise SpendCapExceeded(f"Model spend exceeded {', '.join(exceeded)} for {role}/{loop_id}")

    def spent(self, role: str | None = None, loop_id: str | None = None) -> float:
        costs = [Decimal(str(event.payload["cost"]))
                 for event in self._events.list("model.cost", loop_id=loop_id)
                 if role is None or event.payload["role"] == role]
        return float(sum(costs, Decimal(0)))
