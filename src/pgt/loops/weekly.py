"""The weekly growth review, in three stages split by the user's two approvals:

1. plan_week: findings -> Intake -> Analyst and Strategist in parallel -> test plan.
   Stops for approval 1 (the test plan).
2. produce_week: one Creative Producer per test (ask/answer with the Analyst, Muse Image,
   SAM 3.1, compositor) -> code checks -> model review, with up to two redos -> change set
   of new ads. Stops for approval 2 (the change set).
3. apply_week: the Operator creates the approved ads, PAUSED, in the sandbox.

Each stage refuses to run without a valid approval of the stage before it.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

from PIL import Image
from pydantic_ai.models import Model

from pgt.creative.compose import (
    Box,
    cutout,
    make_demo_product_shot,
    matte,
    png_bytes,
    save_png,
)
from pgt.creative.design import Style, compose
from pgt.creative.video import Cut, contact_sheet, encode
from pgt.harness.creative_checks import (
    allocate_budget,
    check_captions,
    check_copy,
    check_design,
    check_images,
    check_plan,
    check_research,
    check_video,
    extract_numbers,
)
from pgt.harness.policy import CostMeter, SpendCapExceeded
from pgt.harness.protocol import ask_tool, escalate, handoff
from pgt.harness.workspace import Workspace
from pgt.llm.media import MediaClient
from pgt.llm.runtime import Scope
from pgt.roles.analyst import run_answer, run_weekly_analyst
from pgt.roles.growth_lead import missing_facts, run_intake, run_plan
from pgt.roles.metrics import Signal, week_days, weekly_signals
from pgt.roles.operator import apply_change_set
from pgt.roles.producer import run_producer, shot_prompt
from pgt.roles.reviewer import review_creative, review_findings
from pgt.roles.strategist import run_research
from pgt.sandbox import SandboxAccount, product_names, product_photo
from pgt.sandbox.scenario import CreativeKit
from pgt.schemas import (
    CREATIVE_SIZES,
    AdDraft,
    ChangeSet,
    CreativeCopy,
    CreativeImage,
    CreativeReview,
    CreativeSet,
    CreativeVideo,
    ExecutionReceipt,
    Finding,
    Findings,
    MarketResearch,
    PlanTest,
    SharedFacts,
    TestPlan,
    WeeklyBrief,
    WeeklyReviewSummary,
)

MAX_ATTEMPTS = 3  # first draft plus two redos
PLAN_FACT_KEYS = ["weekly_budget", "test_budget_share", "target_cost_per_result"]


def current_week(account: SandboxAccount) -> int:
    return max(1, math.ceil(account.day / 7))


def _scope(ws: Workspace, account: SandboxAccount, week: int, meter: CostMeter | None) -> Scope:
    return Scope(ws, meter or CostMeter(ws.events), f"weekly-w{week}", account.day)


def _summary(scope: Scope, week: int, stage: str, **fields) -> WeeklyReviewSummary:
    escalations = [
        e.payload["reason"]
        for e in scope.ws.events.list("escalation.raised", loop_id=scope.loop_id)
    ]
    summary = WeeklyReviewSummary(
        week=week,
        stage=stage,
        plan_id=fields.get("plan_id"),
        creative_ids=fields.get("creative_ids", []),
        change_set_id=fields.get("change_set_id"),
        escalations=escalations,
    )
    scope.ws.artifacts.publish(f"week{week}/summary", summary, "growth_lead", sim_day=scope.sim_day)
    scope.ws.events.append(
        "loop.stage",
        "growth_lead",
        {"loop": "weekly_review", "week": week, "stage": stage},
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )
    return summary


# --- Stage 1: plan ---------------------------------------------------------------------------


def carried_signals(ws: Workspace, week: int) -> tuple[list[Signal], list[str]]:
    """Daily findings that passed review this week, as signals for the weekly Analyst, plus the
    daily headlines for the Growth Lead's Intake."""
    signals: list[Signal] = []
    headlines: list[str] = []
    readout_keys = sorted({r.key for r in ws.artifacts.list() if "/readout/" in r.key})
    for day in week_days(week):
        brief = ws.artifacts.latest(f"day{day}/brief")
        if brief:
            headlines.append(brief.content["headline"])
        review = ws.artifacts.latest(f"day{day}/review")
        if review is None or review.content["verdict"] != "pass":
            continue
        found: list[dict] = []
        record = ws.artifacts.latest(f"day{day}/findings")
        if record:
            found += [f for f in record.content["findings"] if f["severity"] == "act"]
        for key in (k for k in readout_keys if k.startswith(f"day{day}/readout/")):
            found += ws.artifacts.latest(key).content["findings"]
        for f in found:
            signals.append(
                Signal(
                    subject_id=f["subject_id"],
                    kind="carried",
                    severity=f["severity"],
                    metric=f["metric"],
                    value=f["value"],
                    evidence_ids=f["evidence_ids"],
                    note=f["claim"],
                )
            )
    return signals, headlines


