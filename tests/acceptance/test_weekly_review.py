"""Acceptance scenarios 1, 2, 3 and 6: the weekly review, run offline through the real
client, harness, image pipeline and checks. Each test starts after a 7-day simulation."""

import asyncio

import pytest

from pgt.llm.fake import output_name, prompt_inputs
from pgt.llm.media import MediaClient
from pgt.llm.offline import offline_model, offline_responder, tool_outputs
from pgt.loops.simulate import simulate
from pgt.loops.weekly import apply_week, plan_week, produce_week
from pgt.schemas import ChangeSet, Claim, CreativeCopy, Scene, Storyboard
from pgt.schemas import TestPlan as Plan


def three_scenes():
    return [
        Scene(angle="Morning", prompt="A bright living room at sunrise with open floor space",
              layout="band", words="Done before your coffee cools"),
        Scene(angle="Midday", prompt="A tidy home office corner at midday, clear floor",
              layout="panel", words="Ten minutes. Then lunch."),
        Scene(angle="Evening", prompt="A cosy bedroom corner in lamp light, empty rug",
              layout="poster", words="Show up again tomorrow"),
    ]


def storyboard():
    return Storyboard(hook="Ten minutes. Your living room.", benefit="Fits before work",
                      proof="Short plans you keep", cta="Try It")


def week_one(responder=offline_responder):
    model, fake = offline_model(responder=responder)
    sim = asyncio.run(simulate(7, model))
    return sim.ws, sim.account, model, fake


def approve(ws, key):
    return ws.approvals.grant(key, ws.artifacts.latest(key), actor="user")


def plan_and_produce(ws, account, model, fake, media_dir):
    summary = asyncio.run(plan_week(ws, account, model))
    assert summary.stage == "awaiting_plan_approval"
    approve(ws, "week1/test_plan")
    return asyncio.run(produce_week(ws, account, model, MediaClient(fake.client()), media_dir))


def first_draft_for(test_id, bad_copy):
    """A responder that plants `bad_copy` as the producer's first draft for one test."""

    def responder(body):
        if output_name(body) == "CreativeCopy" and tool_outputs(body):
            inputs = prompt_inputs(body)
            if inputs["test"]["test_id"] == test_id and "review_issues_to_fix" not in inputs:
                return bad_copy(tool_outputs(body))
        return offline_responder(body)

    return responder


def redos(ws, subject):
    return [e.payload for e in ws.events.list("review.redo") if e.payload["subject"] == subject]


def test_scenario_1_normal_weekly_review(tmp_path):
    ws, account, model, fake = week_one()
    ads_before = {a.ad_id for a in account.state().ads}

    summary = plan_and_produce(ws, account, model, fake, tmp_path)
    assert summary.stage == "awaiting_change_approval"
    assert summary.creative_ids == ["w1-t1-c1", "w1-t2-c1"]

    # The plan answers this week's reviewed findings, with code-set budgets.
    plan = Plan.model_validate(ws.artifacts.latest("week1/test_plan").content)
    assert plan.available_test_budget == 77.0
    assert sum(t.weekly_budget for t in plan.tests) == pytest.approx(77.0)
    assert plan.tests[0].evidence_ids == ["ev-d3-ad_1-ctr"]  # the planted day-3 fatigue
    assert plan.tests[0].brief.replaces_ad_id == "ad_1"

    # The Strategist searched the web, and each producer asked the Analyst one question.
    research_calls = [
        e
        for e in ws.events.list("model.call", loop_id="weekly-w1")
        if e.payload["role"] == "strategist"
    ]
    assert sum(e.payload["searches"] for e in research_calls) >= 1
    research = [r for r in fake.requests if output_name(r) == "MarketResearch"]
    assert research and all(r["max_tool_calls"] == 8 for r in research)  # a search budget
    answered = ws.events.list("question.answered", loop_id="weekly-w1")
    assert {e.payload["task_id"] for e in answered} == {"w1-t1", "w1-t2"}
    assert all(not e.payload["issues"] for e in answered)

    # Images came from Muse Image and SAM 3.1, in all three placement sizes.
    models = {e.payload["model"] for e in ws.events.list("model.call", loop_id="weekly-w1")}
    assert {"muse-image-1.0", "sam-3.1"} <= models
    creative = ws.artifacts.latest("week1/creative/w1-t1").content
    assert {i["ratio"] for i in creative["images"]} == {"1:1", "4:5", "9:16"}
    assert creative["question_ids"]

    # Approval 2, then the Operator creates the ads PAUSED.
    approve(ws, "week1/change_set")
    summary, receipt = apply_week(ws, account)
    assert summary.stage == "applied"
    assert len(receipt.created_ad_ids) == 2
    new_ads = [a for a in account.state().ads if a.ad_id not in ads_before]
    assert [a.ad_id for a in new_ads] == receipt.created_ad_ids
    assert all(a.status == "PAUSED" for a in new_ads)
    assert {a.campaign_id for a in new_ads} == {t.campaign_id for t in plan.tests}
    assert fake.violations == []


