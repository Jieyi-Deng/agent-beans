"""Deterministic product cutouts and placement images built with Pillow."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageMath, ImageOps

from pgt.schemas.weekly import CREATIVE_SIZES

FONTS = Path(__file__).with_name("fonts")  # Liberation Sans, SIL Open Font License 1.1
INK = (15, 23, 30)

SAFE_MARGINS: dict[str, tuple[int, int, int, int]] = {
    "1:1": (60, 60, 60, 60),
    "4:5": (60, 60, 60, 60),
    "9:16": (270, 65, 670, 65),
}


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    w: int
    h: int


@dataclass(frozen=True)
class Rendered:
    image: Image.Image
    product_box: tuple[int, int, int, int]
    text_box: tuple[int, int, int, int]


def parse_sam_output(text: str) -> list[Box]:
    """Read box tokens independently of SAM's other output."""
    boxes = []
    for token in re.findall(r"<\|box;([^|]*)\|>", text):
        fields = dict(re.findall(r"(\w+)\s*=\s*([^;]+)", token))
        try:
            box = Box(*(int(fields[key]) for key in ("x1", "y1", "x2", "y2", "w", "h")))
        except (KeyError, ValueError) as exc:
            raise ValueError("Box must contain integer x1, y1, x2, y2, w and h") from exc
        if box.x2 <= box.x1 or box.y2 <= box.y1 or box.w <= 0 or box.h <= 0:
            raise ValueError("Box and reference frame must have positive dimensions")
        boxes.append(box)
    if not boxes:
        raise ValueError("No SAM box tokens found")
    return boxes


def cutout(image: Image.Image, box: Box, radius_ratio: float = 0.08) -> Image.Image:
    """Scale a detection to the image and crop it with rounded corners."""
    if box.w <= 0 or box.h <= 0:
        raise ValueError("Box reference frame must have positive dimensions")
    if not 0 <= radius_ratio <= 0.5:
        raise ValueError("Corner radius ratio must be between 0 and 0.5")
    x1, x2 = (max(0, min(image.width, round(x * image.width / box.w)))
              for x in (box.x1, box.x2))
    y1, y2 = (max(0, min(image.height, round(y * image.height / box.h)))
              for y in (box.y1, box.y2))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("Box has no area inside the image")
    product = image.crop((x1, y1, x2, y2)).convert("RGBA")
    mask = Image.new("L", product.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, product.width - 1, product.height - 1),
        radius=round(radius_ratio * min(product.size)),
        fill=255,
    )
    product.putalpha(mask)
    return product


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    name = "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
    return ImageFont.truetype(str(FONTS / name), max(1, size))


def _studio_color(image: Image.Image) -> tuple[int, int, int]:
    """The plain backdrop of a studio photo: the median colour of its border."""
    rgb = image.convert("RGB")
    small = rgb.resize((128, max(1, round(128 * rgb.height / rgb.width))))
    w, h = small.size
    border = [small.getpixel((x, y)) for x in range(w) for y in (0, h - 1)] + \
             [small.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
    return tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))


def _backdrop_like(crop: Image.Image, backdrop: tuple[int, int, int]) -> Image.Image:
    """255 where a pixel is the backdrop or a shadow on it: the backdrop colour scaled darker
    (or a touch brighter) without turning cooler. A white product on a cream backdrop is
    close in brightness but cooler, so it stays."""
    total = sum(backdrop)
    br, bg, bb = backdrop
    r, g, b = (band.convert("F") for band in crop.split())

    def test(a):
        r, g, b = a["r"], a["g"], a["b"]
        k = (r + g + b) / total
        dr, dg, db = r - k * br, g - k * bg, b - k * bb
        spread = a["max"](a["max"](abs(dr), abs(dg)), abs(db))
        return ((dr - db) > -4) * (dg < 4) * (spread < 13) * (k > 0.45) * (k < 1.08) * 255

    out = ImageMath.lambda_eval(test, r=r, g=g, b=b)
    return out.convert("L")