def _as_findings(signals: list[Signal]) -> list[Finding]:
    return [
        Finding(
            subject_id=s.subject_id,
            claim=s.note,
            metric=s.metric,
            value=s.value,
            evidence_ids=s.evidence_ids,
            severity=s.severity,
        )
        for s in signals
    ]


async def plan_week(
    ws: Workspace,
    account: SandboxAccount,
    model: Model,
    week: int | None = None,
    meter: CostMeter | None = None,
) -> WeeklyReviewSummary:
    from pgt.loops.close import close_open_weeks
    from pgt.loops.notes import triage_notes

    week = week or current_week(account)
    meter = meter or CostMeter(ws.events)
    # Weeks that ended without a closed record (the owner never approved their change set)
    # are closed now, so this review starts from up-to-date memory.
    await close_open_weeks(ws, model, week, meter)
    await triage_notes(ws, model, meter, account.day)
    scope = _scope(ws, account, week, meter)
    log = ws.events.append
    facts, fv = ws.facts.current(), ws.facts.version
    log(
        "loop.started",
        "growth_lead",
        {"loop": "weekly_review", "week": week},
        sim_day=account.day,
        loop_id=scope.loop_id,
    )
    missing = missing_facts(facts)
    if missing:
        escalate(scope, "growth_lead", "Shared facts are missing: " + ", ".join(missing) + ".")
        return _summary(scope, week, "escalated")

    state = account.state()
    week_signals, evidence = weekly_signals(week, state, account.insights(), facts)
    for item in evidence:
        ws.ledger.add(item)
    carried, headlines = carried_signals(ws, week)
    signals = week_signals + carried

    # Intake from what the daily checks already reviewed; then Analyst and Strategist in parallel.
    handoff(scope, "system", "growth_lead", f"Start the week {week} review.")
    brief = await run_intake(scope, model, facts, fv, week, headlines, _as_findings(carried))
    brief_rec = ws.artifacts.publish(
        f"week{week}/brief", brief, "growth_lead", fact_version=fv, sim_day=account.day
    )
    handoff(scope, "growth_lead", "analyst", "Weekly findings", [brief_rec.artifact_id])
    handoff(scope, "growth_lead", "strategist", "Market research", [brief_rec.artifact_id])
    findings, research_run = await asyncio.gather(
        _weekly_findings(scope, model, facts, fv, week, signals, evidence),
        run_research(scope, model, facts, fv, week, brief),
    )
    findings_rec = ws.artifacts.publish(
        f"week{week}/findings",
        findings,
        "analyst",
        fact_version=fv,
        fact_keys=["target_cost_per_result"],
        sim_day=account.day,
        status="in_review",
    )
    if review_findings(findings.findings, ws.ledger):
        escalate(
            scope,
            "reviewer",
            "The weekly findings failed review twice.",
            [findings_rec.artifact_id],
        )
        return _summary(scope, week, "escalated")

    research = research_run.output
    issues = check_research(research)
    if issues:
        log(
            "review.redo",
            "reviewer",
            {"subject": "research", "by": "code", "issues": issues},
            sim_day=account.day,
            loop_id=scope.loop_id,
        )
        research_run = await run_research(scope, model, facts, fv, week, brief, issues)
        research = research_run.output
        issues = check_research(research)
    research_rec = ws.artifacts.publish(
        f"week{week}/research",
        research,
        "strategist",
        fact_version=fv,
        sim_day=account.day,
        status="in_review",
    )
    if issues:
        escalate(
            scope, "reviewer", "Market research failed review twice.", [research_rec.artifact_id]
        )
        return _summary(scope, week, "escalated")
    return await _draft_plan(
        scope, model, state, week, brief, brief_rec, findings, findings_rec, research, research_rec,
        products=products_on_file(account),
    )


