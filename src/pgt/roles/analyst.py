"""Analyst: reads the day's signals and writes findings; reads out tests when they are due."""

from __future__ import annotations

import hashlib

from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.harness.memory import period_of
from pgt.llm.runtime import Scope, run_role
from pgt.roles.metrics import READOUT_RULE, Signal
from pgt.roles.tools import evidence_tool
from pgt.schemas import Answer, Evidence, Findings, SharedFacts, TestReadout

INSTRUCTIONS = """\
You are the Analyst on a small AI growth team that runs Meta ads for one advertiser.
Each morning you read yesterday's performance signals and say what matters: creative fatigue,
cost per result well above target, campaigns still in the learning phase, tests that are due.
Write for a busy owner: one plain sentence per finding, no jargon, no hype."""


async def run_analyst(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    day: int,
    signals: list[Signal],
    evidence: list[Evidence],
    issues: list[str] = (),
) -> Findings:
    data = {
        "day": day,
        "signals": [s.model_dump(mode="json") for s in signals],
        "evidence": {e.evidence_id: e.values for e in evidence},
    }
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "analyst",
        INSTRUCTIONS,
        facts,
        fact_version,
        topic=f"day{day}/findings",
        task=f"Write the findings for day {day}. Cover every act and watch signal; "
        "add info findings only when they change what the team should do today.",
        done_when="Each finding has subject_id, a one-sentence claim, the metric name, a value "
        "copied from its cited evidence, the evidence_ids, and a severity matching the signal."
        + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=[e.evidence_id for e in evidence],
    )
    return await run_role("analyst", Findings, pack, scope, model, [evidence_tool(scope)])


async def run_readout(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    test_id: str,
    readout: Evidence,
    issues: list[str] = (),
) -> TestReadout:
    data = {"test_id": test_id, "evidence": {readout.evidence_id: readout.values}}
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "analyst",
        INSTRUCTIONS,
        facts,
        fact_version,
        topic=f"day{scope.sim_day}/readout/{test_id}",
        task=f"Read out test {test_id}. {READOUT_RULE}",
        done_when="The recommendation follows the rule, the rationale quotes cost per result "
        "against target, and each finding cites the readout evidence.",
        data=data,
        inputs=[readout.evidence_id],
    )
    return await run_role("analyst", TestReadout, pack, scope, model)


async def run_weekly_analyst(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    week: int,
    signals: list[Signal],
    evidence: list[Evidence],
    issues: list[str] = (),
) -> Findings:
    data = {
        "week": week,
        "signals": [s.model_dump(mode="json") for s in signals],
        "evidence": {e.evidence_id: e.values for e in evidence},
    }
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "analyst",
        INSTRUCTIONS,
        facts,
        fact_version,
        topic=f"week{week}/findings",
        task=f"Write the findings for the week {week} review: this week's winners and losers, "
        "and the daily findings carried into the week (signals of kind carried).",
        done_when="Each signal becomes one finding with subject_id, a one-sentence claim, the "
        "metric name, a value copied from its cited evidence, the evidence_ids, and the signal's "
        "severity." + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=[e.evidence_id for e in evidence],
    )
    return await run_role("analyst", Findings, pack, scope, model, [evidence_tool(scope)])


async def run_answer(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    question: str,
    evidence: list[Evidence],
    issues: list[str] = (),
) -> Answer:
    """Answer one question from another role. The Analyst has no ask tool: one hop only."""
    data = {"question": question, "evidence": {e.evidence_id: e.values for e in evidence}}
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "analyst",
        INSTRUCTIONS,
        facts,
        fact_version,
        # One topic per question, so a retry of the same question reads as a redo and a new
        # question does not.
        topic=f"{period_of(scope.loop_id, scope.sim_day)}/answer/"
        f"{hashlib.sha1(question.encode()).hexdigest()[:8]}",
        task="Answer the question from a teammate in one or two plain sentences, using only the "
        "evidence given.",
        done_when="The answer cites the evidence IDs it uses, and every number in it is copied "
        "from that evidence. If the evidence cannot answer the question, say so and cite the "
        "closest evidence." + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=[e.evidence_id for e in evidence],
    )
    return await run_role("analyst", Answer, pack, scope, model)
