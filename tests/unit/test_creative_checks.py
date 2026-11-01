"""Code checks for weekly-review work: copy, images, plan, research, answers (task D)."""

import hashlib

import pytest
from PIL import Image

from pgt.harness.creative_checks import (
    COPY_LIMITS,
    allocate_budget,
    check_answer,
    check_copy,
    check_images,
    check_plan,
    check_research,
    check_video,
    extract_numbers,
)
from pgt.harness.workspace import Workspace
from pgt.schemas import (
    CREATIVE_SIZES,
    Answer,
    Claim,
    CreativeBrief,
    CreativeCopy,
    CreativeImage,
    CreativeVideo,
    Evidence,
    MarketInsight,
    MarketResearch,
    PlannedTest,
    Scene,
    Source,
    Storyboard,
    TestPlanDraft,
)


@pytest.fixture
def ledger():
    ws = Workspace()
    ws.ledger.add(
        Evidence(
            evidence_id="ev-d7-test_1-readout",
            kind="derived",
            values={"cost_per_result": 2.4, "cost_ratio_to_target": 0.6, "results": 42.0},
        )
    )
    ws.ledger.add(
        Evidence(evidence_id="ev-d3-ad_1-ctr", kind="derived", values={"ctr_change": -0.51})
    )
    return ws.ledger


def copy(**update):
    base = CreativeCopy(
        name="Social proof v2",
        headline="42 people started this week",
        primary_text="Ten minutes a day. Start a 10-minute plan and keep your streak going.",
        description="Free 7-day trial",
        claims=[
            Claim(text="42 people started", evidence_ids=["ev-d7-test_1-readout"], source_urls=[])
        ],
        scenes=[
            Scene(angle="Sunrise", prompt="A bright living room at sunrise, empty floor space",
                  layout="band", words="Done before your coffee cools"),
            Scene(angle="Lunch break", prompt="A tidy home office at midday, clear floor",
                  layout="panel", words="Ten minutes. Then lunch."),
            Scene(angle="Evening", prompt="A cosy bedroom corner in lamp light, empty rug",
                  layout="poster", words="Show up again tomorrow"),
        ],
        video=Storyboard(hook="Ten minutes a day", benefit="Fits before work",
                         proof="Free 7-day trial", cta="Try It"),
    )
    return base.model_copy(update=update)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Join 10k+ people", [10000]),
        ("1,200 workouts", [1200]),
        ("$2.40 per trial", [2.4]),
        ("38% more", [38]),
        ("10-minute workouts", [10]),
        ("3.5x faster, 24/7", [3.5, 24, 7]),
        ("Rated #1 app", []),
        ("Your 1st streak", []),
        ("2M users", [2_000_000]),
        ("No numbers here", []),
        ("10km run", [10]),
    ],
)
def test_extract_numbers(text, expected):
    assert extract_numbers(text) == pytest.approx(expected)


def test_allocate_budget():
    assert allocate_budget([1, 1, 1], 77) == pytest.approx([25.67, 25.67, 25.66])
    parts = allocate_budget([3, 2], 77)
    assert parts == pytest.approx([46.2, 30.8]) and round(sum(parts), 2) == 77
    assert sum(allocate_budget([5, 1, 2], 100.01)) == pytest.approx(100.01)
    with pytest.raises(ValueError):
        allocate_budget([], 10)
    with pytest.raises(ValueError):
        allocate_budget([2, 0], 10)


def test_good_copy_passes(ledger):
    assert check_copy(copy(), ledger, [10, 7, 4.0, 385], set()) == []
    assert COPY_LIMITS == {"headline": 40, "primary_text": 125, "description": 30}


def test_copy_length_limits(ledger):
    issues = check_copy(copy(headline="x" * 41, description="y" * 31), ledger, [7], set())
    assert "headline is 41 characters; the limit is 40." in issues
    assert "description is 31 characters; the limit is 30." in issues


