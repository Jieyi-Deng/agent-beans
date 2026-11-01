"""Input budgets per role and per context section.

Must-keep sections (role instructions, team rules, charter, task) are never trimmed. If they
alone exceed MUST_KEEP, assembly raises BudgetConfigError before any call is sent.
May-trim sections (account memory, week stubs, lessons, owner notes, journal, inputs) each have
their own cap, so a long memory cannot push out today's inputs and the other way round.

Inputs start at the measured p95 of input tokens per call (two live runs on Oct 31, image calls
excluded) times 1.5, rounded up to the next hundred. Memory sections start at small fixed sizes.
`scale` multiplies every may-trim cap; the budget sweep runs 0.5x, 1x and 2x.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

# Input tokens per call, p95, from the two live runs on Oct 31.
MEASURED_P95 = {
    "analyst": 5_648,
    "media_buyer": 4_014,
    "growth_lead": 4_857,
    "producer": 7_460,
    "reviewer": 4_623,
    "strategist": 47_208,
}
# The Strategist's size is hosted search results, controlled with the search count and the search
# context size; its own inputs are the week's focus and summary.
STRATEGIST_INPUTS = 2_000
MEMORY_CAPS = {
    "account_memory": 1_500,
    "week_stubs": 400,
    "lessons": 600,
    "owner_notes": 400,
    "journal": 800,
}
MUST_KEEP = 4_000  # role instructions, team rules, charter and the task, together
TASK_ALLOWANCE = 600  # room left for the task when checking must-keep sections at startup
TRIM_FLAG_SHARE = 0.20  # a role trimmed in more than this share of calls is flagged


class BudgetConfigError(RuntimeError):
    """Must-keep sections exceed the must-keep budget: fix the configuration, not the call."""


def tokens(text: str) -> int:
    """About four bytes per token, the same estimate the offline stand-in reports."""
    return (len(text.encode("utf-8")) + 3) // 4


def start_budget(p95: int) -> int:
    return math.ceil(p95 * 1.5 / 100) * 100


def default_scale() -> float:
    try:
        return float(os.environ.get("PGT_BUDGET_SCALE", "1"))
    except ValueError:
        return 1.0


@dataclass(frozen=True)
class RoleBudget:
    role: str
    inputs: int
    memory: dict[str, int]
    must_keep: int = MUST_KEEP

    def cap(self, section: str) -> int | None:
        if section == "inputs":
            return self.inputs
        return self.memory.get(section)

    @property
    def total(self) -> int:
        return self.must_keep + self.inputs + sum(self.memory.values())


def budget_for(role: str, scale: float | None = None) -> RoleBudget:
    scale = default_scale() if scale is None else scale
    base = STRATEGIST_INPUTS if role == "strategist" else start_budget(
        MEASURED_P95.get(role, MEASURED_P95["analyst"]))
    return RoleBudget(
        role=role,
        inputs=max(1, int(base * scale)),
        memory={k: max(1, int(v * scale)) for k, v in MEMORY_CAPS.items()},
    )


def table(scale: float | None = None) -> list[dict]:
    """Every role's budget, for the Harness view and the docs."""
    rows = []
    for role, p95 in MEASURED_P95.items():
        b = budget_for(role, scale)
        rows.append({"role": role, "p95": p95, "inputs": b.inputs,
                     **b.memory, "must_keep": b.must_keep, "total": b.total})
    return rows


def check_must_keep(role: str, must_keep_tokens: int, budget: RoleBudget | None = None) -> None:
    budget = budget or budget_for(role)
    if must_keep_tokens > budget.must_keep:
        raise BudgetConfigError(
            f"{role}: the must-keep sections take {must_keep_tokens:,} tokens, more than the "
            f"{budget.must_keep:,}-token must-keep budget. Shorten the role instructions or the "
            "charter, or raise MUST_KEEP."
        )