def products_on_file(account) -> list[str]:
    """The products the team has a photo of; each planned ad shows one of them."""
    return product_names(getattr(account, "scenario", None))


def shot_file(concept: str, main: str) -> str:
    """Where a week's product photo is saved: the main product keeps the original name."""
    return "product_shot.png" if concept == main else f"product_shot-{concept.replace(' ', '-')}.png"


def owner_issue(note: str) -> str:
    return f"From the owner, who sent the last version back: {note}"


async def revise_plan(
    ws: Workspace,
    account: SandboxAccount,
    model: Model,
    note: str,
    week: int | None = None,
    meter: CostMeter | None = None,
) -> WeeklyReviewSummary:
    """The owner sent the test plan back: redraft it from the same reviewed brief, findings and
    research, with the owner's note as the first issue to fix, and ask again."""
    week = week or current_week(account)
    scope = _scope(ws, account, week, meter)
    records = {k: ws.artifacts.latest(f"week{week}/{k}") for k in ("brief", "findings", "research")}
    if any(r is None for r in records.values()):
        raise ValueError(f"Week {week} has no reviewed brief, findings and research to redo from")
    handoff(scope, "system", "growth_lead", f"Redo the test plan with the owner's note: {note}",
            [records["brief"].artifact_id])
    return await _draft_plan(
        scope, model, account.state(), week,
        WeeklyBrief.model_validate(records["brief"].content), records["brief"],
        Findings.model_validate(records["findings"].content), records["findings"],
        MarketResearch.model_validate(records["research"].content), records["research"],
        owner=[owner_issue(note)], products=products_on_file(account),
    )


async def _draft_plan(
    scope: Scope, model: Model, state, week: int, brief: WeeklyBrief, brief_rec,
    findings: Findings, findings_rec, research: MarketResearch, research_rec,
    owner: list[str] = (), products: list[str] = ("phone",),
) -> WeeklyReviewSummary:
    """Growth Lead drafts the test plan, code checks it (one redo), then it waits on the owner.
    `owner` holds the owner's note when they sent the last version back."""
    ws, log, day = scope.ws, scope.ws.events.append, scope.sim_day
    facts, fv = ws.facts.current(), ws.facts.version
    insights = {
        f"ins-w{week}-{i + 1}": insight.model_dump(mode="json")
        for i, insight in enumerate(research.insights)
    }
    available = round(facts.weekly_budget * facts.test_budget_share, 2)
    account_view = {
        "campaigns": [c.model_dump(mode="json") for c in state.campaigns],
        "ads": [a.model_dump(mode="json") for a in state.ads],
    }
    plan_kwargs = {
        "reviewed_evidence_ids": {i for f in findings.findings for i in f.evidence_ids},
        "insight_ids": set(insights),
        "active_campaign_ids": {c.campaign_id for c in state.campaigns if c.status == "ACTIVE"},
        "ad_ids": {a.ad_id for a in state.ads},
        "products": set(products),
    }

    async def draft_with(issues: list[str]):
        return await run_plan(scope, model, facts, fv, week, brief, findings.findings, insights,
                              account_view, available, issues, products)

    draft = await draft_with(list(owner))
    issues = check_plan(draft, **plan_kwargs)
    if issues:
        log(
            "review.redo",
            "reviewer",
            {"subject": "test_plan", "by": "code", "issues": issues},
            sim_day=day,
            loop_id=scope.loop_id,
        )
        draft = await draft_with([*owner, *issues])
        issues = check_plan(draft, **plan_kwargs)
    if issues:
        escalate(scope, "reviewer", "The test plan failed review twice: " + " ".join(issues))
        return _summary(scope, week, "escalated")

    budgets = allocate_budget([t.budget_weight for t in draft.tests], available)
    plan = TestPlan(
        plan_id=f"week{week}/test_plan",
        week=week,
        available_test_budget=available,
        summary=draft.summary,
        tests=[
            PlanTest(**t.model_dump(), test_id=f"w{week}-t{i + 1}", weekly_budget=budget)
            for i, (t, budget) in enumerate(zip(draft.tests, budgets, strict=True))
        ],
    )
    plan_rec = ws.artifacts.publish(
        plan.plan_id,
        plan,
        "growth_lead",
        inputs=[brief_rec.artifact_id, findings_rec.artifact_id, research_rec.artifact_id],
        fact_version=fv,
        fact_keys=PLAN_FACT_KEYS,
        sim_day=day,
        status="in_review",
    )
    log(
        "approval.requested",
        "growth_lead",
        {"key": plan.plan_id, "what": "test plan"},
        refs=[plan_rec.artifact_id],
        sim_day=day,
        loop_id=scope.loop_id,
    )
    return _summary(scope, week, "awaiting_plan_approval", plan_id=plan.plan_id)


