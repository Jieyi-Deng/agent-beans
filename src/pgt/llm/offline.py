"""OFFLINE STAND-IN for the Muse models, for tests and free demo runs.

This is a small rule-based responder, not a model. It answers the same requests a live run sends,
through the same client, schemas, harness and checks, so everything except the model's judgment
is exercised offline. Live runs (`pgt simulate --live`) use Muse Spark instead.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO

from PIL import Image

from pgt.creative.compose import (
    make_demo_product_shot,
    offline_background,
    png_bytes,
    product_box,
)
from pgt.llm.fake import FunctionCall, MetaFake, RawText, Searched, output_name, prompt_inputs
from pgt.llm.meta import DEFAULT_MODEL, SAM_MODEL, responses_model
from pgt.roles.metrics import expected_readout
from pgt.schemas import (
    Answer,
    Change,
    ChangeSetDraft,
    Claim,
    CreativeBrief,
    CreativeCopy,
    CreativeReview,
    Finding,
    Findings,
    MarketInsight,
    MarketResearch,
    PlannedTest,
    Scene,
    Source,
    Storyboard,
    TestPlanDraft,
    TestReadout,
    WeeklyBrief,
)


def offline_responder(body: dict):
    if body.get("model") == SAM_MODEL:
        return _segment(body)
    inputs = prompt_inputs(body)
    name = output_name(body)
    flavor = STORE if _sells_online(body) else APP
    weekly = {
        "WeeklyBrief": _weekly_brief,
        "Answer": _answer,
        "CreativeReview": _creative_review,
    }
    if name in weekly:
        return weekly[name](inputs)
    if name == "MarketResearch":
        return _research(inputs, flavor)
    if name == "TestPlanDraft":
        return _plan(inputs, flavor)
    if name == "CreativeCopy":
        return _creative_copy(inputs, tool_outputs(body), flavor)
    props = set(body.get("text", {}).get("format", {}).get("schema", {}).get("properties", {}))
    if name == "TestReadout" or "recommendation" in props:
        return _readout(inputs)
    if name == "ChangeSetDraft" or "changes" in props:
        return _changes(inputs)
    return _findings(inputs)


def _findings(inputs: dict) -> Findings:
    return Findings(
        findings=[
            Finding(
                subject_id=s["subject_id"],
                claim=s["note"],
                metric=s["metric"],
                value=s["value"],
                evidence_ids=s["evidence_ids"],
                severity=s["severity"],
            )
            for s in inputs.get("signals", [])
        ]
    )


def _readout(inputs: dict) -> TestReadout:
    (evidence_id, values), *_ = inputs["evidence"].items()
    recommendation = expected_readout(values)
    cpr, target = values["cost_per_result"], values["target_cost_per_result"]
    return TestReadout(
        test_id=inputs["test_id"],
        recommendation=recommendation,
        rationale=f"Cost per result ${cpr:g} against a ${target:g} target "
        f"over {values['results']:g} results.",
        findings=[
            Finding(
                subject_id=inputs["test_id"],
                claim=f"The test's cost per result is ${cpr:g}.",
                metric="cost_per_result",
                value=cpr,
                evidence_ids=[evidence_id],
                severity="act",
            )
        ],
    )


def _changes(inputs: dict) -> ChangeSetDraft:
    account = inputs["account"]
    ads = {a["ad_id"]: a for a in account["ads"]}
    campaigns = {c["test_id"]: c for c in account["campaigns"] if c.get("test_id")}
    changes: list[Change] = []
    used: set[str] = set()
    for f in inputs.get("findings", []):
        if f["severity"] != "act":
            continue
        ad = ads.get(f["subject_id"])
        if ad and ad["status"] == "ACTIVE" and ad["ad_id"] not in used:
            fresh = next(
                (
                    a
                    for a in account["ads"]
                    if a["campaign_id"] == ad["campaign_id"]
                    and a["status"] == "PAUSED"
                    and a["lifetime_spend"] == 0
                    and a["ad_id"] not in used
                ),
                None,
            )
            if fresh:
                used.add(fresh["ad_id"])
                changes.append(
                    _change("activate_ad", fresh["ad_id"], f, f"Swap in {fresh['name']}.")
                )
            used.add(ad["ad_id"])
            changes.append(_change("pause_ad", ad["ad_id"], f, f"{ad['name']}: {f['claim']}"))
        campaign = campaigns.get(f["subject_id"])
        if campaign and f["metric"] == "cost_per_result":
            target = inputs.get("target_cost_per_result")
            if target is None or f["value"] <= 0.85 * target:
                new_budget = round(campaign["daily_budget"] * 1.2, 2)
                changes.append(
                    _change(
                        "set_budget",
                        campaign["campaign_id"],
                        f,
                        "Scale the winning test 20%.",
                        new_budget,
                    )
                )
    summary = "Act on today's findings." if changes else "No change today: nothing calls for one."
    return ChangeSetDraft(summary=_revised(inputs, summary), changes=changes)


def _owner_notes(inputs: dict) -> list[str]:
    return [i for i in inputs.get("review_issues_to_fix", []) if i.startswith("From the owner")]


def _revised(inputs: dict, summary: str) -> str:
    """Mark a redo the owner asked for. The stand-in cannot act on the note's meaning."""
    return f"Redone after the owner's note. {summary}" if _owner_notes(inputs) else summary