def test_copy_numbers_must_match_evidence_or_facts(ledger):
    bad = copy(primary_text="40% more people finish their first week. Start a 10-minute plan.")
    issues = check_copy(bad, ledger, [10, 7], set())
    assert issues == ["Number 40 in primary_text matches no cited evidence or shared fact."]
    # 60% matches the cited ratio 0.6; 51% matches abs(-0.51) once that evidence is cited.
    ok = copy(
        primary_text="60% of the target cost. CTR fell 51% on old ads.",
        claims=[
            Claim(text="60%", evidence_ids=["ev-d7-test_1-readout"], source_urls=[]),
            Claim(text="51%", evidence_ids=["ev-d3-ad_1-ctr"], source_urls=[]),
        ],
        headline="42 people started this week",
    )
    assert check_copy(ok, ledger, [7], set()) == []


def test_copy_claims_need_known_support(ledger):
    bad = copy(
        claims=[
            Claim(text="42 people", evidence_ids=["ev-d7-test_1-readout"], source_urls=[]),
            Claim(text="Most apps", evidence_ids=[], source_urls=[]),
            Claim(text="x", evidence_ids=["ev-nope"], source_urls=["https://made.up/x"]),
            Claim(text="y", evidence_ids=[], source_urls=["https://found.example/a"]),
        ]
    )
    issues = check_copy(bad, ledger, [7, 10], {"https://found.example/a"})
    assert ('Claim 1 has no evidence IDs or source URLs; cite "facts" when the shared facts or '
            "the kit state it.") in issues
    assert "Claim 2 cites unknown evidence ev-nope." in issues
    assert "Claim 2 cites a source URL that research did not find: https://made.up/x." in issues
    assert len(issues) == 3


def test_copy_claims_may_cite_the_advertisers_own_facts(ledger):
    own = copy(primary_text="A 7-day free trial, cancel any time.", headline="Start today",
               claims=[Claim(text="7-day free trial", evidence_ids=["facts"], source_urls=[])])
    assert check_copy(own, ledger, [7], set()) == []
    # Citing the facts does not let a number through that the facts do not state.
    made_up = copy(primary_text="A 30-day free trial, cancel any time.", headline="Start today",
                   claims=[Claim(text="30-day free trial", evidence_ids=["facts"],
                                 source_urls=[])])
    assert check_copy(made_up, ledger, [7], set()) == [
        "Number 30 in primary_text matches no cited evidence or shared fact."]


def write_image(tmp_path, ratio, size=None):
    width, height = size or CREATIVE_SIZES[ratio]
    path = tmp_path / f"{ratio.replace(':', 'x')}.png"
    Image.new("RGB", (width, height), (10, 20, 30)).save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    w, h = CREATIVE_SIZES[ratio]
    return CreativeImage(ratio=ratio, width=w, height=h, path=str(path), sha256=digest)


def test_scenes_and_storyboard(ledger):
    scenes = copy().scenes
    issues = check_copy(copy(scenes=scenes[:2]), ledger, [7, 10], set())
    assert "The ad has 2 scene(s); it needs 3." in issues
    same = [scenes[0], scenes[1], scenes[1]]
    assert "Each scene needs a different angle." in check_copy(copy(scenes=same), ledger, [7], set())

    long_hook = Storyboard(hook="x" * 35, benefit="b", proof="p", cta="Shop Now")
    issues = check_copy(copy(video=long_hook), ledger, [7], set(), ["Try It", "Sign Up"])
    assert "Video hook line is 35 characters; the limit is 34." in issues
    assert "Video button 'Shop Now' is not one of the advertiser's: Try It, Sign Up." in issues

    # Numbers on screen in the video need support like numbers in the ad text.
    made_up = Storyboard(hook="Join 40 friends", benefit="b", proof="p", cta="Try It")
    assert check_copy(copy(video=made_up), ledger, [7, 10], set()) == [
        "Number 40 in video hook matches no cited evidence or shared fact."
    ]


