"""Video captions: a shot moves its words onto a brand strip when the product is too big to
clear them, the whole video then keeps that style, and the caption check catches the rest."""

from PIL import Image, ImageDraw

from pgt.creative.compose import Box
from pgt.creative.design import Style
from pgt.creative.video import Cut
from pgt.harness.creative_checks import check_captions
from pgt.sandbox.scenario import fitness_app_kit
from pgt.schemas import Storyboard


def scene(box: Box) -> Image.Image:
    image = Image.new("RGB", (box.w, box.h), (214, 200, 178))
    ImageDraw.Draw(image).rectangle((box.x1, box.y1, box.x2, box.y2), fill=(30, 34, 40))
    return image


STORY = Storyboard(hook="Ten minutes still counts", benefit="No gear, no gym",
                   proof="Keep the streak going", cta="Try It")


def cut(boxes: list[Box]) -> Cut:
    style = Style.from_kit(fitness_app_kit(), "Try It")
    product = Image.new("RGBA", (400, 400), (30, 34, 40, 255))
    return Cut([scene(box) for box in boxes], boxes, product, STORY, "Ten minutes", style)


def test_a_small_product_keeps_its_captions_on_the_photo():
    small = Box(500, 640, 780, 1000, 1280, 1920)
    video = cut([small])
    assert not any(shot.strip for shot in video.shots)
    assert max(video.covered()) <= 0.02


def test_a_big_product_moves_every_caption_onto_the_strip():
    small = Box(500, 640, 780, 1000, 1280, 1920)
    big = Box(240, 300, 1040, 1560, 1280, 1920)  # two thirds of the frame's height
    video = cut([small, big])
    assert all(shot.strip for shot in video.shots)  # one caption style for the whole video
    assert max(video.covered()) <= 0.02
    frame = video.keyframes()[1]
    assert frame.getpixel((540, 20))[:3] == video.style.panel  # the strip, not the photo


def test_check_captions_flags_only_covered_scenes():
    assert check_captions("phone", [(1, 0.0), (2, 0.04), (3, 0.0)]) == []
    issues = check_captions("phone", [(1, 0.0), (2, 0.3), (1, 0.0)])
    assert len(issues) == 1 and issues[0].startswith("Video, scene 2:")