def _change(kind, target_id, finding, reason, budget=None) -> Change:
    return Change(
        kind=kind,
        target_id=target_id,
        new_daily_budget=budget,
        reason=reason,
        evidence_ids=finding["evidence_ids"],
    )


# --- Weekly review --------------------------------------------------------------------------


def tool_outputs(body: dict) -> list[dict]:
    """Results of function calls earlier in this run (for example the producer's questions)."""
    outputs = []
    for item in body.get("input", []):
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            try:
                outputs.append(json.loads(item.get("output", "")))
            except (TypeError, ValueError):
                outputs.append({"raw": item.get("output")})
    return outputs


def _sells_online(body: dict) -> bool:
    """The facts in the system prompt say the business counts purchases: an online store."""
    return '\\"result_name\\": \\"purchase\\"' in json.dumps(body)


def _segment(body: dict) -> RawText:
    """SAM 3.1 stand-in: the phone in the demo product shot, or the product in a studio photo."""
    image, concept = None, "phone"
    for item in body.get("input", []):
        for part in item.get("content", []) if isinstance(item, dict) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "input_text":
                concept = part.get("text", concept)
            url = part.get("image_url", "")
            if url.startswith("data:image/"):
                image = Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))
    if image is not None and concept != "phone":
        box = product_box(image)
    else:
        _, box = make_demo_product_shot(image.width if image is not None else 1600)
    return RawText(
        f"<0f>0<|box;x1={box.x1};y1={box.y1};x2={box.x2};y2={box.y2};w={box.w};h={box.h}|>"
    )


def offline_image(body: dict) -> bytes:
    """Muse Image stand-in: a deterministic gradient per prompt."""
    return png_bytes(offline_background(body.get("prompt", "")))


def _weekly_brief(inputs: dict) -> WeeklyBrief:
    headlines = inputs.get("daily_headlines", [])
    acts = [f for f in inputs.get("findings", []) if f["severity"] == "act"]
    return WeeklyBrief(
        summary=f"Last week the daily checks ran {len(headlines)} times and raised "
        f"{len(acts)} finding(s) to act on. This week we refresh tired creative and build on "
        "what worked.",
        focus=["Replace tired creative", "Build on the winning test", "Find fresh angles"],
        questions_for_user=[],
    )


APP = {
    "product": "phone",
    "query": "fitness app ads",
    "angles": ("Time saved", "Habit streak"),
    "key_message": "Short home workouts you can keep up.",
    "primary_text": "Short home workouts that fit your day. Keep your streak going.",
    "description": "Start your free trial",
    "scenes": (
        ("Morning before work", ("A calm, bright living room at sunrise, a rolled yoga mat by "
         "the window, open wooden floor in the lower middle, soft warm light")),
        ("Lunch break at home", ("A tidy home office corner at midday, a water bottle on the "
         "desk, a small rug with clear floor space in the lower middle, daylight")),
        ("Evening wind-down", ("A cosy bedroom corner in the evening, a folded towel and a mug "
         "of tea on a low shelf, empty rug in the lower middle, lamp light")),
    ),
    "video": ("Ten minutes. Your living room.", "Fits before work or after", "Free 7-day trial",
              "Try It"),
}
STORE = {
    "product": "dog harness",
    "query": "pet supply ads",
    "angles": ("Real walks", "Calmer walks"),
    "key_message": "A no-pull harness that makes the daily walk easier.",
    "primary_text": "Our no-pull harness spreads the pull across the chest, so the daily walk "
                    "feels easy for both of you. Free shipping over $50.",
    "description": "Shop the harness",
    "scenes": (
        ("Morning park walk", ("A leafy park path in soft morning light, autumn leaves on the "
         "ground, open path in the lower middle")),
        ("By the front door", ("A bright entryway with a leash hook by the front door, wooden "
         "floor with clear space in the lower middle, window light")),
        ("Autumn street", ("A quiet tree-lined street in autumn, warm low sun, clean pavement in "
         "the lower middle")),
    ),
    "video": ("The walk you both look forward to", "Spreads the pull across the chest",
              "Free shipping over $50", "Shop Now"),
}