def test_every_scene_needs_every_size(tmp_path):
    images = []
    for variant in (1, 2, 3):
        folder = tmp_path / f"v{variant}"
        folder.mkdir()
        images += [write_image(folder, r).model_copy(update={"variant": variant})
                   for r in CREATIVE_SIZES]
    assert check_images(images) == []
    assert check_images(images[:-1]) == ["Missing image for scene 3 9:16."]


def test_video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    video = CreativeVideo(width=1080, height=1920, seconds=10, path=str(path), sha256=digest)
    assert check_video(video) == []
    assert check_video(None) == []
    assert "Video is 6 seconds; expected 10." in check_video(video.model_copy(update={"seconds": 6}))
    assert "Video file does not match its sha256." in check_video(
        video.model_copy(update={"sha256": "0" * 64})
    )
    assert check_video(video.model_copy(update={"path": str(tmp_path / "gone.mp4")})) == [
        "Video file is missing."
    ]


def test_images(tmp_path):
    images = [write_image(tmp_path, r) for r in CREATIVE_SIZES]
    assert check_images(images, variants=1) == []

    assert check_images(images[:2], variants=1) == ["Missing image for 9:16."]
    assert "Duplicate image for 1:1." in check_images([*images, images[0]], variants=1)

    wrong = images[1].model_copy(update={"width": 1000})
    assert "Image 4:5 is 1000x1350; expected 1080x1350." in check_images(
        [images[0], wrong, images[2]], variants=1
    )

    missing = images[0].model_copy(update={"path": str(tmp_path / "gone.png")})
    assert "Image 1:1 file is missing." in check_images([missing, *images[1:]], variants=1)

    tampered = images[0].model_copy(update={"sha256": "0" * 64})
    assert "Image 1:1 file does not match its sha256." in check_images([tampered, *images[1:]], variants=1)

    (tmp_path / "small").mkdir()
    small = write_image(tmp_path / "small", "9:16", size=(500, 500))
    issues = check_images([*images[:2], small], variants=1)
    assert "Image 9:16 is 500x500; expected 1080x1920." in issues


def planned(**update):
    base = PlannedTest(
        name="Social proof hook",
        hypothesis="Real member counts lower cost per trial.",
        campaign_id="cmp_1",
        budget_weight=3,
        metric="cost_per_result",
        stop_rule="Stop after 7 days or 3x target spend with no result.",
        evidence_ids=["ev-d7-test_1-readout"],
        brief=CreativeBrief(
            product="phone",
            angle="Social proof",
            hook="42 people started this week",
            key_message="People like you are doing it.",
            insight_ids=["ins-w1-1"],
            replaces_ad_id="ad_1",
        ),
    )
    return base.model_copy(update=update)


PLAN_KW = {
    "reviewed_evidence_ids": {"ev-d7-test_1-readout", "ev-w1-ad_1-week"},
    "insight_ids": {"ins-w1-1", "ins-w1-2"},
    "active_campaign_ids": {"cmp_1", "cmp_2"},
    "ad_ids": {"ad_1", "ad_2"},
}