def test_scenario_2_unsupported_claim_is_sent_back(tmp_path):
    def bad(outputs):
        return CreativeCopy(
            name="Number one v1",
            headline="The #1 fitness app for busy people",
            primary_text="Short home workouts that fit your day.",
            description="Start your free trial",
            claims=[
                Claim(text="fits your day", evidence_ids=outputs[0]["evidence_ids"], source_urls=[])
            ],
            scenes=three_scenes(),
            video=storyboard(),
        )

    ws, account, model, fake = week_one(first_draft_for("w1-t1", bad))
    summary = plan_and_produce(ws, account, model, fake, tmp_path)

    sent_back = redos(ws, "w1-t1-c1")
    assert len(sent_back) == 1
    # Code checks ignore "#1" as a rank; the model Reviewer is the one that sends it back.
    assert sent_back[0]["by"] == "reviewer"
    assert any("#1" in issue for issue in sent_back[0]["issues"])
    assert "w1-t1-c2" in summary.creative_ids
    final = ws.artifacts.latest("week1/creative/w1-t1").content
    assert final["attempt"] == 2 and "#1" not in final["ad_copy"]["headline"]
    assert ws.artifacts.latest("week1/review/w1-t1").content["verdict"] == "pass"
    assert summary.stage == "awaiting_change_approval"


def test_scenario_3_wrong_number_is_blocked_by_code(tmp_path):
    def bad(outputs):
        return CreativeCopy(
            name="Fatigue v1",
            headline="Old ads tired out fast",
            primary_text="CTR fell 50% on old ads. Try a fresh 10-minute plan.",
            description="Start your free trial",
            # The cited evidence says the drop was 51%.
            claims=[Claim(text="CTR fell 50%", evidence_ids=["ev-d3-ad_1-ctr"], source_urls=[])],
            scenes=three_scenes(),
            video=storyboard(),
        )

    ws, account, model, fake = week_one(first_draft_for("w1-t1", bad))
    assert ws.ledger.get("ev-d3-ad_1-ctr").values["ctr_change_vs_first_day"] == pytest.approx(-0.51)
    summary = plan_and_produce(ws, account, model, fake, tmp_path)

    blocked = redos(ws, "w1-t1-c1")
    assert len(blocked) == 1 and blocked[0]["by"] == "code"
    assert blocked[0]["issues"] == [
        "Number 50 in primary_text matches no cited evidence or shared fact."
    ]
    # The model Reviewer never saw the blocked draft: one review call per passing draft only.
    reviews = [
        e
        for e in ws.events.list("context.assembled", loop_id="weekly-w1")
        if e.payload["role"] == "reviewer"
    ]
    assert len(reviews) == 2  # w1-t1-c2 and w1-t2-c1
    assert "w1-t1-c2" in summary.creative_ids


def test_scenario_6_edit_after_approval_voids_it(tmp_path):
    ws, account, model, fake = week_one()
    assert asyncio.run(plan_week(ws, account, model)).stage == "awaiting_plan_approval"
    approve(ws, "week1/test_plan")

    # Someone edits the approved plan: the approval no longer covers it.
    plan = Plan.model_validate(ws.artifacts.latest("week1/test_plan").content)
    edited = plan.model_copy(update={"summary": plan.summary + " Doubled the first test."})
    ws.artifacts.publish("week1/test_plan", edited, "growth_lead", sim_day=account.day)
    media = MediaClient(fake.client())
    with pytest.raises(PermissionError):
        asyncio.run(produce_week(ws, account, model, media, tmp_path))
    assert ws.events.list("approval.voided")

    # Approve again, produce, approve the change set, then edit the change set.
    approve(ws, "week1/test_plan")
    asyncio.run(produce_week(ws, account, model, media, tmp_path))
    approve(ws, "week1/change_set")
    change_set = ChangeSet.model_validate(ws.artifacts.latest("week1/change_set").content)
    draft = change_set.ad_drafts[0].model_copy(update={"campaign_id": "cmp_2"})
    edited_cs = change_set.model_copy(update={"ad_drafts": [draft, *change_set.ad_drafts[1:]]})
    ws.artifacts.publish("week1/change_set", edited_cs, "growth_lead", sim_day=account.day)

    ads_before = len(account.state().ads)
    with pytest.raises(PermissionError):
        apply_week(ws, account)
    assert len(account.state().ads) == ads_before
    assert ws.events.list("changeset.refused")


def test_no_product_found_escalates(tmp_path):
    from pgt.llm.fake import RawText

    def responder(body):
        if body.get("model") == "sam-3.1":
            return RawText("no objects found")
        return offline_responder(body)

    ws, account, model, fake = week_one(responder)
    summary = plan_and_produce(ws, account, model, fake, tmp_path)
    assert summary.stage == "escalated"
    assert summary.escalations == ["SAM 3.1 found no phone in the product photo."]
    assert ws.artifacts.latest("week1/change_set") is None


def test_a_reviewer_call_that_fails_reaches_the_owner_as_a_failure_not_a_send_back(tmp_path):
    def responder(body):
        if output_name(body) == "CreativeReview":
            return {"verdict": 7}  # never a valid review: the call fails after its retries
        return offline_responder(body)

    ws, account, model, fake = week_one(responder)
    summary = plan_and_produce(ws, account, model, fake, tmp_path)
    assert summary.stage == "escalated"
    failed = ws.events.list("review.failed")
    assert failed and all(e.payload["failure"] == "schema" for e in failed)
    reasons = [e.payload["reason"] for e in ws.events.list("escalation.raised")]
    assert any(r.startswith("Review could not run for w1-t") for r in reasons)
    # Nothing was sent back to the Producer, and its work is not judged by the failure.
    assert not [e for e in ws.events.list("review.redo") if e.payload["subject"].endswith("-c1")]
    from pgt.harness import memory

    memory.record_outcomes(ws)
    outcomes = [o.payload for o in ws.events.list("journal.outcome")
                if o.payload["outcome"] == "review_failed"]
    assert outcomes and all(o["good"] is None and o["role"] == "producer" for o in outcomes)