def _main_piece(mask: Image.Image) -> Image.Image:
    """Keep the largest solid piece of a 0/255 mask; specks of backdrop are dropped."""
    small = mask.resize((max(1, mask.width // 4), max(1, mask.height // 4)), Image.Resampling.NEAREST)
    best, seen = None, small.copy()
    for fy in (0.5, 0.4, 0.6, 0.3, 0.7):
        for fx in (0.5, 0.35, 0.65):
            xy = (int(small.width * fx), int(small.height * fy))
            if seen.getpixel(xy) != 255:
                continue
            piece = small.copy()
            ImageDraw.floodfill(piece, xy, 128)
            piece = piece.point(lambda v: 255 if v == 128 else 0)
            area = piece.histogram()[255]
            ImageDraw.floodfill(seen, xy, 0)
            if best is None or area > best[0]:
                best = (area, piece)
    if best is None:
        return mask
    keep = best[1].filter(ImageFilter.MaxFilter(5)).resize(mask.size, Image.Resampling.NEAREST)
    return ImageChops.multiply(mask, keep)


def _outline(crop: Image.Image, step: int = 10) -> Image.Image:
    """255 on sharp brightness steps. A product's outline is sharp; a shadow fades softly,
    so this keeps the flood out of a shaded product that is shadow-coloured."""
    lum = crop.convert("L")
    grad = ImageChops.lighter(
        ImageChops.difference(lum, ImageChops.offset(lum, 2, 0)),
        ImageChops.difference(lum, ImageChops.offset(lum, 0, 2)),
    )
    edge = grad.point(lambda v: 255 if v >= step else 0)
    frame = Image.new("L", crop.size, 0)
    frame.paste(edge.crop((2, 2, crop.width - 2, crop.height - 2)), (2, 2))
    return frame.filter(ImageFilter.MaxFilter(7))


def matte(image: Image.Image, box: Box) -> Image.Image:
    """Cut the product out of a studio photo along its own edges.

    SAM 3.1 gives the box. Inside it, backdrop and shadow pixels that join the border are
    removed; what is left keeps its pixels, with a soft edge."""
    if box.w <= 0 or box.h <= 0:
        raise ValueError("Box reference frame must have positive dimensions")
    pad_x = round(0.03 * (box.x2 - box.x1) * image.width / box.w)
    pad_y = round(0.03 * (box.y2 - box.y1) * image.height / box.h)
    x1 = max(0, round(box.x1 * image.width / box.w) - pad_x)
    x2 = min(image.width, round(box.x2 * image.width / box.w) + pad_x)
    y1 = max(0, round(box.y1 * image.height / box.h) - pad_y)
    y2 = min(image.height, round(box.y2 * image.height / box.h) + pad_y)
    if x2 <= x1 or y2 <= y1:
        raise ValueError("Box has no area inside the image")
    crop = image.convert("RGB").crop((x1, y1, x2, y2))
    smooth = crop.filter(ImageFilter.MedianFilter(3))
    like = ImageChops.subtract(_backdrop_like(smooth, _studio_color(image)), _outline(smooth))
    # A one-pixel frame joins every border pixel to the flood seed.
    near = Image.new("L", (crop.width + 2, crop.height + 2), 255)
    near.paste(like, (1, 1))
    ImageDraw.floodfill(near, (0, 0), 128)
    outside = near.crop((1, 1, crop.width + 1, crop.height + 1)).point(
        lambda v: 255 if v == 128 else 0)
    alpha = _main_piece(ImageChops.invert(outside).filter(ImageFilter.MedianFilter(5)))
    # Opening drops thin bits of backdrop stuck to the edge, then the edge is pulled in a little.
    alpha = alpha.filter(ImageFilter.MinFilter(11)).filter(ImageFilter.MaxFilter(11))
    alpha = _main_piece(alpha.filter(ImageFilter.MinFilter(5))).filter(ImageFilter.GaussianBlur(1.2))
    found = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    if found is None:
        raise ValueError("Nothing in the box stands out from the backdrop")
    product = crop.convert("RGBA")
    product.putalpha(alpha)
    return product.crop(found)


def _shadow(product: Image.Image, opacity: float = 0.42) -> tuple[Image.Image, tuple[int, int]]:
    """A soft drop shadow for a cutout and where it sits relative to the cutout's corner."""
    blur = max(2, round(0.035 * product.height))
    pad = blur * 3
    alpha = product.getchannel("A").point(lambda v: round(v * opacity))
    layer = Image.new("L", (product.width + 2 * pad, product.height + 2 * pad), 0)
    layer.paste(alpha, (pad, pad))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(layer)
    return shadow, (-pad, -pad + round(0.03 * product.height))


def place_product(canvas: Image.Image, product: Image.Image, xy: tuple[int, int]) -> None:
    """Paste a cutout with its shadow onto an RGBA canvas, in place."""
    shadow, (dx, dy) = _shadow(product)
    canvas.alpha_composite(shadow, (xy[0] + dx, xy[1] + dy))
    canvas.alpha_composite(product, xy)


def scrim(size: tuple[int, int], depth: int, ink: tuple[int, int, int] = INK,
          strength: int = 200, bottom: bool = False) -> Image.Image:
    """A dark fade from one edge, so white words read on any background."""
    _, height = size
    fade = Image.new("L", (1, height), 0)
    for y in range(min(depth, height)):
        t = 1 - y / max(1, depth)
        fade.putpixel((0, height - 1 - y if bottom else y), round(strength * t ** 1.4))
    layer = Image.new("RGBA", size, (*ink, 0))
    layer.putalpha(fade.resize(size))
    return layer


def product_box(image: Image.Image, threshold: int = 40) -> Box:
    """Where the product sits in a studio photo: everything that differs from the border colour.

    Only the offline SAM stand-in uses this; live runs ask SAM 3.1.
    """
    rgb = image.convert("RGB")
    small = rgb.resize((256, round(256 * rgb.height / rgb.width)))
    w, h = small.size
    border = [small.getpixel((x, y)) for x in range(w) for y in (0, h - 1)] + \
             [small.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    r, g, b = ImageChops.difference(small, Image.new("RGB", small.size, bg)).split()
    diff = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mask = diff.point(lambda v: 255 if v > threshold else 0).filter(ImageFilter.MedianFilter(5))
    found = mask.getbbox() or (0, 0, w, h)
    sx, sy = rgb.width / w, rgb.height / h
    x1, y1 = round(found[0] * sx), round(found[1] * sy)
    x2, y2 = round(found[2] * sx), round(found[3] * sy)
    return Box(x1, y1, x2, y2, rgb.width, rgb.height)


def make_demo_product_shot(size: int = 1600) -> tuple[Image.Image, Box]:
    """Draw a synthetic Pallas Fit phone on a plain product-shot background."""
    if size < 32:
        raise ValueError("Product shot size must be at least 32 pixels")
    image = Image.new("RGB", (size, size), (242, 240, 234))
    width, height = round(size * 0.38), round(size * 0.74)
    x1, y1 = (size - width) // 2, (size - height) // 2
    box = Box(x1, y1, x1 + width, y1 + height, size, size)
    radius = round(width * 0.13)
    # A soft shadow, side buttons, a metal rim, a camera and a status bar: SAM 3.1 does not
    # see a flat dark rectangle as a phone.
    shadow = Image.new("L", image.size, 0)
    offset = round(width * 0.04)
    ImageDraw.Draw(shadow).rounded_rectangle(
        (x1 + offset, y1 + offset * 2, box.x2 + offset, box.y2 + offset), radius=radius, fill=90
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, round(width * 0.05))))
    image.paste((150, 146, 138), mask=shadow)
    draw = ImageDraw.Draw(image)
    nub = max(1, round(width * 0.014))
    for top, length in ((0.17, 0.09), (0.29, 0.14)):
        draw.rounded_rectangle(
            (
                box.x2 - nub,
                y1 + round(height * top),
                box.x2 + nub,
                y1 + round(height * (top + length)),
            ),
            radius=nub,
            fill=(96, 100, 108),
        )
    draw.rounded_rectangle(
        (box.x1, box.y1, box.x2 - 1, box.y2 - 1), radius=radius, fill=(120, 124, 132)
    )
    rim = max(1, round(width * 0.012))
    draw.rounded_rectangle(
        (box.x1 + rim, box.y1 + rim, box.x2 - rim - 1, box.y2 - rim - 1),
        radius=radius - rim,
        fill=(18, 20, 24),
    )
    inset = max(2, round(width * 0.045))
    screen = (x1 + inset, y1 + inset, box.x2 - inset - 1, box.y2 - inset - 1)
    draw.rounded_rectangle(screen, radius=radius - inset, fill=(230, 240, 235))
    cam = max(1, round(width * 0.028))
    cx, cy = (box.x1 + box.x2) // 2, y1 + inset + round(height * 0.03)
    draw.ellipse((cx - cam, cy - cam, cx + cam, cy + cam), fill=(18, 20, 24))
    status = ImageFont.load_default(size=max(1, round(width * 0.05)))
    draw.text((x1 + round(width * 0.14), cy), "9:41", font=status, fill=(30, 34, 38), anchor="lm")
    bw, bh = round(width * 0.08), max(2, round(width * 0.035))
    bx = box.x2 - round(width * 0.14) - bw
    draw.rounded_rectangle(
        (bx, cy - bh // 2, bx + bw, cy + bh // 2), radius=max(1, bh // 4), fill=(30, 34, 38)
    )
    home_w, home_h = round(width * 0.34), max(2, round(height * 0.008))
    home_y = box.y2 - inset - round(height * 0.025)
    draw.rounded_rectangle(
        (cx - home_w // 2, home_y, cx + home_w // 2, home_y + home_h),
        radius=home_h // 2,
        fill=(30, 34, 38),
    )
    left = x1 + round(width * 0.12)
    draw.text(
        (left, y1 + round(height * 0.12)), "Pallas Fit",
        font=ImageFont.load_default(size=max(1, round(width * 0.12))), fill=(25, 64, 54),
    )
    draw.text(
        (left, y1 + round(height * 0.23)), "Day 12 streak",
        font=ImageFont.load_default(size=max(1, round(width * 0.065))), fill=(62, 91, 79),
    )
    bar_width, bar_height = round(width * 0.76), max(2, round(height * 0.028))
    font = ImageFont.load_default(size=max(1, round(width * 0.055)))
    for index, (label, progress) in enumerate((("Movement", 0.8), ("Strength", 0.6),
                                                ("Recovery", 0.9))):
        top = y1 + round(height * (0.39 + index * 0.15))
        draw.text((left, top), label, font=font, fill=(45, 72, 63))
        bar_top = top + round(height * 0.05)
        draw.rounded_rectangle(
            (left, bar_top, left + bar_width, bar_top + bar_height),
            radius=bar_height // 2, fill=(199, 216, 206),
        )
        draw.rounded_rectangle(
            (left, bar_top, left + round(bar_width * progress), bar_top + bar_height),
            radius=bar_height // 2, fill=(59, 130, 103),
        )
    return image, box


def offline_background(prompt: str, size: tuple[int, int] = (1024, 1024)) -> Image.Image:
    """Draw a prompt-seeded gradient with softly blurred circles."""
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("Background dimensions must be positive")
    seed = hashlib.sha256(prompt.encode("utf-8")).digest()
    first = tuple(55 + value // 2 for value in seed[:3])
    second = tuple(55 + value // 2 for value in seed[3:6])
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        fraction = y / max(1, height - 1)
        color = tuple(round(a + (b - a) * fraction) for a, b in zip(first, second))
        draw.line((0, y, width - 1, y), fill=color)
    circles = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(circles)
    for index in range(3):
        offset = 6 + index * 3
        x = round(seed[offset] / 255 * width)
        y = round(seed[offset + 1] / 255 * height)
        radius = round(min(size) * (0.12 + seed[offset + 2] / 255 * 0.15))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 255, 255, 45))
    circles = circles.filter(ImageFilter.GaussianBlur(max(1, min(size) * 0.045)))
    return Image.alpha_composite(image.convert("RGBA"), circles).convert("RGB")


def _wrap_headline(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        # Split a single word too wide for the band instead of clipping it.
        for character in word:
            if line and font.getlength(line + character) > width:
                lines.append(line)
                line = ""
            line += character
    return [*lines, line] if line or not lines else lines


def fit_headline(text: str, width: int, sizes=range(84, 43, -4), max_lines: int = 3):
    """The largest bold size that fits the text in `max_lines`; trims with an ellipsis if none
    does. Returns (font, lines)."""
    for size in sizes:
        face = font(size)
        lines = _wrap_headline(text, face, width)
        if len(lines) <= max_lines:
            return face, lines
    lines = lines[:max_lines]
    last = lines[-1].rstrip()
    while last and face.getlength(last + "...") > width:
        last = last[:-1].rstrip()
    lines[-1] = last + "..."
    return face, lines


def draw_lines(canvas: Image.Image, lines: list[str], face, xy: tuple[int, int],
               fill="white", gap: float = 0.18) -> int:
    """Draw left-aligned lines with a faint shadow and return the y below the last line."""
    x, y = xy
    ascent, descent = face.getmetrics()
    step = round((ascent + descent) * (1 + gap))
    shade = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shade).multiline_text((x, y + 2), "\n".join(lines), font=face,
                                         fill=(0, 0, 0, 110), spacing=step - ascent - descent)
    canvas.alpha_composite(shade.filter(ImageFilter.GaussianBlur(3)))
    ImageDraw.Draw(canvas).multiline_text((x, y), "\n".join(lines), font=face, fill=fill,
                                          spacing=step - ascent - descent)
    return y + step * (len(lines) - 1) + ascent + descent


def render_creative(
    background: Image.Image, product: Image.Image, headline: str, ratio: str,
    brand: str | None = None, ink: tuple[int, int, int] = INK,
) -> Rendered:
    """Background scene, the headline on a dark fade at the top, the product below it.
    Words and product stay inside the placement's safe area."""
    if ratio not in CREATIVE_SIZES:
        raise ValueError(f"Unknown creative ratio: {ratio}")
    output = ImageOps.fit(
        background.convert("RGB"), CREATIVE_SIZES[ratio], method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    top, right, bottom, left = SAFE_MARGINS[ratio]
    safe_width = output.width - left - right
    safe_bottom = output.height - bottom

    face, lines = fit_headline(headline, safe_width - 8)
    label = font(30, bold=False) if brand else None
    y = top
    if label:
        y += label.getmetrics()[0] + label.getmetrics()[1] + 14
    text_bottom = y + (face.getmetrics()[0] + face.getmetrics()[1]) * len(lines) \
        + round(0.18 * sum(face.getmetrics())) * (len(lines) - 1)
    output.alpha_composite(scrim(output.size, text_bottom + round(0.22 * output.height), ink))
    if label:
        ImageDraw.Draw(output).text((left, top), brand.upper(), font=label,
                                    fill=(255, 255, 255, 225))
    end = draw_lines(output, lines, face, (left, y))
    text_box = (left, top, left + safe_width, max(end, text_bottom))

    product_top = text_box[3] + 40
    room = safe_bottom - product_top
    scale = min(0.72 * safe_width / product.width, 0.86 * room / product.height)
    size = (max(1, int(product.width * scale)), max(1, int(product.height * scale)))
    resized = product.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    x = left + (safe_width - resized.width) // 2
    y = product_top + round((room - resized.height) * 0.62)
    place_product(output, resized, (x, y))
    product_box = (x, y, x + resized.width, y + resized.height)
    return Rendered(output.convert("RGB"), product_box, text_box)


def png_bytes(image: Image.Image) -> bytes:
    """Encode pixels without inheriting source image metadata."""
    clean = Image.new(image.mode, image.size)
    clean.paste(image)
    if image.mode == "P":
        clean.putpalette(image.getpalette())
    buffer = BytesIO()
    clean.save(buffer, format="PNG")
    return buffer.getvalue()


def save_png(image: Image.Image, path: str | Path) -> str:
    """Write a deterministic PNG and return the digest of its bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = png_bytes(image)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
