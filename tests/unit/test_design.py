"""Layout code: every layout keeps its words readable, inside the safe zone and off the product,
and the design checks catch the cases it cannot fix."""

import asyncio
import base64
import io

import pytest
from PIL import Image, ImageDraw

from pgt.creative.compose import Box, make_demo_product_shot, png_bytes, product_box
from pgt.creative.design import MIN_CONTRAST, Style, compose, safe_rect
from pgt.harness.creative_checks import check_design
from pgt.llm.offline import offline_model
from pgt.sandbox.scenario import fitness_app_kit, pet_store_kit


def photo_with_product(busy: bool = False) -> tuple[Image.Image, Box]:
    """A 2:3 scene with a dark product in the upper middle, like a Muse shot."""
    image = Image.new("RGB", (1024, 1536), (214, 200, 178))
    draw = ImageDraw.Draw(image)
    if busy:  # light and dark stripes behind where words go
        for x in range(0, 1024, 24):
            draw.rectangle((x, 0, x + 11, 1536), fill=(250, 250, 250))
    box = Box(380, 420, 640, 900, 1024, 1536)
    draw.rectangle((box.x1, box.y1, box.x2, box.y2), fill=(30, 34, 40))
    return image, box


@pytest.mark.parametrize("kit", [fitness_app_kit(), pet_store_kit()], ids=["app", "store"])
@pytest.mark.parametrize("layout", ["band", "panel", "poster", "offer"])
@pytest.mark.parametrize("ratio", ["1:1", "4:5", "9:16"])
@pytest.mark.parametrize("busy", [False, True], ids=["calm", "busy"])
def test_layouts_pass_their_own_checks(kit, layout, ratio, busy):
    photo, box = photo_with_product(busy)
    style = Style.from_kit(kit, "Shop Now")
    design = compose(photo, box, ratio, layout, "The walk you both look forward to", style)

    assert design.image.size == {"1:1": (1080, 1080), "4:5": (1080, 1350),
                                 "9:16": (1080, 1920)}[ratio]
    assert design.contrast >= MIN_CONTRAST - 0.05
    left, top, right, bottom = safe_rect(ratio)
    for x1, y1, x2, y2 in design.text_boxes:
        assert left - 2 <= x1 and top - 2 <= y1 and x2 <= right + 2 and y2 <= bottom + 2
    assert check_design("harness", [(1, ratio, design)]) == []


def test_offer_layout_falls_back_without_an_offer():
    photo, box = photo_with_product()
    style = Style.from_kit(fitness_app_kit(), "Try It")
    no_offer = Style(**{**style.__dict__, "badge": ""})
    assert compose(photo, box, "1:1", "offer", "Ten minutes a day", no_offer).layout == "band"


def test_check_design_reports_missing_and_cut_off_products():
    photo, _ = photo_with_product()
    style = Style.from_kit(pet_store_kit(), "Shop Now")
    lost = compose(photo, None, "1:1", "band", "Made for the daily walk", style)
    issues = check_design("dog harness", [(2, "1:1", lost)])
    assert issues == [
        ('Scene 2: SAM 3.1 could not find the dog harness in the photo. Show "the dog harness '
         'from the reference photo" clearly, large enough to see.')
    ]

    edge = Box(0, 0, 1024, 1536, 1024, 1536)  # the "product" fills the frame and more
    cut = compose(photo, edge, "1:1", "band", "Made for the daily walk", style)
    assert any("cut off" in issue for issue in check_design("dog harness", [(1, "1:1", cut)]))


def test_offline_edit_puts_the_reference_product_in_the_scene():
    _, fake = offline_model()
    reference, _ = make_demo_product_shot(800)

    async def edit():
        result = await fake.client().images.edit(
            model="muse-image-1.0", prompt="A calm living room, the phone from @Image1", n=1,
            image=("product.png", png_bytes(reference), "image/png"), size="1024x1536",
            response_format="b64_json",
        )
        return Image.open(io.BytesIO(base64.b64decode(result.data[0].b64_json)))

    scene = asyncio.run(edit())
    assert scene.size == (1024, 1536)
    found = product_box(scene)
    assert (found.x2 - found.x1) * (found.y2 - found.y1) > 0.08 * found.w * found.h
    assert fake.violations == []
    assert fake.requests[-1]["prompt"].endswith("@Image1")
    assert "image" not in fake.requests[-1]