async def _weekly_findings(scope, model, facts, fv, week, signals, evidence) -> Findings:
    findings = await run_weekly_analyst(scope, model, facts, fv, week, signals, evidence)
    issues = review_findings(findings.findings, scope.ws.ledger)
    if issues:
        scope.ws.events.append(
            "review.redo",
            "reviewer",
            {"subject": "weekly_findings", "by": "code", "issues": issues},
            sim_day=scope.sim_day,
            loop_id=scope.loop_id,
        )
        findings = await run_weekly_analyst(
            scope, model, facts, fv, week, signals, evidence, issues
        )
    return findings


# --- Stage 2: produce ------------------------------------------------------------------------


def fact_numbers(facts: SharedFacts, kit: CreativeKit | None = None) -> list[float]:
    """Numbers ad copy may use without citing evidence: those stated in the facts' text and in
    the advertiser's creative kit (offer, product cards), plus the target and budget.
    House-rule numbers are left out on purpose."""
    parts = [facts.product, facts.audience, facts.goal, facts.brand_voice, *facts.constraints]
    if kit is not None:
        parts += [kit.offer, *(p for card in kit.products for p in card.selling_points)]
    return [*extract_numbers(" ".join(parts)), facts.target_cost_per_result, facts.weekly_budget]


def creative_kit(account) -> CreativeKit | None:
    scenario = getattr(account, "scenario", None)
    return getattr(scenario, "creative", None)


def _require_approval(ws: Workspace, key: str, stage: str, sim_day: int) -> None:
    if not ws.approvals.is_valid(key):
        ws.events.append(
            "stage.refused",
            "system",
            {"stage": stage, "key": key, "reason": "no valid approval"},
            sim_day=sim_day,
        )
        raise PermissionError(f"{key} has no valid approval for its current content.")


