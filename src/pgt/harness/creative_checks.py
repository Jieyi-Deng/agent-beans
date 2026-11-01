"""Deterministic checks for weekly-review deliverables."""

import hashlib
import re
from collections import Counter
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image

from pgt.harness.ledger import EvidenceLedger
from pgt.harness.verify import number_matches
from pgt.schemas.weekly import (
    CREATIVE_SIZES,
    Answer,
    CreativeCopy,
    CreativeImage,
    CreativeVideo,
    MarketResearch,
    TestPlanDraft,
)

COPY_LIMITS: dict[str, int] = {"headline": 40, "primary_text": 125, "description": 30}
# Words on screen in the 10-second video: each line is read in under three seconds.
VIDEO_LIMITS: dict[str, int] = {"hook": 34, "benefit": 34, "proof": 34, "cta": 20}
SCENES = 3
VARIANTS = SCENES
WORDS_LIMIT = 40  # the line set on each picture
OWN_FACTS = "facts"  # the evidence ID for what the advertiser's own facts and kit state
# The share of the product the words may sit on, and the share that must stay in the frame.
MAX_COVERED = 0.15  # share of the product the words may sit on before the shot goes back
MIN_SHOWN = 0.85

_NUMBER = re.compile(
    r"(?<![\w.#])(?P<rank>#)?"
    r"(?P<value>\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
    r"(?:(?P<ordinal>st|nd|rd|th)\b|(?P<scale>[kmM])(?![a-zA-Z]))?"
)


def extract_numbers(text: str) -> list[float]:
    """Read numeric claims, excluding ranks and ordinals."""
    numbers = []
    for match in _NUMBER.finditer(text):
        if match["rank"] or match["ordinal"]:
            continue
        value = float(match["value"].replace(",", ""))
        scale = match["scale"]
        if scale:
            value *= 1000 if scale == "k" else 1_000_000
        numbers.append(value)
    return numbers


def allocate_budget(weights: list[int], total: float) -> list[float]:
    """Allocate cents proportionally, leaving the rounding remainder to the last test."""
    if not weights or any(weight < 1 for weight in weights):
        raise ValueError("Budget weights must be non-empty and each at least 1.")
    amount = Decimal(str(total))
    weight_sum = sum(weights)
    parts = [
        (amount * weight / weight_sum).quantize(Decimal("0.01")) for weight in weights[:-1]
    ]
    parts.append(Decimal(str(round(total, 2))) - sum(parts, Decimal(0)))
    return [float(part) for part in parts]


def check_copy(
    ad_copy: CreativeCopy,
    ledger: EvidenceLedger,
    fact_numbers: list[float],
    known_urls: set[str],
    cta_buttons: list[str] | None = None,
) -> list[str]:
    """Check copy lengths, the three scenes, the storyboard, claim citations and numeric
    support. Numbers in the video count like numbers in the ad text."""
    issues = []
    for field, limit in COPY_LIMITS.items():
        length = len(getattr(ad_copy, field))
        if length > limit:
            issues.append(f"{field} is {length} characters; the limit is {limit}.")

    if len(ad_copy.scenes) != SCENES:
        issues.append(f"The ad has {len(ad_copy.scenes)} scene(s); it needs {SCENES}.")
    for index, scene in enumerate(ad_copy.scenes):
        if not scene.prompt.strip() or not scene.angle.strip():
            issues.append(f"Scene {index + 1} needs an angle and a prompt.")
    angles = [scene.angle.strip().lower() for scene in ad_copy.scenes]
    if len(set(angles)) != len(angles):
        issues.append("Each scene needs a different angle.")
    words = [scene.words.strip() for scene in ad_copy.scenes]
    for index, line in enumerate(words):
        if not line:
            issues.append(f"Scene {index + 1} needs words: the line set on its picture.")
        elif len(line) > WORDS_LIMIT:
            issues.append(f"Scene {index + 1} words are {len(line)} characters; the limit is "
                          f"{WORDS_LIMIT}.")
    if len({w.lower() for w in words if w}) < len([w for w in words if w]):
        issues.append("Each scene needs different words.")
    if len(ad_copy.scenes) == SCENES and len({scene.layout for scene in ad_copy.scenes}) < 2:
        issues.append("Use at least two different layouts across the three scenes.")
    for field, limit in VIDEO_LIMITS.items():
        text = getattr(ad_copy.video, field)
        if not text.strip():
            issues.append(f"Video {field} line is empty.")
        elif len(text) > limit:
            issues.append(f"Video {field} line is {len(text)} characters; the limit is {limit}.")
    if cta_buttons and ad_copy.video.cta not in cta_buttons:
        issues.append(f"Video button {ad_copy.video.cta!r} is not one of the advertiser's: "
                      f"{', '.join(cta_buttons)}.")

    candidates = list(fact_numbers)
    for index, claim in enumerate(ad_copy.claims):
        if not claim.evidence_ids and not claim.source_urls:
            issues.append(f"Claim {index} has no evidence IDs or source URLs; cite "
                          f"\"{OWN_FACTS}\" when the shared facts or the kit state it.")
        for evidence_id in claim.evidence_ids:
            if evidence_id == OWN_FACTS:  # numbers in it still have to match the facts
                continue
            evidence = ledger.get(evidence_id)
            if evidence is None:
                issues.append(f"Claim {index} cites unknown evidence {evidence_id}.")
            else:
                candidates.extend(abs(value) for value in evidence.values.values())
        for url in claim.source_urls:
            if url not in known_urls:
                issues.append(
                    f"Claim {index} cites a source URL that research did not find: {url}."
                )

    texts = {field: getattr(ad_copy, field) for field in COPY_LIMITS}
    texts |= {f"video {field}": getattr(ad_copy.video, field) for field in VIDEO_LIMITS}
    texts |= {f"scene {i + 1} words": scene.words for i, scene in enumerate(ad_copy.scenes)}
    for field, text in texts.items():
        for value in extract_numbers(text):
            if not number_matches(value, candidates):
                issues.append(
                    f"Number {value:g} in {field} matches no cited evidence or shared fact."
                )
    return issues


