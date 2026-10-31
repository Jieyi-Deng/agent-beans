"""Deliverables of the weekly growth review.

Classes under "Returned by model calls" follow Meta's strict JSON-schema rules: every field
required, no defaults. Classes under "Built in code" are assembled and checked by the harness.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Returned by model calls ---------------------------------------------------------------


class WeeklyBrief(_Out):
    """Growth Lead, Intake: what this week is about."""

    summary: str  # two or three plain sentences: last week and this week's goal
    focus: list[str]  # what the team should look into this week
    questions_for_user: list[str]  # empty when nothing is unclear


class Source(_Out):
    title: str
    url: str


class MarketInsight(_Out):
    claim: str  # what competitors or the market are doing, one sentence
    angle: str  # the creative angle it suggests
    hook: str  # a short opening line that angle could use
    sources: list[Source]  # every market claim needs at least one link


class MarketResearch(_Out):
    """Creative Strategist, Discover."""

    insights: list[MarketInsight]


class CreativeBrief(_Out):
    product: str  # the one product the ad shows, a name from products_on_file
    angle: str
    hook: str
    key_message: str
    insight_ids: list[str]  # insights this brief draws on, e.g. "ins-w1-2"
    replaces_ad_id: str | None  # the ad this creative is meant to replace, if any

    @model_validator(mode="before")
    @classmethod
    def _older_plans(cls, data):
        """Plans saved before briefs named a product show the main product."""
        if isinstance(data, dict) and "product" not in data:
            return {"product": "", **data}
        return data


class PlannedTest(_Out):
    name: str
    hypothesis: str
    campaign_id: str  # where the new ads will run
    budget_weight: int  # relative share of the test budget, 1-5; code turns it into dollars
    metric: Literal["cost_per_result", "ctr"]
    stop_rule: str
    evidence_ids: list[str]  # evidence of the findings this test responds to
    brief: CreativeBrief


class TestPlanDraft(_Out):
    """Growth Lead, Plan."""

    __test__ = False  # not a pytest test class

    summary: str
    tests: list[PlannedTest]


class Claim(_Out):
    text: str  # the phrase in the copy that states a fact
    evidence_ids: list[str]
    source_urls: list[str]


class Scene(_Out):
    angle: str  # what this picture tries, in a few words, e.g. "Dark evening walk"
    prompt: str  # background scene only: no text, no product, no people's bodies


class Storyboard(_Out):
    """Words on screen in the 10-second 9:16 video. Code animates the three scenes in order and
    ends on a card with the product, the offer and the button."""

    hook: str  # 0-3 s over scene 1: stops the scroll
    benefit: str  # 3-6 s over scene 2: what changes for the customer
    proof: str  # 6-8.5 s over scene 3: why to believe it
    cta: str  # 8.5-10 s end card: one of the advertiser's call-to-action buttons


class CreativeCopy(_Out):
    """Creative Producer: the words of one ad, three background scenes and a video storyboard."""

    name: str  # short ad name, e.g. "Progress photo v1"
    headline: str
    primary_text: str
    description: str
    claims: list[Claim]  # every factual statement in the copy, with its support
    scenes: list[Scene]  # exactly three, each a different setting for the same message
    video: Storyboard

    @model_validator(mode="before")
    @classmethod
    def _older_copy(cls, data):
        """Copy saved before ads had three scenes carries one image_prompt and no video."""
        if isinstance(data, dict) and "image_prompt" in data and "scenes" not in data:
            data = dict(data)
            prompt = data.pop("image_prompt")
            data["scenes"] = [{"angle": data.get("name", ""), "prompt": prompt}]
            data["video"] = {"hook": data.get("headline", ""), "benefit": "", "proof": "",
                             "cta": ""}
        return data


class Answer(_Out):
    """Analyst, answering one question from another role."""

    answer: str
    evidence_ids: list[str]


class CreativeReview(_Out):
    """Reviewer's judgment on one creative set."""

    verdict: Literal["pass", "redo"]
    issues: list[str]  # required fixes, one per line; empty on pass


# --- Built in code ---------------------------------------------------------------------------

Ratio = Literal["1:1", "4:5", "9:16"]

# Output size in pixels for each placement ratio (Feed square, Feed portrait, Stories and Reels).
CREATIVE_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "9:16": (1080, 1920),
}


class PlanTest(PlannedTest):
    test_id: str  # "w1-t1"
    weekly_budget: float  # dollars, from budget_weight


class TestPlan(_Out):
    __test__ = False  # not a pytest test class

    plan_id: str  # "week1/test_plan"
    week: int
    available_test_budget: float  # weekly_budget x test_budget_share
    summary: str
    tests: list[PlanTest]


class CreativeImage(_Out):
    ratio: Ratio
    width: int
    height: int
    path: str
    sha256: str
    variant: int = 1  # which of the copy's scenes, from 1


class CreativeVideo(_Out):
    width: int
    height: int
    seconds: float
    path: str
    sha256: str


class CreativeSet(_Out):
    creative_id: str  # "w1-t1-c1"
    test_id: str
    attempt: int  # 1 for the first draft; counts on through redos and send-backs
    ad_copy: CreativeCopy
    images: list[CreativeImage]  # every scene in every placement size
    video: CreativeVideo | None = None  # None when ffmpeg is not installed
    question_ids: list[str]  # ask/answer events this set relied on


class WeeklyReviewSummary(_Out):
    week: int
    stage: Literal["awaiting_plan_approval", "awaiting_change_approval", "applied", "escalated"]
    plan_id: str | None
    creative_ids: list[str]
    change_set_id: str | None
    escalations: list[str]