async def produce_week(
    ws: Workspace,
    account: SandboxAccount,
    model: Model,
    media: MediaClient,
    media_dir: str | Path,
    week: int | None = None,
    product_shot: Image.Image | None = None,
    meter: CostMeter | None = None,
    note: str | None = None,
) -> WeeklyReviewSummary:
    """`note` is set when the owner sent the new ads back: every Producer starts again with it
    as the first issue to fix, and attempts carry on from the last version."""
    week = week or current_week(account)
    plan_key = f"week{week}/test_plan"
    _require_approval(ws, plan_key, "produce", account.day)
    scope = _scope(ws, account, week, meter)
    facts, fv = ws.facts.current(), ws.facts.version
    plan_rec = ws.artifacts.latest(plan_key)
    plan = TestPlan.model_validate(plan_rec.content)
    research = MarketResearch.model_validate(ws.artifacts.latest(f"week{week}/research").content)
    insights = {
        f"ins-w{week}-{i + 1}": insight.model_dump(mode="json")
        for i, insight in enumerate(research.insights)
    }
    known_urls = {s.url for i in research.insights for s in i.sources}
    findings = Findings.model_validate(ws.artifacts.latest(f"week{week}/findings").content)
    evidence_ids = sorted({i for f in findings.findings for i in f.evidence_ids})
    evidence = [ws.ledger.get(i) for i in evidence_ids if ws.ledger.has(i)]

    # Each product a new ad shows is segmented once with SAM 3.1 and shared by every test that
    # shows it. Nothing is segmented when every ad is reused as it was.
    media_dir = Path(media_dir) / f"week{week}"
    scenario = getattr(account, "scenario", None)
    names = products_on_file(account)

    def shown(test: PlanTest) -> str:
        return test.brief.product if test.brief.product in names else names[0]

    cutouts: dict[str, Image.Image] = {}
    references: dict[str, Image.Image] = {}
    fresh = [t for t in plan.tests if note or _reusable(ws, week, t.test_id) is None]
    for concept in dict.fromkeys(shown(t) for t in fresh):
        shot = product_shot if concept == names[0] else None
        if shot is None and scenario is not None:
            shot = product_photo(scenario, concept)
        if shot is None:
            shot, _ = make_demo_product_shot()
        save_png(shot, media_dir / shot_file(concept, names[0]))
        references[concept] = shot.convert("RGB")
        try:
            boxes = await media.segment(scope, "producer", shot, concept)
        except ValueError:
            escalate(scope, "producer", f"SAM 3.1 found no {concept} in the product photo.")
            return _summary(scope, week, "escalated", plan_id=plan.plan_id)
        try:
            cutouts[concept] = matte(shot, boxes[0])
        except ValueError:
            # Busy photo: fall back to the box itself with rounded corners.
            cutouts[concept] = cutout(shot, boxes[0])
    photos: dict[tuple[str, str], tuple[Image.Image, Box | None]] = {}
    kit = creative_kit(account)
    allowed_numbers = fact_numbers(facts, kit)

    async def answer_for(question: str, issues: list[str]):
        return await run_answer(scope, model, facts, fv, question, evidence, issues)

    async def produce(test: PlanTest) -> CreativeSet | None:
        reused = None if note else _reusable(ws, week, test.test_id)
        if reused is not None:
            ws.events.append(
                "creative.reused", "growth_lead",
                {"test_id": test.test_id, "creative_id": reused.creative_id},
                refs=[ws.artifacts.latest(f"week{week}/creative/{test.test_id}").artifact_id],
                sim_day=account.day, loop_id=scope.loop_id,
            )
            return reused
        task = f"Creative for {test.test_id}"
        if note:
            task = f"Redo the creative for {test.test_id} with the owner's note: {note}"
        handoff(scope, "growth_lead", "producer", task, [plan_rec.artifact_id])
        ask = ask_tool(scope, "producer", "analyst", test.test_id, answer_for)
        issues: list[str] = [owner_issue(note)] if note else []
        last = ws.artifacts.latest(f"week{week}/creative/{test.test_id}")
        first = last.content["attempt"] + 1 if last else 1
        for attempt in range(first, first + MAX_ATTEMPTS):
            ad_copy = await run_producer(
                scope, model, facts, fv, test, insights, ask, issues, kit
            )
            creative_id = f"{test.test_id}-c{attempt}"
            concept = shown(test)
            images, video, sheet, laid_out, captions = await _render(
                scope, media, ad_copy, concept, references[concept], cutouts[concept], photos,
                media_dir / creative_id, kit,
            )
            creative = CreativeSet(
                creative_id=creative_id,
                test_id=test.test_id,
                attempt=attempt,
                ad_copy=ad_copy,
                images=images,
                video=video,
                question_ids=_question_ids(scope, test.test_id),
            )
            creative_rec = ws.artifacts.publish(
                f"week{week}/creative/{test.test_id}",
                creative,
                "producer",
                inputs=[plan_rec.artifact_id],
                fact_version=fv,
                sim_day=account.day,
                status="in_review",
            )
            issues = check_copy(ad_copy, ws.ledger, allowed_numbers, known_urls,
                                kit.cta_buttons if kit else None)
            issues += check_images(images, variants=len(ad_copy.scenes) or 1)
            issues += check_video(video)
            issues += check_design(concept, laid_out)
            issues += check_captions(concept, captions)
            by = "code"
            checked: list[str] = []
            if not issues:
                try:
                    review = await review_creative(
                        scope, model, facts, fv, test, ad_copy, sheet, kit
                    )
                except SpendCapExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001 - a failed review is not a verdict
                    _review_failed(scope, week, test.test_id, creative_id, creative_rec, exc)
                    return None
                issues = [] if review.verdict == "pass" else review.issues or ["Redo requested."]
                checked = review.checked
                by = "reviewer"
            ws.artifacts.publish(
                f"week{week}/review/{test.test_id}",
                CreativeReview(verdict="redo" if issues else "pass", issues=issues,
                               checked=checked),
                "reviewer",
                inputs=[creative_rec.artifact_id],
                sim_day=account.day,
            )
            if not issues:
                return creative
            ws.events.append(
                "review.redo",
                "reviewer",
                {"subject": creative_id, "by": by, "attempt": attempt, "issues": issues},
                refs=[creative_rec.artifact_id],
                sim_day=account.day,
                loop_id=scope.loop_id,
            )
        escalate(
            scope, "reviewer", f"Creative for {test.test_id} failed review {MAX_ATTEMPTS} times."
        )
        return None

    results = await asyncio.gather(*(produce(t) for t in plan.tests))
    passed = [(t, c) for t, c in zip(plan.tests, results, strict=True) if c is not None]
    if not passed:
        return _summary(scope, week, "escalated", plan_id=plan.plan_id)

    change_set = ChangeSet(
        change_set_id=f"cs-w{week}",
        day=account.day,
        author="growth_lead",
        summary=(f"{len(passed)} new {'ad' if len(passed) == 1 else 'ads'}, created paused, "
                 f"for the Week {week} tests."),
        changes=[],
        gate=[],
        ad_drafts=[
            AdDraft(
                campaign_id=t.campaign_id,
                name=f"{t.test_id} {c.ad_copy.name}",
                creative_id=c.creative_id,
                test_id=t.test_id,
                evidence_ids=t.evidence_ids,
            )
            for t, c in passed
        ],
    )
    creative_ids = [c.creative_id for _, c in passed]
    cs_rec = ws.artifacts.publish(
        f"week{week}/change_set",
        change_set,
        "growth_lead",
        inputs=[
            plan_rec.artifact_id,
            *(
                ws.artifacts.latest(f"week{week}/creative/{t.test_id}").artifact_id
                for t, _ in passed
            ),
        ],
        fact_version=fv,
        sim_day=account.day,
        status="in_review",
    )
    ws.events.append(
        "approval.requested",
        "growth_lead",
        {"key": f"week{week}/change_set", "what": "new ads"},
        refs=[cs_rec.artifact_id],
        sim_day=account.day,
        loop_id=scope.loop_id,
    )
    return _summary(
        scope,
        week,
        "awaiting_change_approval",
        plan_id=plan.plan_id,
        creative_ids=creative_ids,
        change_set_id=change_set.change_set_id,
    )