def _check_image_size(ratio: str, width: int, height: int, issues: list[str]) -> None:
    expected_width, expected_height = CREATIVE_SIZES[ratio]
    if (width, height) != (expected_width, expected_height):
        issue = (
            f"Image {ratio} is {width}x{height}; "
            f"expected {expected_width}x{expected_height}."
        )
        if issue not in issues:
            issues.append(issue)


def check_images(images: list[CreativeImage], variants: int = VARIANTS) -> list[str]:
    """Check that every scene has every placement size, the dimensions, and the actual local
    image bytes."""
    issues = []
    counts = Counter((image.variant, image.ratio) for image in images)
    for variant in range(1, variants + 1):
        for ratio in CREATIVE_SIZES:
            name = f"scene {variant} {ratio}" if variants > 1 else ratio
            if counts[(variant, ratio)] == 0:
                issues.append(f"Missing image for {name}.")
            elif counts[(variant, ratio)] > 1:
                issues.append(f"Duplicate image for {name}.")

    for image in images:
        ratio = image.ratio
        _check_image_size(ratio, image.width, image.height, issues)
        try:
            data = Path(image.path).read_bytes()
        except FileNotFoundError:
            issues.append(f"Image {ratio} file is missing.")
            continue
        except OSError:
            issues.append(f"Image {ratio} file cannot be read.")
            continue

        if hashlib.sha256(data).hexdigest() != image.sha256:
            issues.append(f"Image {ratio} file does not match its sha256.")
        try:
            with Image.open(BytesIO(data)) as pixels:
                _check_image_size(ratio, *pixels.size, issues)
                pixels.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            issues.append(f"Image {ratio} file cannot be read as an image.")
    return issues


def check_video(video: CreativeVideo | None) -> list[str]:
    """Check the video file is there, unchanged, 9:16, and ten seconds long. No video (ffmpeg
    missing) is not an issue: the ad still has its images."""
    if video is None:
        return []
    issues = []
    if (video.width, video.height) != CREATIVE_SIZES["9:16"]:
        issues.append(f"Video is {video.width}x{video.height}; expected 1080x1920.")
    if abs(video.seconds - 10) > 0.1:
        issues.append(f"Video is {video.seconds:g} seconds; expected 10.")
    try:
        data = Path(video.path).read_bytes()
    except OSError:
        return [*issues, "Video file is missing."]
    if hashlib.sha256(data).hexdigest() != video.sha256:
        issues.append("Video file does not match its sha256.")
    if data[4:8] != b"ftyp":
        issues.append("Video file is not an MP4.")
    return issues


