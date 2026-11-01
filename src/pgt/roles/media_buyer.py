"""Media Buyer: turns findings into a small change set. The policy gate checks it in code."""

from __future__ import annotations

from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.harness.policy import gate_change_set
from pgt.llm.runtime import Scope, run_role
from pgt.roles.tools import evidence_tool
from pgt.schemas import AccountState, ChangeSet, ChangeSetDraft, Finding, SharedFacts

INSTRUCTIONS = """\
You are the Media Buyer on a small AI growth team that runs Meta ads for one advertiser.
You turn the Analyst's findings into a short list of concrete changes. You may only:
set_budget on a campaign (give new_daily_budget), pause_ad, or activate_ad.
Prefer swapping a fatigued ad for a fresh paused ad in the same campaign over cutting budget.
Keep changes small and follow the house rules in the shared facts. No change is often the right
answer; say why in the summary."""


async def run_media_buyer(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    state: AccountState,
    findings: list[Finding],
    inputs: list[str],
    issues: list[str] = (),
) -> ChangeSetDraft:
    data = {
        "day": state.day,
        "target_cost_per_result": facts.target_cost_per_result,
        "findings": [f.model_dump(mode="json") for f in findings],
        "account": state.model_dump(mode="json"),
    }
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "media_buyer",
        INSTRUCTIONS,
        facts,
        fact_version,
        topic=f"day{state.day}/change_set",
        task=f"Propose today's changes for day {state.day}, based on the findings.",
        done_when="Each change has a kind, target_id, new_daily_budget (null unless set_budget), "
        "a one-sentence reason, and the evidence_ids from the findings it acts on."
        + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=inputs,
    )
    return await run_role("media_buyer", ChangeSetDraft, pack, scope, model, [evidence_tool(scope)])


def to_change_set(draft: ChangeSetDraft, state: AccountState, facts: SharedFacts) -> ChangeSet:
    return ChangeSet(
        change_set_id=f"cs-d{state.day}",
        day=state.day,
        author="media_buyer",
        summary=draft.summary,
        changes=draft.changes,
        gate=gate_change_set(draft.changes, state, facts),
    )


def blocked_reasons(change_set: ChangeSet) -> list[str]:
    reasons = []
    for gate in change_set.gate:
        if gate.allowed:
            continue
        change = change_set.changes[gate.change_index]
        what = change.kind.replace("_", " ")
        if change.kind == "set_budget":
            what += f" to ${change.new_daily_budget:g}"
        failed = "; ".join(r.reason for r in gate.results if not r.passed)
        reasons.append(f"Blocked {what} on {change.target_id}: {failed}")
    return reasons