def _review_failed(scope: Scope, week: int, test_id: str, creative_id: str, creative_rec,
                   exc: Exception) -> None:
    """The Reviewer's call failed (timeout, provider or schema): the owner hears that the review
    could not run. The creative is not sent back and the Producer's work is not judged."""
    failures = [e.payload["failure"] for e in scope.ws.events.list("model.call",
                                                                  loop_id=scope.loop_id)
                if e.payload.get("role") == "reviewer" and e.payload.get("failure")]
    failure = failures[-1] if failures else "provider"
    scope.ws.events.append(
        "review.failed", "reviewer",
        {"topic": f"week{week}/creative/{test_id}", "test_id": test_id,
         "creative_id": creative_id, "failure": failure,
         "error": f"{type(exc).__name__}: {exc}"[:300]},
        refs=[creative_rec.artifact_id], sim_day=scope.sim_day, loop_id=scope.loop_id)
    escalate(scope, "reviewer",
             f"Review could not run for {test_id} ({failure}). The creative was not sent back; "
             "run the week again or review it yourself.", [creative_rec.artifact_id])


def _reusable(ws: Workspace, week: int, test_id: str) -> CreativeSet | None:
    """The last creative for this test, if it passed review and nothing it used has changed
    since (a re-split budget keeps it; see loops/refresh.py)."""
    creative = ws.artifacts.latest(f"week{week}/creative/{test_id}")
    review = ws.artifacts.latest(f"week{week}/review/{test_id}")
    if (creative is None or creative.status in ("stale", "superseded") or review is None
            or review.content.get("verdict") != "pass" or review.inputs != [creative.artifact_id]):
        return None
    return CreativeSet.model_validate(creative.content)