OFFLINE_INSIGHTS = [
    (
        "Home workout apps lead with short sessions that fit a busy day.",
        "Time saved",
        "Done before your coffee cools",
        "short-sessions",
    ),
    (
        "Streak and habit mechanics show up in many fitness app ads.",
        "Habit streak",
        "Keep the streak going",
        "streaks",
    ),
    (
        "Ads that show the app screen beat stock gym imagery for trial starts.",
        "Show the app",
        "See your plan before you start",
        "show-the-app",
    ),
]

STORE_INSIGHTS = [
    (
        "Pet brands that show their product in real use lead over plain studio shots.",
        "Real walks",
        "Made for the daily walk",
        "real-use",
    ),
    (
        "Calm, no-pull and quiet-pump benefits are what owners mention most in reviews.",
        "Calmer walks",
        "Calmer walks start here",
        "calm-benefits",
    ),
    (
        "Gift sets and free-shipping thresholds show up across pet store ads before the holidays.",
        "Gift ready",
        "The gift they will use every day",
        "gift-sets",
    ),
]


def _research(inputs: dict, flavor: dict = APP) -> Searched:
    insights = [
        MarketInsight(
            claim=claim,
            angle=angle,
            hook=hook,
            sources=[
                Source(
                    title=f"Offline example: {angle}",
                    url=f"https://offline.invalid/research/{slug}",
                )
            ],
        )
        for claim, angle, hook, slug in (STORE_INSIGHTS if flavor is STORE else OFFLINE_INSIGHTS)
    ]
    return Searched(
        output=MarketResearch(insights=insights),
        queries=[f"{flavor['query']} {focus}" for focus in inputs.get("focus", [])[:2]],
        sources=[(s.title, s.url) for i in insights for s in i.sources],
    )


def _plan(inputs: dict, flavor: dict = APP) -> TestPlanDraft:
    account = inputs["account"]
    ads = {a["ad_id"]: a for a in account["ads"]}
    active = [c for c in account["campaigns"] if c["status"] == "ACTIVE"]
    by_test = {c["test_id"]: c for c in active if c.get("test_id")}
    insight_ids = sorted(inputs["insights"])
    findings = sorted(inputs["reviewed_findings"], key=lambda f: f["severity"] != "act")
    tests: list[PlannedTest] = []
    seen: set[str] = set()
    for f in findings:
        if f["subject_id"] in seen or len(tests) == 2:
            continue
        ad = ads.get(f["subject_id"])
        if ad and f["severity"] == "act":
            campaign_id, replaces = ad["campaign_id"], ad["ad_id"]
            name, angle = "Fresh hook for a tired ad", flavor["angles"][0]
        elif f["subject_id"] in by_test:
            campaign_id, replaces = by_test[f["subject_id"]]["campaign_id"], None
            name, angle = "Build on the winning test", flavor["angles"][1]
        else:
            continue
        if campaign_id not in {c["campaign_id"] for c in active}:
            continue
        seen.add(f["subject_id"])
        if any(t.name == name for t in tests):  # two tired ads: name each test after its ad
            name = f"{name}: {ad['name'] if ad else f['subject_id']}"
        insight = insight_ids[len(tests) % len(insight_ids)]
        hook = inputs["insights"][insight]["hook"]
        tests.append(
            PlannedTest(
                name=name,
                hypothesis=f"A new {angle.lower()} creative lowers cost per result.",
                campaign_id=campaign_id,
                budget_weight=3 if not tests else 2,
                metric="cost_per_result",
                stop_rule="Stop after 7 days, or at 3x target spend with no result.",
                evidence_ids=f["evidence_ids"],
                brief=CreativeBrief(
                    product=flavor["product"],
                    angle=angle,
                    hook=hook,
                    key_message=flavor["key_message"],
                    insight_ids=[insight],
                    replaces_ad_id=replaces,
                ),
            )
        )
    if len(tests) == 1 and findings:
        # Only one finding to act on: also try a fresh angle where most of the budget runs.
        main = max(active, key=lambda c: c.get("daily_budget", 0))
        insight = insight_ids[1 % len(insight_ids)]
        tests.append(PlannedTest(
            name="Try a fresh angle",
            hypothesis=f"A new {flavor['angles'][1].lower()} creative lowers cost per result.",
            campaign_id=main["campaign_id"],
            budget_weight=2,
            metric="cost_per_result",
            stop_rule="Stop after 7 days, or at 3x target spend with no result.",
            evidence_ids=findings[0]["evidence_ids"],
            brief=CreativeBrief(
                product=flavor["product"],
                angle=flavor["angles"][1],
                hook=inputs["insights"][insight]["hook"],
                key_message=flavor["key_message"],
                insight_ids=[insight],
                replaces_ad_id=None,
            ),
        ))
    summary = _revised(inputs, "Two creative tests drawn from this week's findings.")
    return TestPlanDraft(summary=summary, tests=tests)