def check_plan(
    draft: TestPlanDraft,
    *,
    reviewed_evidence_ids: set[str],
    insight_ids: set[str],
    active_campaign_ids: set[str],
    ad_ids: set[str],
    products: set[str] | None = None,
    min_tests: int = 2,
    max_tests: int = 3,
) -> list[str]:
    """Check test coverage and references to this week's reviewed inputs."""
    issues = []
    count = len(draft.tests)
    if not min_tests <= count <= max_tests:
        issues.append(f"The plan has {count} test(s); it needs {min_tests} to {max_tests}.")
    if len({test.name for test in draft.tests}) != count:
        issues.append("Test names must be unique.")
    exploratory = sum(test.exploratory for test in draft.tests)
    if count >= 2 and exploratory != 1:
        # Keeps the plan from shrinking to fixes only: one test always tries something new.
        issues.append(f"The plan marks {exploratory} test(s) exploratory; mark exactly one, a "
                      "test that tries a new angle rather than fixing a finding.")

    for index, test in enumerate(draft.tests):
        label = f"Test {index}"
        if not 1 <= test.budget_weight <= 5:
            issues.append(f"{label} budget_weight {test.budget_weight} is outside 1-5.")
        if not test.evidence_ids:
            issues.append(f"{label} cites no evidence.")
        for evidence_id in test.evidence_ids:
            if evidence_id not in reviewed_evidence_ids:
                issues.append(
                    f"{label} cites evidence {evidence_id} "
                    "that is not in this week's reviewed findings."
                )
        if not test.brief.insight_ids:
            issues.append(f"{label} has no insight IDs.")
        for insight_id in test.brief.insight_ids:
            if insight_id not in insight_ids:
                issues.append(f"{label} cites unknown insight {insight_id}.")
        if test.campaign_id not in active_campaign_ids:
            issues.append(f"{label} uses campaign {test.campaign_id}, which is not active.")
        replaces = test.brief.replaces_ad_id
        if replaces is not None and replaces not in ad_ids:
            issues.append(f"{label} replaces unknown ad {replaces}.")
        if products is not None and test.brief.product not in products:
            issues.append(f"{label} shows {test.brief.product!r}, which has no photo on file; "
                          f"pick one of {', '.join(sorted(products))}.")
    return issues


def check_research(research: MarketResearch) -> list[str]:
    """Require complete insights and an HTTP source for each market claim."""
    issues = []
    if not research.insights:
        issues.append("Research has no insights.")
    for index, insight in enumerate(research.insights):
        for field in ("claim", "angle", "hook"):
            if not getattr(insight, field).strip():
                issues.append(f"Insight {index} is missing {field}.")
        if not any(source.url.startswith(("http://", "https://")) for source in insight.sources):
            issues.append(f"Insight {index} has no http(s) source.")
    return issues


def check_answer(answer: Answer, ledger: EvidenceLedger) -> list[str]:
    """Check that answers cite known evidence supporting every number."""
    issues = []
    if not answer.evidence_ids:
        issues.append("The answer cites no evidence.")
    candidates = []
    for evidence_id in answer.evidence_ids:
        evidence = ledger.get(evidence_id)
        if evidence is None:
            issues.append(f"The answer cites unknown evidence {evidence_id}.")
        else:
            candidates.extend(abs(value) for value in evidence.values.values())
    for value in extract_numbers(answer.answer):
        if not number_matches(value, candidates):
            issues.append(f"Number {value:g} in the answer matches no cited evidence.")
    return issues


def check_design(product: str, designs) -> list[str]:
    """Check the laid-out pictures: the product was found and is in the frame, the words keep
    off it, read at 4.5:1 and stay inside the safe zone. `designs` is a list of
    (scene number, ratio, Design); a Design's product_box is None when SAM found nothing."""
    from pgt.creative.design import MIN_CONTRAST, safe_rect

    issues: list[str] = []
    missing = sorted({n for n, _, d in designs if d.product_box is None})
    for n in missing:
        issues.append(f"Scene {n}: SAM 3.1 could not find the {product} in the photo. Show "
                      f"\"the {product} from the reference photo\" clearly, large enough to see.")
    for n, ratio, design in designs:
        if n in missing:
            continue
        where = f"Scene {n} ({ratio}, {design.layout})"
        if design.product_shown < MIN_SHOWN:
            issues.append(f"{where}: the {product} is cut off at the edge of the picture; frame "
                          "it with room around it.")
        elif design.layout != "panel" and _covered(design) > MAX_COVERED:
            calm = "top third" if design.layout == "poster" else "lower third"
            issues.append(f"{where}: the words sit on the {product}. Keep the {calm} of the "
                          "photo calm and the product out of it, show it smaller (about a third "
                          "of the frame's height), or choose another layout.")
        if design.contrast < MIN_CONTRAST - 0.05:
            issues.append(f"{where}: the words read at only {design.contrast:.1f}:1 against the "
                          "photo; ask for a calmer, simpler backdrop where they sit.")
        left, top, right, bottom = safe_rect(ratio)
        for box in design.text_boxes:
            if box[0] < left - 2 or box[1] < top - 2 or box[2] > right + 2 or box[3] > bottom + 2:
                issues.append(f"{where}: words fall outside the safe zone.")
                break
    # One line per problem, not one per placement size.
    return list(dict.fromkeys(issues))


def check_captions(product: str, captions: list[tuple[int, float]]) -> list[str]:
    """Check the video: each caption keeps off the product. `captions` is a list of
    (scene number, share of the product the caption sits on), one per shot."""
    issues = [f"Video, scene {n}: the caption sits on the {product}. Show it smaller in that "
              "scene (about a third of the frame's height), with calm space above it."
              for n, share in captions if share > MAX_COVERED]
    return list(dict.fromkeys(issues))


def _covered(design) -> float:
    from pgt.creative.design import covered

    return covered(design.product_box, design.text_boxes)