async def _render(
    scope: Scope,
    media: MediaClient,
    ad_copy: CreativeCopy,
    concept: str,
    reference: Image.Image,
    product: Image.Image,
    photos: dict[tuple[str, str], tuple[Image.Image, Box | None]],
    folder: Path,
    kit: CreativeKit | None = None,
) -> tuple[list[CreativeImage], CreativeVideo | None, bytes, list, list[tuple[int, float]]]:
    """Each scene as a Muse Image photograph with the real product in it (the product photo is
    the reference; cached per prompt), where SAM 3.1 finds the product; then each scene laid out
    in the three placement sizes and the 10-second video from the same shots. Also returns the
    contact sheet the Reviewer looks at, every laid-out picture for the design checks, and how
    much of the product each video caption covers, by scene number."""
    prompts = [shot_prompt(scene, concept, kit) for scene in ad_copy.scenes]
    missing = [p for p in dict.fromkeys(prompts) if (concept, p) not in photos]

    async def shoot(prompt: str) -> tuple[Image.Image, Box | None]:
        photo = await media.scene(scope, "producer", prompt, reference)
        try:
            boxes = await media.segment(scope, "producer", photo, concept)
        except ValueError:
            return photo, None
        return photo, boxes[0]

    shot = await asyncio.gather(*(shoot(prompt) for prompt in missing))
    photos.update({(concept, p): result for p, result in zip(missing, shot, strict=True)})
    scenes = [photos[(concept, p)] for p in prompts]

    style = Style.from_kit(kit, ad_copy.video.cta)
    _keep_shots(folder, scenes)
    images, squares, laid_out = [], [], []
    for variant, (scene, (photo, box)) in enumerate(zip(ad_copy.scenes, scenes, strict=True),
                                                     start=1):
        for ratio, (width, height) in CREATIVE_SIZES.items():
            design = compose(photo, box, ratio, scene.layout, scene.words or ad_copy.headline,
                             style)
            path = folder / f"v{variant}-{ratio.replace(':', 'x')}.png"
            digest = save_png(design.image, path)
            images.append(CreativeImage(ratio=ratio, width=width, height=height, path=str(path),
                                        sha256=digest, variant=variant))
            laid_out.append((variant, ratio, design))
            if ratio == "1:1":
                squares.append(design.image)

    cut = Cut([photo for photo, _ in scenes], [box for _, box in scenes], product,
              ad_copy.video, ad_copy.headline, style, kit.offer if kit else "",
              kit.sign_off if kit else "")
    video = await asyncio.to_thread(encode, cut, folder / "video.mp4")
    if video is None:
        scope.ws.events.append(
            "creative.video_skipped", "producer",
            {"folder": str(folder), "reason": "ffmpeg is not installed"},
            loop_id=scope.loop_id,
        )
    sheet = contact_sheet(squares, cut.keyframes(), reference)
    captions = [(i % len(scenes) + 1, share) for i, share in enumerate(cut.covered())]
    return images, video, png_bytes(sheet), laid_out, captions


def _keep_shots(folder: Path, scenes: list[tuple[Image.Image, Box | None]]) -> None:
    """Keep each raw Muse shot and where SAM found the product, so the layouts can be redrawn
    later without paying for the photographs again."""
    folder.mkdir(parents=True, exist_ok=True)
    boxes = {}
    for n, (photo, box) in enumerate(scenes, start=1):
        photo.convert("RGB").save(folder / f"shot{n}.jpg", quality=92)
        boxes[f"shot{n}.jpg"] = [box.x1, box.y1, box.x2, box.y2, box.w, box.h] if box else None
    (folder / "shots.json").write_text(json.dumps(boxes, indent=1))


def _question_ids(scope: Scope, task_id: str) -> list[str]:
    return [
        e.payload["question_id"]
        for e in scope.ws.events.list("question.answered", loop_id=scope.loop_id)
        if e.payload["task_id"] == task_id and not e.payload["issues"]
    ]


# --- Stage 3: apply --------------------------------------------------------------------------


def apply_week(
    ws: Workspace, account: SandboxAccount, week: int | None = None
) -> tuple[WeeklyReviewSummary, ExecutionReceipt]:
    week = week or current_week(account)
    key = f"week{week}/change_set"
    plan_key = f"week{week}/test_plan"
    _require_approval(ws, plan_key, "apply", account.day)
    receipt = apply_change_set(ws, account, key)
    scope = _scope(ws, account, week, None)
    change_set = ChangeSet.model_validate(ws.artifacts.latest(key).content)
    summary = _summary(
        scope,
        week,
        "applied",
        plan_id=plan_key,
        creative_ids=[d.creative_id for d in change_set.ad_drafts],
        change_set_id=change_set.change_set_id,
    )
    return summary, receipt