def test_plan_passes_and_fails():
    second = planned(name="Coach angle", evidence_ids=["ev-w1-ad_1-week"], exploratory=True)
    assert check_plan(TestPlanDraft(summary="s", tests=[planned(), second]), **PLAN_KW) == []

    assert check_plan(TestPlanDraft(summary="s", tests=[planned()]), **PLAN_KW) == [
        "The plan has 1 test(s); it needs 2 to 3."
    ]

    bad = planned(
        name="Social proof hook",
        exploratory=True,
        budget_weight=7,
        campaign_id="cmp_9",
        evidence_ids=["ev-other"],
        brief=planned().brief.model_copy(
            update={"insight_ids": ["ins-w1-9"], "replaces_ad_id": "ad_9"}
        ),
    )
    issues = check_plan(TestPlanDraft(summary="s", tests=[planned(), bad]), **PLAN_KW)
    assert set(issues) == {
        "Test names must be unique.",
        "Test 1 budget_weight 7 is outside 1-5.",
        "Test 1 cites evidence ev-other that is not in this week's reviewed findings.",
        "Test 1 cites unknown insight ins-w1-9.",
        "Test 1 uses campaign cmp_9, which is not active.",
        "Test 1 replaces unknown ad ad_9.",
    }

    empty = planned(
        name="Empty",
        exploratory=True,
        evidence_ids=[],
        brief=planned().brief.model_copy(update={"insight_ids": [], "replaces_ad_id": None}),
    )
    issues = check_plan(TestPlanDraft(summary="s", tests=[planned(), empty]), **PLAN_KW)
    assert set(issues) == {"Test 1 cites no evidence.", "Test 1 has no insight IDs."}


def test_plan_keeps_exactly_one_exploratory_test():
    fixes = [planned(), planned(name="Coach angle")]
    assert check_plan(TestPlanDraft(summary="s", tests=fixes), **PLAN_KW) == [
        ("The plan marks 0 test(s) exploratory; mark exactly one, a test that tries a new "
         "angle rather than fixing a finding.")
    ]
    both = [planned(exploratory=True), planned(name="Coach angle", exploratory=True)]
    assert "marks 2 test(s) exploratory" in check_plan(
        TestPlanDraft(summary="s", tests=both), **PLAN_KW)[0]
    # Plans saved before the flag existed load with every test as a fix.
    old = planned().model_dump()
    del old["exploratory"]
    assert PlannedTest.model_validate(old).exploratory is False
    assert "exploratory" in PlannedTest.model_json_schema()["required"]


def test_research():
    good = MarketInsight(
        claim="Fitness apps lean on streak mechanics in ads.",
        angle="Streaks",
        hook="Do not break the chain",
        sources=[Source(title="A report", url="https://example.com/report")],
    )
    assert check_research(MarketResearch(insights=[good])) == []
    assert check_research(MarketResearch(insights=[])) == ["Research has no insights."]
    bad = good.model_copy(update={"hook": " ", "sources": [Source(title="x", url="ftp://x")]})
    assert set(check_research(MarketResearch(insights=[good, bad]))) == {
        "Insight 1 is missing hook.",
        "Insight 1 has no http(s) source.",
    }


def test_answer(ledger):
    ok = Answer(
        answer="The test cost $2.40 per trial, 60% of target.",
        evidence_ids=["ev-d7-test_1-readout"],
    )
    assert check_answer(ok, ledger) == []
    assert check_answer(Answer(answer="Fine.", evidence_ids=[]), ledger) == [
        "The answer cites no evidence."
    ]
    assert check_answer(Answer(answer="Fine.", evidence_ids=["ev-x"]), ledger) == [
        "The answer cites unknown evidence ev-x."
    ]
    wrong = Answer(answer="It cost $3.10 per trial.", evidence_ids=["ev-d7-test_1-readout"])
    assert check_answer(wrong, ledger) == ["Number 3.1 in the answer matches no cited evidence."]


def test_plan_shows_only_products_with_a_photo():
    store = planned(name="Fountain", exploratory=True, brief=planned().brief.model_copy(
        update={"product": "cat water fountain"}))
    kw = {**PLAN_KW, "products": {"dog harness", "cat water fountain"}}
    assert check_plan(TestPlanDraft(summary="s", tests=[planned(), store]), **kw) == [
        ("Test 0 shows 'phone', which has no photo on file; "
         "pick one of cat water fountain, dog harness.")
    ]


def test_plans_saved_before_briefs_named_a_product_still_load():
    old = planned().brief.model_dump(mode="json")
    del old["product"]
    assert CreativeBrief.model_validate(old).product == ""
