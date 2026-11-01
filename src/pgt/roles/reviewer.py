"""Reviewer: checks deliverables before anything reaches the user or the account.

Code checks come first (evidence, numbers, lengths, image files, the readout rule). Creative work
then gets a model review of what code cannot judge: tone, brand voice, risky claims, and whether
the image and words fit together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.messages import BinaryContent
from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.harness.ledger import EvidenceLedger
from pgt.harness.memory import period_of
from pgt.harness.verify import check_changes, check_findings
from pgt.llm.runtime import Scope, run_role
from pgt.roles.metrics import expected_readout
from pgt.roles.producer import creative_context
from pgt.schemas import (
    Change,
    CreativeCopy,
    CreativeReview,
    Finding,
    PlanTest,
    ReviewVerdict,
    SharedFacts,
    TestReadout,
)

if TYPE_CHECKING:
    from pgt.sandbox.scenario import CreativeKit

CREATIVE_INSTRUCTIONS = """\
You are the Reviewer on a small AI growth team that runs Meta ads for one advertiser. You review
an ad the way a creative director signs off work before it goes live.
Code has already checked lengths, numbers, evidence, contrast, safe zones, that the words keep
off the product and that the product is in frame. You judge what code cannot: does the ad follow
the brand voice and every constraint in the facts, does it avoid claims that need proof (being #1
or the best, guaranteed results, weight loss or other health outcomes), and does it match its
test brief.
How the ad is made: the Producer pitches one concept and three shots. Muse Image shoots each scene
with the owner's real product photo as its reference, SAM 3.1 finds the product in the result,
and layout code sets the brand fonts, the scene's words, the wordmark and the button in the
layout the Producer chose (band, panel, poster or offer). The video is cut in code from the same
three shots with slow camera moves and animated words, then an end card with the product, the
offer and the button. The layouts, fades and end card are the house design, not faults.
Code sets every overlay word exactly as the copy spells it, so a spelling or punctuation slip you
seem to see in an overlay is the type at small size, not a fault: check those words in the copy,
and report overlay text only when it is hard to read or in the way.
Look at the attached sheet and send the ad back only for something the Producer can fix:
- the product in a scene is not the one in the reference photo (other shape, colour or parts),
  or a scene shows more than one of it;
- visible AI faults: warped hands, faces or paws, melted objects, gibberish text or logos in
  the photo itself;
- words that are hard to read, or words, the button or the offer badge covering a face or the
  product;
- anything off the creative kit (persona, season, look) or on its avoid list;
- three shots that do not tell one story, or a storyboard that does not follow them.
List each fix as one plain line that says which scene and what to change. A claim that cites
evidence IDs or a source URL has already passed the evidence check."""


def review_findings(findings: list[Finding], ledger: EvidenceLedger) -> list[str]:
    return check_findings(findings, ledger)


def review_readout(readout: TestReadout, ledger: EvidenceLedger, evidence_id: str) -> list[str]:
    issues = check_findings(readout.findings, ledger)
    evidence = ledger.get(evidence_id)
    if evidence is None:
        return [*issues, f"Readout evidence {evidence_id} is missing."]
    expected = expected_readout(evidence.values)
    if readout.recommendation != expected:
        issues.append(
            f"Readout recommends {readout.recommendation}, but the readout rule gives {expected}."
        )
    return issues


def review_changes(changes: list[Change], ledger: EvidenceLedger) -> list[str]:
    issues = check_changes(changes, ledger)
    for index, change in enumerate(changes):
        if change.kind != "set_budget" and change.new_daily_budget is not None:
            issues.append(f"Change {index} ({change.kind}) must not set new_daily_budget.")
    return issues


def verdict(subject_id: str, issues: list[str], final: bool) -> ReviewVerdict:
    if not issues:
        return ReviewVerdict(subject_id=subject_id, verdict="pass", issues=[])
    return ReviewVerdict(subject_id=subject_id, verdict="block" if final else "redo", issues=issues)


async def review_creative(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    test: PlanTest,
    ad_copy: CreativeCopy,
    image_png: bytes,
    kit: CreativeKit | None = None,
) -> CreativeReview:
    """Model review of one creative set. A contact sheet is attached so the model sees it: the
    square image of each scene on top, the video's four shots in order below."""
    data = {"brief": test.brief.model_dump(mode="json"), "ad": ad_copy.model_dump(mode="json")}
    if kit is not None:
        data["creative_kit"] = creative_context(kit, test.brief.product)
    pack = assemble(
        "reviewer",
        CREATIVE_INSTRUCTIONS,
        facts,
        fact_version,
        topic=f"{period_of(scope.loop_id, scope.sim_day)}/review/{test.test_id}",
        task=f"Review the ad written for test {test.test_id} ({test.name}). The attached sheet "
        "shows, on top, the owner's product photo first and then the 1:1 image of each of the "
        "three scenes; below, the four shots of its 10-second video (hook, benefit, proof, end "
        "card).",
        done_when="verdict is pass when nothing needs fixing, with no issues; otherwise redo with "
        "one line per required fix.",
        data=data,
        inputs=test.evidence_ids,
    )
    image = BinaryContent(image_png, media_type="image/png")
    return await run_role("reviewer", CreativeReview, pack, scope, model, attachments=[image])
