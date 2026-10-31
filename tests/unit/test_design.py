"""Layout code: every layout keeps its words readable, inside the safe zone and off the product,
and the design checks catch the cases it cannot fix."""

import asyncio
import base64
import io

import pytest
from PIL import Image, ImageDraw

from pgt.creative.compose import Box, make_demo_product_shot, png_bytes, product_box
from pgt.creative.design import MIN_CONTRAST, Style, compose, covered, safe_rect
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


def test_a_tall_product_the_panel_would_cut_off_gets_another_layout():
    image = Image.new("RGB", (1024, 1536), (214, 200, 178))
    tall = Box(380, 330, 660, 1030, 1024, 1536)  # a fountain nearly half the shot's height
    ImageDraw.Draw(image).rectangle((tall.x1, tall.y1, tall.x2, tall.y2), fill=(30, 34, 40))
    style = Style.from_kit(pet_store_kit(), "Shop Now")
    design = compose(image, tall, "1:1", "panel", "Fresh water all day", style)
    assert design.layout != "panel"
    assert check_design("cat water fountain", [(3, "1:1", design)]) == []


def test_the_9x16_panel_keeps_the_photo_under_the_reels_buttons():
    photo = Image.new("RGB", (1024, 1536), (214, 200, 178))
    box = Box(420, 360, 600, 700, 1024, 1536)  # a phone in hand
    ImageDraw.Draw(photo).rectangle((box.x1, box.y1, box.x2, box.y2), fill=(30, 34, 40))
    style = Style.from_kit(fitness_app_kit(), "Try It")
    design = compose(photo, box, "9:16", "panel", "Ten minutes still counts", style)
    assert design.layout == "panel"
    lowest_row = design.image.crop((0, 1880, 1080, 1920)).getcolors(1080 * 40)
    assert not (len(lowest_row) == 1 and lowest_row[0][1] == style.panel)  # photo, not a block
    assert check_design("phone", [(1, "9:16", design)]) == []


def test_the_poster_sets_its_words_low_when_the_product_sits_high():
    image = Image.new("RGB", (1024, 1536), (214, 200, 178))
    high = Box(380, 120, 640, 640, 1024, 1536)
    ImageDraw.Draw(image).rectangle((high.x1, high.y1, high.x2, high.y2), fill=(30, 34, 40))
    style = Style.from_kit(fitness_app_kit(), "Try It")
    design = compose(image, high, "9:16", "poster", "Your streak, quietly kept going", style)
    head = design.text_boxes[0]
    assert head[1] > design.product_box[3]  # the headline starts below the product
    assert check_design("phone", [(1, "9:16", design)]) == []


@pytest.mark.parametrize("layout", ["band", "poster"])
def test_the_9x16_wordmark_and_button_stay_off_a_product_in_the_middle(layout):
    """The 9:16 safe zone ends two thirds down, right where the product usually is, so the
    wordmark and button go up with the headline instead of sitting on it."""
    image = Image.new("RGB", (1024, 1536), (214, 200, 178))
    middle = Box(380, 700, 640, 1150, 1024, 1536)
    ImageDraw.Draw(image).rectangle((middle.x1, middle.y1, middle.x2, middle.y2),
                                    fill=(30, 34, 40))
    style = Style.from_kit(pet_store_kit(), "Shop Now")
    design = compose(image, middle, "9:16", layout, "Calmer walks start here", style)
    assert covered(design.product_box, design.text_boxes) == 0
    assert all(box[3] < design.product_box[1] for box in design.text_boxes)  # all above it
