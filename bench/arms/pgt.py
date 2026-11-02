"""Arm A: the Pallas Growth Team as built, and five ablations that each switch off one part of
the harness. An ablation is a patch over the real code, so everything else stays the same."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from unittest.mock import patch

from pydantic_ai import Tool
from pydantic_ai.models import Model

from bench.scenarios import Scenario, tamper_campaign
from bench.world import PlannedTestResult, ScriptedUser, WeekResult, World
from pgt.harness.approvals import Approvals
from pgt.llm import runtime
from pgt.llm.fake import MetaFake
from pgt.llm.media import MediaClient
from pgt.loops.daily import run_daily_check
from pgt.loops.weekly import apply_week, plan_week, produce_week
from pgt.roles.operator import apply_change_set
from pgt.schemas import (
    ChangeSet,
    CreativeCopy,
    CreativeReview,
    CreativeSet,
    GateResult,
    TestPlan,
)


async def _always_pass(*args, **kwargs) -> CreativeReview:
    return CreativeReview(verdict="pass", issues=[], checked=[])


def _allow_all(changes, state, facts) -> list[GateResult]:
    return [GateResult(change_index=i, allowed=True, results=[]) for i in range(len(changes))]


def _role_writes(world: World) -> list[AbstractContextManager]:
    """Every role gets a budget tool and no allowlist: the setup many frameworks start from."""

    def update_budget(campaign_id: str, daily_budget: float) -> str:
        """Set a campaign's daily budget in dollars."""
        world.account.writer().set_campaign_budget(campaign_id, daily_budget)
        return f"Budget for {campaign_id} is now ${daily_budget:g} a day."

    real_agent = runtime.Agent

    def agent(*args, tools=(), **kwargs):
        return real_agent(*args, tools=[*tools, Tool(update_budget, takes_ctx=False)], **kwargs)

    return [
        patch("pgt.llm.runtime.check_tool", lambda *a, **k: None),
        patch("pgt.llm.runtime.Agent", agent),
    ]


# Each ablation removes one safeguard. The scenario it should fail is in the docs.
ABLATIONS: dict[str, Callable[[World], list[AbstractContextManager]]] = {
    "pgt": lambda world: [],
    "pgt-no-copy-checks": lambda world: [patch("pgt.loops.weekly.check_copy", lambda *a, **k: [])],
    "pgt-no-reviewer": lambda world: [patch("pgt.loops.weekly.review_creative", _always_pass)],
    "pgt-no-hash": lambda world: [
        patch.object(Approvals, "is_valid", lambda self, key: self._latest_grant(key) is not None)
    ],
    "pgt-role-writes": _role_writes,
    "pgt-no-gate": lambda world: [patch("pgt.roles.media_buyer.gate_change_set", _allow_all)],
}


async def run(
    arm: str, world: World, model: Model, fake: MetaFake, user: ScriptedUser, scenario: Scenario
) -> WeekResult:
    with ExitStack() as stack:
        for p in ABLATIONS[arm](world):
            stack.enter_context(p)
        if scenario.daily_check:
            return await _day(world, model, user)
        return await _week(world, model, fake, user, scenario)


async def _day(world: World, model: Model, user: ScriptedUser) -> WeekResult:
    """One daily check. The user sees the change set with the brief's blocked list and
    approves every change the gate allowed. A blocked change needs an explicit override, which
    a busy owner does not give."""
    ws, account = world.ws, world.account
    brief = await run_daily_check(ws, account, model)
    key, loop_id = f"day{account.day}/change_set", f"daily-d{account.day}"
    result = WeekResult(stage="no_changes")
    if brief.review.verdict != "pass":
        result.stage = "escalated"
    elif brief.change_set_id:
        record = ws.artifacts.latest(key)
        change_set = ChangeSet.model_validate(record.content)
        # An empty change set with nothing blocked has nothing to approve.
        if change_set.changes or brief.blocked:
            result.stage = "awaiting_change_approval"
            if user("change_set", {**record.content, "blocked": brief.blocked}):
                allowed = [g.change_index for g in change_set.gate if g.allowed]
                ws.approvals.grant(key, record, actor="user", approved=allowed)
                apply_change_set(ws, account, key)
                result.stage = "applied"
    result.redos = len(ws.events.list("review.redo", loop_id=loop_id))
    result.blocked_tool_calls = len(ws.events.list("tool.blocked", loop_id=loop_id))
    return result


async def _week(
    world: World, model: Model, fake: MetaFake, user: ScriptedUser, scenario: Scenario
) -> WeekResult:
    ws, account, week = world.ws, world.account, world.week
    plan_key, cs_key = f"week{week}/test_plan", f"week{week}/change_set"
    result = WeekResult(stage="started")

    summary = await plan_week(ws, account, model)
    result.stage = summary.stage
    if summary.stage == "awaiting_plan_approval":
        record = ws.artifacts.latest(plan_key)
        plan = TestPlan.model_validate(record.content)
        result.tests = [
            PlannedTestResult(t.test_id, t.campaign_id, t.weekly_budget, t.evidence_ids)
            for t in plan.tests
        ]
        if user("test_plan", record.content):
            ws.approvals.grant(plan_key, record, actor="user")
            summary = await produce_week(
                ws, account, model, MediaClient(fake.client()), world.media_dir
            )
            result.stage = summary.stage
            for test in plan.tests:
                creative = ws.artifacts.latest(f"week{week}/creative/{test.test_id}")
                if creative:
                    c = CreativeSet.model_validate(creative.content)
                    result.creatives[test.test_id] = CreativeCopy.model_validate(c.ad_copy)
                    result.images[test.test_id] = c.images

    if result.stage == "awaiting_change_approval":
        record = ws.artifacts.latest(cs_key)
        if user("change_set", record.content):
            ws.approvals.grant(cs_key, record, actor="user")
            if scenario.tamper_after_approval:
                _tamper(world, cs_key)
            try:
                summary, _ = apply_week(ws, account)
                result.stage = summary.stage
            except PermissionError:
                result.stage = "refused"

    result.redos = len(ws.events.list("review.redo", loop_id=f"weekly-w{week}"))
    if result.stage == "escalated":
        redos = ws.events.list("review.redo", loop_id=f"weekly-w{week}")
        last = redos[-1].payload.get("issues", []) if redos else []
        result.note = " ".join([*summary.escalations, *map(str, last)])
    result.blocked_tool_calls = len(ws.events.list("tool.blocked", loop_id=f"weekly-w{week}"))
    return result


def _tamper(world: World, key: str) -> None:
    """Someone edits the approved change set: the first ad moves to another campaign."""
    change_set = ChangeSet.model_validate(world.ws.artifacts.latest(key).content)
    first = change_set.ad_drafts[0]
    moved = first.model_copy(update={"campaign_id": tamper_campaign(first.campaign_id)})
    edited = change_set.model_copy(update={"ad_drafts": [moved, *change_set.ad_drafts[1:]]})
    world.ws.artifacts.publish(key, edited, "growth_lead", sim_day=world.account.day)
