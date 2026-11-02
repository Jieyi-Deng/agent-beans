"""OFFLINE STAND-IN for the Muse models, for tests and free demo runs.

This is a small rule-based responder, not a model. It answers the same requests a live run sends,
through the same client, schemas, harness and checks, so everything except the model's judgment
is exercised offline. Live runs (`pgt simulate --live`) use Muse Spark instead.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO

from PIL import Image, ImageStat

from pgt.creative.compose import (
    make_demo_product_shot,
    matte,
    offline_background,
    place_product,
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
    LineVerdict,
    MarketInsight,
    MarketResearch,
    MemoryCheck,
    MemoryLineDraft,
    MemoryPass,
    NoteTriage,
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
        "MemoryPass": _memory_pass,
        "MemoryCheck": _memory_check,
        "NoteTriage": _note_triage,
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


DEMO_BACKDROP = (242, 240, 234)  # corner colour of the drawn demo phone shot


def _segment(body: dict) -> RawText:
    """SAM 3.1 stand-in: the phone in the demo product shot, or whatever stands out from the
    backdrop in a studio photo or an offline scene."""
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
    demo = image is None or (
        image.width == image.height and image.convert("RGB").getpixel((0, 0)) == DEMO_BACKDROP
    )
    if demo:
        _, box = make_demo_product_shot(image.width if image is not None else 1600)
    else:
        box = product_box(image)
    return RawText(
        f"<0f>0<|box;x1={box.x1};y1={box.y1};x2={box.x2};y2={box.y2};w={box.w};h={box.h}|>"
    )


def offline_image(body: dict) -> bytes:
    """Muse Image stand-in: a deterministic gradient per prompt, or for an edit, the reference
    product set large on a plain prompt-seeded backdrop."""
    if body.get("image"):
        return png_bytes(_offline_scene(body))
    return png_bytes(offline_background(body.get("prompt", "")))


def _offline_scene(body: dict) -> Image.Image:
    width, height = (int(v) for v in str(body.get("size") or "1024x1536").split("x"))
    with Image.open(BytesIO(body["image"])) as reference:
        reference = reference.convert("RGB")
    if reference.getpixel((0, 0)) == DEMO_BACKDROP and reference.width == reference.height:
        _, box = make_demo_product_shot(reference.width)
    else:
        box = product_box(reference)
    product = matte(reference, box)
    # A flat backdrop far from the product's own brightness, so the SAM stand-in finds it.
    seed = offline_background(body.get("prompt", ""), (8, 8)).getpixel((4, 4))
    light = ImageStat.Stat(product.convert("L")).mean[0] < 128
    tone = tuple(190 + v // 4 for v in seed) if light else tuple(20 + v // 6 for v in seed)
    scene = Image.new("RGBA", (width, height), (*tone, 255))
    scale = min(0.5 * width / product.width, 0.42 * height / product.height)
    product = product.resize((round(product.width * scale), round(product.height * scale)))
    place_product(scene, product, ((width - product.width) // 2, round(0.24 * height)))
    return scene.convert("RGB")


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


LAYOUTS = ("band", "panel", "poster")
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
    "words": ("Ten minutes. Then coffee.", "Show up again tomorrow"),
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
    "words": ("Clips on in seconds", "Free shipping over $50"),
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
                exploratory=False,
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
            exploratory=True,
            brief=CreativeBrief(
                product=flavor["product"],
                angle=flavor["angles"][1],
                hook=inputs["insights"][insight]["hook"],
                key_message=flavor["key_message"],
                insight_ids=[insight],
                replaces_ad_id=None,
            ),
        ))
    if tests and not any(t.exploratory for t in tests):
        tests[-1] = tests[-1].model_copy(update={"exploratory": True})
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
        concept=f"{brief['angle']}: {brief['key_message']}",
        scenes=[Scene(angle=angle, prompt=f"{prompt}, the {brief.get('product') or flavor['product']} "
                      f"from the reference photo, mood: {brief['angle']}", layout=layout,
                      words=words)
                for (angle, prompt), layout, words in zip(
                    flavor["scenes"], LAYOUTS, (brief["hook"][:40], *flavor["words"]),
                    strict=True)],
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
    brief = inputs.get("brief", {})
    checked = [f"Copy follows the brief's angle ({brief.get('angle', 'the brief')}).",
               "No claim that needs proof (#1, best, guarantees, health)."]
    return CreativeReview(verdict="redo" if issues else "pass", issues=issues,
                          checked=[] if issues else checked)


# --- Memory calls -------------------------------------------------------------------------------


def _memory_pass(inputs: dict) -> MemoryPass:
    if "week_record" in inputs:
        return _account_pass(inputs)
    return _lesson_pass(inputs)


def _account_pass(inputs: dict) -> MemoryPass:
    """Merge the week record into the account memory: one line per test read out, one for the
    owner's verdicts. Lines about the same test are changed, not repeated."""
    week, record = inputs["week"], inputs["week_record"]
    current = {line["text"].split(":")[0]: line for line in inputs["current_memory"]
               if not line.get("owner")}
    lines: list[MemoryLineDraft] = []
    for readout in inputs.get("readouts") or record.get("readouts", []):
        head = f"{readout['name'] or readout['test_id']} ({readout['angle'] or 'test'})"
        verdict = {"scale": "worked", "stop": "did not work", "continue": "is still open"}.get(
            readout["recommendation"], "is still open")
        text = f"{head}: {verdict} on {readout['metric'].replace('_', ' ')}."
        old = current.get(head)
        lines.append(MemoryLineDraft(id=old["id"] if old else "new", text=text,
                                     sources=[*readout["evidence_ids"][:2], f"wk-{week}"]))
    owner = record.get("owner", {})
    if owner.get("sent_back"):
        head = "Owner send-backs"
        old = current.get(head)
        lines.append(MemoryLineDraft(
            id=old["id"] if old else "new",
            text=f"{head}: the owner sent back {owner['sent_back']} proposal(s) in week {week}; "
                 "read their notes before the next plan.",
            sources=[f"wk-{week}"]))
    keep = inputs["max_lines"] - len([x for x in lines if x.id == "new"])
    movable = [line for line in inputs["current_memory"] if not line.get("owner")]
    drop = [line["id"] for line in movable[: max(0, len(inputs["current_memory"]) - keep)]]
    summary = (f"Added what week {week}'s tests and approvals showed." if lines or drop
               else f"No change: week {week} had no test readouts or send-backs.")
    return MemoryPass(drop=drop, lines=lines, summary=summary)


