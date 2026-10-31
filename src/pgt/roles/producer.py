"""Creative Producer: writes the copy for one planned test, three background scenes and the
storyboard for a 10-second video. Images and the video are made in code from Muse Image,
SAM 3.1 and the compositor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Tool
from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.llm.runtime import Scope, run_role
from pgt.schemas import CreativeCopy, PlanTest, SharedFacts

if TYPE_CHECKING:
    from pgt.sandbox.scenario import CreativeKit

INSTRUCTIONS = """\
You are the Creative Producer on a small AI growth team that runs Meta ads for one advertiser.
You write one ad for one planned test:
- a headline (40 characters at most), primary text (125 at most), a description (30 at most);
- three scenes, each a different setting for the same message, with a short angle (what the
  picture tries) and a prompt for Muse Image. A scene prompt describes the background only: no
  text, no logos, no product, no people's bodies (hands, paws and pets are fine if the kit allows
  them). Leave calm, empty space in the lower middle, where code places the product photo;
- a storyboard for a 10-second 9:16 video: hook (over scene 1), benefit (scene 2), proof
  (scene 3), each 34 characters at most, and the end-card button, one of the advertiser's
  cta_buttons.
Work from the creative kit: write for one of its personas at the moment they meet the ad, use its
season, look and props, and never use anything on its avoid list. Follow the brand voice and
constraints. List every factual statement in claims with its evidence IDs or source URLs. If you
need a number from the account, ask the analyst first; use only numbers that come back with
evidence or that the facts or the kit's offer state."""


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
        "kit's offer. There are exactly three scenes with different angles, each prompt a "
        "background only. The storyboard button is one of the kit's cta_buttons."
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
