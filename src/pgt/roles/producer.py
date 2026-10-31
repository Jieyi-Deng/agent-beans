"""Creative Producer: art-directs one ad for a planned test: the concept, the copy, three shots
and the storyboard for a 10-second video. Muse Image renders each shot around the real product
photo, SAM 3.1 finds the product, and layout code sets the brand's type."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Tool
from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.llm.runtime import Scope, run_role
from pgt.schemas import CreativeCopy, PlanTest, Scene, SharedFacts

if TYPE_CHECKING:
    from pgt.sandbox.scenario import CreativeKit

INSTRUCTIONS = """\
You are the Creative Producer on a small AI growth team that runs Meta ads for one advertiser.
Work like an art director: one clear idea, shot and set so a marketer would run it as is.

Write one ad for one planned test:
- concept: the one idea behind the ad in a sentence, as you would pitch it, grounded in one of the
  kit's personas at the moment they meet the ad and in the brief's angle;
- headline (40 characters at most), primary text (125 at most), description (30 at most);
- three scenes that tell the idea in order: 1 the moment (the persona's situation, the product in
  it), 2 the detail (a close look at the feature that answers their doubt), 3 the payoff (life with
  the product, the result). For each scene:
  - angle: a few words on what the picture tries;
  - prompt: the photograph for Muse Image, which receives the real product photo as a reference.
    Write it as a photographer's shot note: subject and action, where (a setting from the kit's
    look and props), camera (lens, distance, angle), light, palette and mood. Put "the <product>
    from the reference photo" in the picture, used as the product card's how_to_show says, and
    exactly one of it. No text, signs, logos or screens with words; nothing on the avoid list;
  - layout: how code sets the words over the photo. "band": a headline band along the bottom
    edge; "panel": a solid brand-colour panel below the photo; "poster": large type over a calm
    top third; "offer": band plus a badge with the kit's offer_badge. Use at least two different
    layouts, and "offer" only if the kit has an offer;
  - words: the line set on this picture, 40 characters at most, different for each scene;
- a storyboard for the 10-second 9:16 video: hook (over scene 1), benefit (scene 2), proof
  (scene 3), each 34 characters at most, and the end-card button, one of the kit's cta_buttons.
Short, concrete, specific words beat clever ones. Follow the brand voice and constraints. List
every factual statement in claims with its evidence IDs or source URLs. If you need a number from
the account, ask the analyst first; use only numbers that come back with evidence or that the
facts or the kit's offer state."""


async def run_producer(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    test: PlanTest,
    insights: dict[str, dict],
    ask: Tool,
    issues: list[str] = (),
    kit: CreativeKit | None = None,
) -> CreativeCopy:
    data = {
        "test": test.model_dump(mode="json"),
        "insights": {i: insights[i] for i in test.brief.insight_ids if i in insights},
    }
    if kit is not None:
        data["creative_kit"] = creative_context(kit, test.brief.product)
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "producer",
        INSTRUCTIONS,
        facts,
        fact_version,
        task=f"Write the ad for test {test.test_id} ({test.name}) from its brief.",
        done_when="Lengths are within limits. Every number in the copy and the storyboard is "
        "backed by a claim whose evidence or source supports it, or stated in the facts or the "
        "kit's offer. There are exactly three scenes with different angles and words, each "
        "prompt a photograph with the product from the reference in it, and at least two "
        "different layouts. The storyboard button is one of the kit's cta_buttons."
        + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=test.evidence_ids,
    )
    return await run_role("producer", CreativeCopy, pack, scope, model, [ask])


def creative_context(kit: CreativeKit, product: str) -> dict:
    """The kit as the Producer and Reviewer see it, with the card of the product this test
    shows first."""
    context = kit.model_dump(mode="json")
    cards = sorted(kit.products, key=lambda card: card.name != product)
    context["products"] = [card.model_dump(mode="json") for card in cards]
    return context


# Where each layout sets its words, so the photograph leaves that part calm.
COMPOSITION = {
    "band": "Keep the bottom third of the frame calm and simple (floor, table, wall or path) "
    "with the subject and the product in the upper two thirds.",
    "offer": "Keep the bottom third of the frame calm and simple (floor, table, wall or path) "
    "with the subject and the product in the upper two thirds.",
    "panel": "Frame the subject a little above centre with room around it on every side.",
    "poster": "Keep the top third of the frame calm and simple (sky, wall or soft background) "
    "with the subject and the product in the lower half.",
}


def shot_prompt(scene: Scene, product: str, kit: CreativeKit | None) -> str:
    """The Muse Image prompt for one shot: the Producer's shot note plus what code always asks
    for. The product photo goes with it as @Image1."""
    parts = [
        f"Photograph for a {kit.brand} ad." if kit else "Photograph for an ad.",
        scene.prompt.strip(),
        (f"The {product} in this photo is the one in @Image1: keep its exact shape, colours, "
         "materials and details, and show exactly one."),
        COMPOSITION.get(scene.layout, COMPOSITION["band"]),
    ]
    if kit:
        parts.append(f"Look: {kit.look}.")
        parts.append("Avoid: " + "; ".join(kit.avoid) + ".")
    parts.append("Photorealistic, natural colour, sharp focus on the product. No text, letters, "
                 "logos or watermarks anywhere in the image.")
    return " ".join(parts)