BAD_OUTCOMES = {
    "redo": "review sent it back",
    "sent_back": "the owner sent it back",
    "gate_blocked": "a house rule blocked a change",
    "test_result": "the test missed its target",
}
CARE = {
    "change_set": "Check each budget move against the house rules and the owner's notes before "
                  "proposing it.",
    "test_plan": "Tie every test to a reviewed finding and keep the plan small.",
    "creative": "Check every claim against the brand constraints before handing in the copy.",
    "findings": "Copy each number from its evidence and cite the evidence ID.",
    "brief": "Keep the weekly brief to the week's real signals.",
    "research": "Back every insight with a source page.",
    "review": "Name the exact phrase to fix in each review issue.",
}


def _lesson_pass(inputs: dict) -> MemoryPass:
    """Draw lessons from repeated bad outcomes. One lesson per kind of work, citing every
    entry that went badly; the harness decides whether that is enough to keep it."""
    journal = inputs["your_journal"]
    current = {line["text"]: line for line in inputs["your_lessons"]}
    by_kind: dict[str, list[str]] = {}
    for entry in journal:
        bad = [o for o in entry["outcomes"] if o["outcome"] in BAD_OUTCOMES and not o["good"]]
        if bad:
            kind = entry["topic"].split("/")[1] if "/" in entry["topic"] else entry["topic"]
            by_kind.setdefault(kind, []).append(entry["id"])
    lines = []
    for kind, ids in sorted(by_kind.items()):
        text = CARE.get(kind, f"Take more care with {kind.replace('_', ' ')}.")
        old = current.get(text)
        lines.append(MemoryLineDraft(id=old["id"] if old else "new", text=text, sources=ids[-4:]))
    for note in inputs["owner_notes_for_you"]:
        # The stand-in copies the owner's words as they are; code checks what may be kept.
        text = note["text"].split("\n")[0][:200]
        if text and text not in current:
            lines.append(MemoryLineDraft(id="new", text=text, sources=[note["id"]]))
    flagged = [f.split(":")[0] for f in inputs["flags"] if ":" in f]
    drop = [i for i in flagged if any(line["id"] == i for line in inputs["your_lessons"])]
    summary = (f"{len(lines)} lesson(s) from this week's outcomes." if lines or drop
               else "No change: no repeated bad outcomes this week.")
    return MemoryPass(drop=drop, lines=lines[: inputs["max_lessons"]], summary=summary)


# What the stand-in reads as a looser habit. Narrower than the code backstop on purpose, so
# each path is exercised on its own.
LOOSER_HABITS = ("lighter", "one pass", "saving fixes", "fewer", "skip")
TIGHTER_HABITS = ("check", "wait", "protect", "before", "only", "avoid")


def _memory_check(inputs: dict) -> MemoryCheck:
    verdicts = []
    for line in inputs["lines"]:
        text = line["text"].lower()
        risky = next((fix for phrase, fix in RISKY.items() if phrase in text), None)
        looser = next((w for w in LOOSER_HABITS if w in text), None)
        tighter = next((w for w in TIGHTER_HABITS if w in text), None)
        direction = "loosen" if looser else "tighten" if tighter else "neutral"
        verdicts.append(LineVerdict(
            line_id=line["line_id"], verdict="keep" if risky is None else "reject",
            reason="" if risky is None else risky, narrowed="", direction=direction,
            direction_reason=f'it says "{looser or tighter}"' if looser or tighter else ""))
    return MemoryCheck(verdicts=verdicts)


NOTE_ROUTES = (
    ("media_buyer", ("budget", "pause", "spend", "bid", "turn off", "scale")),
    ("producer", ("image", "headline", "copy", "video", "photo", "creative", "ad text")),
    ("analyst", ("number", "metric", "ctr", "cpa", "cost per", "data", "signal")),
    ("strategist", ("research", "competitor", "market", "trend")),
    ("reviewer", ("claim", "review", "compliance")),
)


def _note_triage(inputs: dict) -> NoteTriage:
    text = inputs["note"]["text"].lower()
    if not text.rstrip().endswith("?"):
        for role, words in NOTE_ROUTES:
            if any(w in text for w in words):
                return NoteTriage(action="handoff", to=role,
                                  answer=f"Owner note {inputs['note']['id']}: take this into "
                                         "your next piece of work.",
                                  reason=f"It is about {role.replace('_', ' ')} work.")
    return NoteTriage(action="answer", to="growth_lead",
                      answer="Noted. I will take this into the next plan and say how it went.",
                      reason="It is about the plan as a whole.")


def offline_model(model: str = DEFAULT_MODEL, responder=offline_responder):
    """Returns (model, fake). The fake records every request and any Meta rule it breaks.
    `fake.client()` also serves the offline Muse Image and SAM 3.1 stand-ins."""
    fake = MetaFake(responder, offline_image)
    return responses_model(fake.client(), model), fake