def _creative_copy(
    inputs: dict, outputs: list[dict], flavor: dict = APP
) -> CreativeCopy | FunctionCall:
    test = inputs["test"]
    if not outputs:
        return FunctionCall(
            "ask_analyst",
            {"question": f"What numbers back test {test['test_id']} ({test['name']})?"},
        )
    evidence_ids = next((o["evidence_ids"] for o in outputs if o.get("evidence_ids")), [])
    brief = test["brief"]
    redo = bool(_owner_notes(inputs))
    hook, benefit, proof, cta = flavor["video"]
    if redo:
        hook = brief["key_message"][:34]
    return CreativeCopy(
        name=f"{brief['angle']} {'v2' if redo else 'v1'}",
        # A rule-based stand-in cannot read the note; it switches to the key message so the
        # redo is visibly a new version. Muse Spark reads the note in live runs.
        headline=(brief["key_message"] if redo else brief["hook"])[:40],
        primary_text=flavor["primary_text"],
        description=flavor["description"],
        claims=[Claim(text=brief["key_message"], evidence_ids=evidence_ids, source_urls=[])],
        scenes=[Scene(angle=angle, prompt=f"{prompt}, mood: {brief['angle']}")
                for angle, prompt in flavor["scenes"]],
        video=Storyboard(hook=hook, benefit=benefit, proof=proof, cta=cta),
    )


def _answer(inputs: dict) -> Answer:
    evidence = inputs.get("evidence", {})
    if not evidence:
        return Answer(answer="There is no evidence to answer with.", evidence_ids=[])
    evidence_id = next(
        (i for i, v in evidence.items() if "cost_per_result" in v), next(iter(evidence))
    )
    values = evidence[evidence_id]
    metric = "cost_per_result" if "cost_per_result" in values else next(iter(values))
    return Answer(
        answer=f"The closest figure is {metric.replace('_', ' ')} at {abs(values[metric]):g}.",
        evidence_ids=[evidence_id],
    )


RISKY = {
    "#1": "Remove the #1 claim; the constraints forbid it without proof.",
    "number one": "Remove the number one claim; the constraints forbid it without proof.",
    "best": "Remove 'best'; it is a superlative that needs proof.",
    "guarantee": "Remove the guarantee; results are not guaranteed.",
    "weight loss": "Remove the weight-loss claim; the brand voice rules out health claims.",
    "lose weight": "Remove the weight-loss claim; the brand voice rules out health claims.",
}


def _creative_review(inputs: dict) -> CreativeReview:
    ad = inputs["ad"]
    text = " ".join([ad["headline"], ad["primary_text"], ad["description"]]).lower()
    issues = [fix for phrase, fix in RISKY.items() if phrase in text]
    return CreativeReview(verdict="redo" if issues else "pass", issues=issues)


def offline_model(model: str = DEFAULT_MODEL, responder=offline_responder):
    """Returns (model, fake). The fake records every request and any Meta rule it breaks.
    `fake.client()` also serves the offline Muse Image and SAM 3.1 stand-ins."""
    fake = MetaFake(responder, offline_image)
    return responses_model(fake.client(), model), fake
