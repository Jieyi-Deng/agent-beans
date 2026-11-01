"""Growth Lead: frames the week (Intake) and turns reviewed findings and research into a test
plan (Plan). Budget dollars are set in code from the plan's weights."""

from __future__ import annotations

from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.llm.runtime import Scope, run_role
from pgt.schemas import Finding, SharedFacts, TestPlanDraft, WeeklyBrief

INSTRUCTIONS = """\
You are the Growth Lead on a small AI growth team that runs Meta ads for one advertiser.
You set the week's focus and decide which creative tests to run. You do not write ads yourself.
Plan small, honest tests: each one answers a question the data raised, and each one has a clear
hypothesis, metric and stop rule. Write for a busy owner: plain words, no hype."""


def missing_facts(facts: SharedFacts) -> list[str]:
    """Facts the weekly review cannot start without. Checked in code before any model call."""
    missing = [name for name in ("product", "audience", "goal") if not getattr(facts, name).strip()]
    if facts.target_cost_per_result <= 0:
        missing.append("target_cost_per_result")
    if facts.weekly_budget <= 0:
        missing.append("weekly_budget")
    return missing


async def run_intake(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    week: int,
    headlines: list[str],
    findings: list[Finding],
) -> WeeklyBrief:
    pack = assemble(
        "growth_lead",
        INSTRUCTIONS,
        facts,
        fact_version,
        topic=f"week{week}/brief",
        task=f"Start the weekly review for week {week}. Summarize last week from the daily "
        "headlines and findings, and name what the team should look into this week.",
        done_when="The summary is two or three plain sentences. Focus lists at most three items. "
        "questions_for_user lists only questions whose answers would change the plan; "
        "leave it empty otherwise.",
        data={
            "week": week,
            "daily_headlines": headlines,
            "findings": [f.model_dump(mode="json") for f in findings],
        },
        inputs=sorted({i for f in findings for i in f.evidence_ids}),
    )
    return await run_role("growth_lead", WeeklyBrief, pack, scope, model)


async def run_plan(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    week: int,
    brief: WeeklyBrief,
    findings: list[Finding],
    insights: dict[str, dict],
    account: dict,
    available_test_budget: float,
    issues: list[str] = (),
    products: list[str] = ("phone",),
) -> TestPlanDraft:
    data = {
        "week": week,
        "weekly_brief": brief.model_dump(mode="json"),
        "reviewed_findings": [f.model_dump(mode="json") for f in findings],
        "insights": insights,
        "account": account,
        "available_test_budget": available_test_budget,
        "products_on_file": list(products),
    }
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "growth_lead",
        INSTRUCTIONS,
        facts,
        fact_version,
        topic=f"week{week}/test_plan",
        task=f"Plan two or three creative tests for week {week}. Each test responds to one or "
        "more reviewed findings and draws on at least one market insight. Put each test in an "
        "active campaign. If a test is meant to replace a tired ad, set replaces_ad_id. Each ad "
        "shows exactly one product photo, so set brief.product to one name from "
        "products_on_file and write the angle and key message around that product alone. Mark "
        "exactly one test exploratory: it tries a new angle or audience instead of fixing a "
        "finding, so the plan never shrinks to fixes alone.",
        done_when="Every test cites evidence IDs from reviewed_findings and insight IDs from "
        "insights, exactly as given. budget_weight is 1-5 (code turns weights into dollars). "
        "Test names are unique. Exactly one test has exploratory true. The brief's hook is a "
        "short opening line with no made-up numbers."
        + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=sorted({i for f in findings for i in f.evidence_ids}),
    )
    return await run_role("growth_lead", TestPlanDraft, pack, scope, model)
