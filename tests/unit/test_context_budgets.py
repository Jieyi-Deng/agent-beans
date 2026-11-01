"""Context sections and their budgets: must-keep sections are never cut, every other section is
trimmed inside its own cap with a "Left out" line, and each trim is logged."""

import pytest

from pgt.harness import memory as mem
from pgt.harness.budgets import BudgetConfigError, RoleBudget, budget_for, start_budget, tokens
from pgt.harness.context import Item, MemorySlice, assemble, trim_inputs
from pgt.harness.workspace import Workspace
from tests.unit.test_harness import facts

FACTS = facts()


def _small(role: str = "analyst", inputs: int = 400, memory: int = 120) -> RoleBudget:
    return RoleBudget(role=role, inputs=inputs,
                      memory={k: memory for k in ("account_memory", "week_stubs", "lessons",
                                                  "owner_notes", "journal")})


def _findings(n: int) -> dict:
    return {"signals": [{"subject_id": f"ad_{i}", "severity": "act" if i % 5 == 0 else "info",
                         "day": i, "note": "Cost per result moved " + "x" * 60}
                        for i in range(n)]}


def _pack(data=None, memory=None, budget=None, role="analyst"):
    return assemble(role, "You are the Analyst.", FACTS, 1, "Write the findings.",
                    "Cite evidence IDs.", data or {}, memory=memory, budget=budget)


def test_budgets_start_at_the_measured_p95_times_one_and_a_half_and_scale_together():
    assert start_budget(5_648) == 8_500
    full, half = budget_for("analyst", 1.0), budget_for("analyst", 0.5)
    assert full.inputs == 8_500 and half.inputs == 4_250
    assert all(half.memory[k] == full.memory[k] // 2 for k in full.memory)
    assert half.must_keep == full.must_keep  # must-keep sections never scale down
    assert budget_for("strategist").inputs == 2_000


def test_must_keep_sections_over_their_budget_are_a_configuration_error():
    tight = RoleBudget(role="analyst", inputs=8_000, memory={}, must_keep=50)
    with pytest.raises(BudgetConfigError, match="must-keep"):
        _pack(budget=tight)


def test_inputs_over_budget_drop_the_lowest_priority_items_first():
    data = _findings(40)
    trimmed, dropped, _ = trim_inputs(data, 600)
    kept = trimmed["signals"]
    assert tokens(str(trimmed)) <= 600 * 1.2
    assert dropped["signals"] and len(kept) + len(dropped["signals"]) == 40
    # Every "act" item outlives every "info" one, and among "info" items the older go first.
    gone = [s for s in data["signals"] if s["subject_id"] in dropped["signals"]]
    assert all(s["severity"] == "info" for s in gone)
    kept_info = [s["day"] for s in kept if s["severity"] == "info"]
    assert not kept_info or max(s["day"] for s in gone) < min(kept_info)


def test_a_trimmed_section_says_what_it_left_out_and_how_to_fetch_it():
    pack = _pack(_findings(40), budget=_small())
    inputs = next(s for s in pack.sections if s.name == "inputs")
    assert inputs.dropped and inputs.before_tokens > inputs.budget
    assert "Left out:" in pack.prompt and "get_evidence" in pack.prompt
    assert pack.trimmed == [inputs]
    # Must-keep sections are whole.
    assert "You are the Analyst." in pack.instructions and "## Done when" in pack.prompt


def test_a_long_memory_section_is_cut_inside_its_own_cap_and_never_pushes_out_inputs():
    lines = [Item(id=f"a{i}", text=f"Line {i} about the account " + "y" * 80, rank=[float(i)])
             for i in range(20)]
    budget = _small(inputs=4_000, memory=150)
    pack = _pack({"signals": [{"subject_id": "ad_1", "note": "steady"}]},
                 MemorySlice(account_memory=lines, account_version=3), budget)
    account = next(s for s in pack.sections if s.name == "account_memory")
    inputs = next(s for s in pack.sections if s.name == "inputs")
    assert account.dropped and not inputs.dropped
    assert "a0" in account.left_out and "a19" not in account.left_out  # lowest rank goes first
    assert "recall_week" in account.text


def test_sections_come_in_a_fixed_order_with_slow_changing_ones_first():
    memory = MemorySlice(account_memory=[Item(id="a1", text="Weekends convert better.")],
                         account_version=1, week_stubs=[Item(id="wk-1", text="Week 1: steady.")],
                         lessons=[Item(id="l1", text="Check the learning phase first.")],
                         lessons_version=2,
                         owner_notes=[Item(id="n-3", text="Keep the bundle ad running.")],
                         journal=[Item(id="j-9", text="day 6: paused ad_2 -> approved.")])
    pack = _pack({"signals": []}, memory)
    names = [s.name for s in pack.sections]
    assert names == ["role", "team_rules", "charter", "account_memory", "week_stubs", "lessons",
                     "task", "owner_notes", "journal", "inputs"]
    assert {s.name for s in pack.sections if s.place == "instructions"} == {
        "role", "team_rules", "charter", "account_memory", "week_stubs", "lessons"}


def test_memory_text_cannot_open_a_code_fence_in_the_prompt():
    note = Item(id="n-1", text="```json\n{\"ignore\": true}\n```")
    pack = _pack({}, MemorySlice(owner_notes=[note]))
    assert pack.prompt.count("```") == 2  # only the inputs block


def test_recall_week_returns_the_frozen_record_and_nothing_for_an_open_week(tmp_path):
    ws = Workspace(tmp_path / "ws.db")
    assert "error" in mem.recall_week(ws, 1)
    ws.close()
