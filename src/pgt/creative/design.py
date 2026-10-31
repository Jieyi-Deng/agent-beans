"""Ad layouts drawn in code: the brand's type and colours set over a photograph.

Muse Image renders each shot with the real product in it and SAM 3.1 says where the product is.
This module crops the shot for each placement size so the product stays clear of the words, sets
the words in the brand's fonts, darkens the photo under them only as much as reading needs
(WCAG contrast 4.5:1), and keeps all of it inside Meta's safe zones.

Layouts:
- band: the photo full bleed, the headline and button in a band along the bottom (or top) edge;
- panel: the photo above a solid brand-colour panel that holds the words;
- poster: large editorial type over a calm part of the photo, wordmark and button below;
- offer: band, plus a round badge with the offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from pgt.creative.compose import FONTS, INK, SAFE_MARGINS, Box, _wrap_headline, font
from pgt.schemas.weekly import CREATIVE_SIZES

if TYPE_CHECKING:
    from pgt.sandbox.scenario import CreativeKit

Rect = tuple[int, int, int, int]
RGB = tuple[int, int, int]
MIN_CONTRAST = 4.5
LAYOUTS = ("band", "panel", "poster", "offer")
PAD = 72  # least space between the words and the picture's edge


def hex_rgb(value: str | None, default: RGB) -> RGB:
    if not value or len(value.lstrip("#")) != 6:
        return default
    raw = value.lstrip("#")
    return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


# --- Type ------------------------------------------------------------------------------------


@lru_cache(maxsize=256)
def type_face(spec: str, size: int) -> ImageFont.FreeTypeFont:
    """A brand font at a pixel size. `spec` is "<file in fonts/>:<weight>", e.g. "Fraunces:600".
    Optical size follows the pixel size and Fraunces' wonky letters are off. Liberation Sans when
    the font is not there."""
    name, _, weight = (spec or "").partition(":")
    wght = int(weight) if weight.isdigit() else 700
    size = max(1, size)
    path = FONTS / f"{name}.ttf"
    if not name or not path.exists():
        return font(size, bold=wght >= 600)
    face = ImageFont.truetype(str(path), size)
    try:
        axes = face.get_variation_axes()
    except OSError:
        return face
    values = []
    for axis in axes:
        label = axis["name"]
        label = (label.decode() if isinstance(label, bytes) else str(label)).lower()
        if label.startswith("weight"):
            value = wght
        elif label.startswith("optical"):
            value = size
        elif label.startswith("wonk"):
            value = 0
        else:
            value = axis["default"]
        values.append(max(axis["minimum"], min(axis["maximum"], value)))
    face.set_variation_by_axes(values)
    return face


def wrap(text: str, face: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap to the fewest lines, then even the lines out so no word sits alone on the last."""
    lines = _wrap_headline(text, face, width)
    best = lines
    for narrower in range(width - 24, width // 2, -24):
        trial = _wrap_headline(text, face, narrower)
        if len(trial) > len(lines):
            break
        best = trial
    return best


def fit_text(text: str, spec: str, width: int, sizes: range, max_lines: int):
    """The largest size in `sizes` that sets `text` in `max_lines` or fewer; trims with an
    ellipsis when none does. Returns (face, lines)."""
    face, lines = type_face(spec, sizes[0]), [text]
    for size in sizes:
        face = type_face(spec, size)
        lines = wrap(text, face, width)
        if len(lines) <= max_lines:
            return face, lines
    lines = lines[:max_lines]
    last = lines[-1].rstrip()
    while last and face.getlength(last + "...") > width:
        last = last[:-1].rstrip()
    lines[-1] = last + "..."
    return face, lines


def leading(face: ImageFont.FreeTypeFont, tight: float = 1.08) -> int:
    return round(face.size * tight)


def block_height(face: ImageFont.FreeTypeFont, lines: list[str], tight: float = 1.08) -> int:
    """Height of set lines from the cap line of the first to the descender of the last."""
    return round(face.size * 0.74) + leading(face, tight) * (len(lines) - 1) + round(face.size * 0.24)


def draw_block(draw: ImageDraw.ImageDraw, lines: list[str], face, xy: tuple[int, int],
               fill, align: str = "left", width: int = 0, tight: float = 1.08) -> Rect:
    """Draw lines with their cap line at xy[1]; return the box they cover."""
    x, top = xy
    baseline = top + round(face.size * 0.74)
    x_min, x_max = 10**6, 0
    for line in lines:
        w = face.getlength(line)
        lx = x if align == "left" else (x + (width - w) / 2 if align == "center" else x + width - w)
        draw.text((lx, baseline), line, font=face, fill=fill, anchor="ls")
        x_min, x_max = min(x_min, lx), max(x_max, lx + w)
        baseline += leading(face, tight)
    return (round(x_min), top, round(x_max), top + block_height(face, lines, tight))


# --- Colour and contrast ---------------------------------------------------------------------


def _channel(v: int) -> float:
    c = v / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


_LINEAR = [_channel(v) for v in range(256)]


def luminance(rgb: RGB) -> float:
    r, g, b = rgb[:3]
    return 0.2126 * _LINEAR[r] + 0.7152 * _LINEAR[g] + 0.0722 * _LINEAR[b]


def contrast(a: RGB, b: RGB) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def backdrop_contrast(image: Image.Image, rect: Rect, text: RGB) -> float:
    """Contrast of `text` against the worst tenth of the pixels behind it: the brightest tenth
    for light words, the darkest tenth for dark ones."""
    region = image.crop(rect).convert("RGB")
    region = region.resize((max(1, region.width // 6), max(1, region.height // 6)))
    values = sorted(luminance(p) for p in region.get_flattened_data())
    lt = luminance(text)
    median = values[len(values) // 2]
    worst = values[int(0.9 * (len(values) - 1))] if lt >= median else values[len(values) // 10]
    high, low = sorted((lt, worst), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _shade(size: tuple[int, int], rect: Rect, side: str, color: RGB, strength: float,
           fade: int) -> Image.Image:
    """A soft wash of `color` from one edge that is solid behind `rect` and fades out beyond."""
    h = size[1]
    pad = 36
    column = []
    for y in range(h):
        if side == "bottom":
            edge = rect[1] - pad
            t = 1.0 if y >= edge else max(0.0, 1 - (edge - y) / fade)
        else:
            edge = rect[3] + pad
            t = 1.0 if y <= edge else max(0.0, 1 - (y - edge) / fade)
        column.append(round(255 * strength * t * t * (3 - 2 * t)))
    mask = Image.new("L", (1, h))
    mask.putdata(column)
    layer = Image.new("RGBA", size, (*color, 0))
    layer.putalpha(mask.resize(size))
    return layer


def wash_for(canvas: Image.Image, rect: Rect, side: str, text: RGB, wash: RGB,
             start: float = 0.0) -> tuple[Image.Image | None, float]:
    """The lightest wash of `wash` from `side` that lets `text` read at 4.5:1 over `rect`.
    Returns the wash as a layer (None when the photo needs none) and the contrast reached."""
    fade = max(320, round(0.55 * (rect[3] - rect[1])) + 160)
    strengths = [s for s in (0.0, 0.3, 0.45, 0.6, 0.72, 0.82, 0.9, 0.95) if s >= start]
    best: tuple[Image.Image | None, float] = (None, 0.0)
    for strength in strengths:
        layer = _shade(canvas.size, rect, side, wash, strength, fade) if strength else None
        trial = Image.alpha_composite(canvas, layer) if layer else canvas
        best = (layer, backdrop_contrast(trial, rect, text))
        if best[1] >= MIN_CONTRAST:
            break
    return best


def legible(canvas: Image.Image, rect: Rect, side: str, text: RGB, wash: RGB,
            start: float = 0.0) -> tuple[Image.Image, float]:
    """Darken (or lighten) the photo from `side` just enough for `text` to read at 4.5:1 over
    `rect`. Returns the new canvas and the contrast reached."""
    layer, score = wash_for(canvas, rect, side, text, wash, start)
    return (Image.alpha_composite(canvas, layer) if layer else canvas), score


# --- The brand -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Style:
    """The brand as layout code needs it."""

    brand: str
    display: str  # "<font>:<weight>" for headlines and the wordmark
    text: str  # for buttons and small print
    panel: RGB  # brand colour
    ink: RGB
    paper: RGB
    accent: RGB
    cta: str
    badge: str = ""

    @classmethod
    def from_kit(cls, kit: CreativeKit | None, cta: str) -> Style:
        if kit is None:
            return cls("", "", "", (40, 72, 64), INK, (246, 242, 234), (40, 72, 64), cta)
        c = kit.colors
        panel = hex_rgb(c.get("brand"), (40, 72, 64))
        return cls(
            brand=kit.brand,
            display=kit.fonts.get("display", ""),
            text=kit.fonts.get("text", ""),
            panel=panel,
            ink=hex_rgb(c.get("ink"), INK),
            paper=hex_rgb(c.get("paper"), (246, 242, 234)),
            accent=hex_rgb(c.get("accent"), panel),
            cta=cta,
            badge=kit.offer_badge,
        )

    def on(self, fill: RGB) -> RGB:
        """Paper or ink, whichever reads better on `fill`."""
        return self.paper if contrast(self.paper, fill) >= contrast(self.ink, fill) else self.ink


def wordmark(draw: ImageDraw.ImageDraw, style: Style, xy: tuple[int, int], size: int, fill: RGB,
             align: str = "left", width: int = 0) -> Rect:
    """The brand name in its display face behind a small accent dot."""
    face = type_face(style.display, size)
    dot = round(size * 0.42)
    gap = round(size * 0.32)
    text_w = face.getlength(style.brand)
    total = dot + gap + text_w
    x, top = xy
    if align == "right":
        x = x + width - total
    elif align == "center":
        x = x + (width - total) / 2
    cap = round(size * 0.74)
    cy = top + cap / 2
    draw.ellipse((x, cy - dot / 2, x + dot, cy + dot / 2), fill=style.accent)
    draw.text((x + dot + gap, top + cap), style.brand, font=face, fill=fill, anchor="ls")
    return (round(x), top, round(x + total), top + round(size * 0.98))


def pill_size(style: Style, size: int, label: str) -> tuple[int, int]:
    face = type_face(style.text, size)
    return round(face.getlength(label) + size * 2.0), round(size * 2.2)


def pill(draw: ImageDraw.ImageDraw, style: Style, xy: tuple[int, int], size: int, fill: RGB,
         label: str, text: RGB | None = None) -> Rect:
    """A rounded call-to-action button with its top left at xy."""
    face = type_face(style.text, size)
    w, h = pill_size(style, size, label)
    x, y = xy
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=fill)
    draw.text((x + w / 2, y + h / 2), label, font=face, fill=text or style.on(fill), anchor="mm")
    return (x, y, x + w, y + h)


def badge(style: Style, diameter: int) -> Image.Image:
    """A round accent badge with the offer in it, tilted a little."""
    scale = 2
    d = diameter * scale
    layer = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((0, 0, d - 1, d - 1), fill=style.accent)
    inner = round(d * 0.66)
    face, lines = fit_text(style.badge, style.display, inner,
                           range(round(d * 0.2), round(d * 0.08), -2 * scale), 3)
    height = block_height(face, lines, 1.0)
    draw_block(draw, lines, face, ((d - inner) // 2, (d - height) // 2), style.on(style.accent),
               align="center", width=inner, tight=1.0)
    layer = layer.rotate(10, resample=Image.Resampling.BICUBIC)
    return layer.resize((diameter, diameter), Image.Resampling.LANCZOS)


# --- Cropping --------------------------------------------------------------------------------


def product_rect(box: Box | None, size: tuple[int, int]) -> tuple[float, float, float, float] | None:
    """A SAM box in the pixels of an image of `size`."""
    if box is None:
        return None
    sx, sy = size[0] / box.w, size[1] / box.h
    return (box.x1 * sx, box.y1 * sy, box.x2 * sx, box.y2 * sy)


def crop_box(src: tuple[int, int], region: tuple[int, int], focus: tuple[float, float] | None,
             target: tuple[float, float], zoom: float = 1.0) -> tuple[float, float, float, float]:
    """The part of a `src`-sized photo that fills a `region`-sized frame, zoomed in by `zoom`,
    with `focus` (photo pixels) as close as it can get to `target` (fractions of the frame)."""
    cover = max(region[0] / src[0], region[1] / src[1]) * zoom
    cw, ch = region[0] / cover, region[1] / cover
    fx, fy = focus if focus else (src[0] / 2, src[1] / 2)
    x0 = min(max(0.0, fx - target[0] * cw), src[0] - cw)
    y0 = min(max(0.0, fy - target[1] * ch), src[1] - ch)
    return (x0, y0, x0 + cw, y0 + ch)


def _map(rect, crop, region_xy, region_size) -> Rect:
    sx = region_size[0] / (crop[2] - crop[0])
    sy = region_size[1] / (crop[3] - crop[1])
    x1 = region_xy[0] + (rect[0] - crop[0]) * sx
    y1 = region_xy[1] + (rect[1] - crop[1]) * sy
    x2 = region_xy[0] + (rect[2] - crop[0]) * sx
    y2 = region_xy[1] + (rect[3] - crop[1]) * sy
    return (round(x1), round(y1), round(x2), round(y2))


def _area(r) -> float:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def _meet(a, b) -> Rect:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def covered(product: Rect | None, boxes) -> float:
    """Share of the product's box that words or badges sit on."""
    if product is None or not _area(product):
        return 0.0
    return min(1.0, sum(_area(_meet(product, b)) for b in boxes) / _area(product))


def shown(product: Rect | None, frame: Rect) -> float:
    """Share of the product's box inside the photo's frame."""
    if product is None or not _area(product):
        return 1.0
    return _area(_meet(product, frame)) / _area(product)


def safe_rect(ratio: str) -> Rect:
    width, height = CREATIVE_SIZES[ratio]
    top, right, bottom, left = SAFE_MARGINS[ratio]
    return (max(left, PAD), max(top, PAD), width - max(right, PAD), height - max(bottom, PAD))


# --- Layouts ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Design:
    image: Image.Image
    layout: str
    product_box: Rect | None  # where the product ended up; None when SAM found nothing
    text_boxes: tuple[Rect, ...]  # every word, button and badge
    contrast: float  # the lowest contrast of any words over their backdrop
    product_shown: float  # share of the product inside the picture


@dataclass
class _Plan:
    """Where the words go, worked out before the photo is placed."""

    boxes: list[Rect]  # areas the product should keep clear of
    free: tuple[int, int]  # the rows where the product should sit
    draw: object  # callable(canvas) -> (canvas, boxes, contrast)


def compose(photo: Image.Image, product: Box | None, ratio: str, layout: str, words: str,
            style: Style) -> Design:
    """Set `words` over `photo` in one layout and one placement size."""
    if layout not in LAYOUTS:
        layout = "band"
    if layout == "offer" and not style.badge:
        layout = "band"
    size = CREATIVE_SIZES[ratio]
    rect = product_rect(product, photo.size)
    focus = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2) if rect else None
    if layout == "panel":
        return _panel(photo, rect, focus, ratio, words, style)
    best = None
    sides = ("bottom", "top") if layout != "poster" else ("top",)
    for side in sides:
        plan = (_poster if layout == "poster" else _band)(ratio, words, style, side,
                                                          layout == "offer")
        for zoom in (1.0, 1.15, 1.3):
            target = (0.5, (plan.free[0] + plan.free[1]) / 2 / size[1])
            crop = crop_box(photo.size, size, focus, target, zoom)
            placed = _map(rect, crop, (0, 0), size) if rect else None
            score = (round(covered(placed, plan.boxes), 2) + (1 - shown(placed, (0, 0, *size))),
                     side != "bottom", zoom)
            if best is None or score < best[0]:
                best = (score, plan, crop, placed)
    _, plan, crop, placed = best
    canvas = photo.convert("RGB").resize(size, Image.Resampling.LANCZOS, box=crop).convert("RGBA")
    canvas, boxes, low = plan.draw(canvas)
    return Design(canvas.convert("RGB"), layout, placed, tuple(boxes), low,
                  shown(placed, (0, 0, *size)))


def _headline_sizes(ratio: str, big: bool) -> range:
    if big:
        return range({"1:1": 116, "4:5": 124, "9:16": 132}[ratio], 63, -4)
    return range({"1:1": 92, "4:5": 96, "9:16": 104}[ratio], 55, -4)


def _band(ratio: str, words: str, style: Style, side: str, offer: bool) -> _Plan:
    width = CREATIVE_SIZES[ratio][0]
    left, top, right, bottom = safe_rect(ratio)
    face, lines = fit_text(words, style.display, right - left, _headline_sizes(ratio, False), 3)
    head_h = block_height(face, lines)
    cta_size = 34 if ratio != "9:16" else 36
    pw, ph = pill_size(style, cta_size, style.cta)
    mark_size = 38
    gap = 30
    if side == "bottom":
        pill_xy = (left, bottom - ph)
        head_top = pill_xy[1] - gap - head_h
        mark_xy = (left, top)
        corner_y = top
        free = (top + 60, head_top - 40)
    else:
        head_top = top
        pill_xy = (left, head_top + head_h + gap)
        mark_xy = (left, bottom - round(mark_size * 0.98))
        corner_y = bottom - round(0.22 * width)
        free = (pill_xy[1] + ph + 40, bottom - 60)
    head_box = (left, head_top, right, head_top + head_h)
    pill_box = (pill_xy[0], pill_xy[1], pill_xy[0] + pw, pill_xy[1] + ph)
    mark_box = (left, mark_xy[1], left + round(width * 0.4), mark_xy[1] + mark_size)
    diameter = round(0.22 * width)
    badge_box = (right - diameter, corner_y, right, corner_y + diameter)
    boxes = [head_box, pill_box] + ([badge_box] if offer else [])

    def draw(canvas: Image.Image):
        canvas, low = legible(canvas, head_box, side, style.paper, style.ink, start=0.3)
        mark_side = "top" if side == "bottom" else "bottom"
        if style.brand:
            canvas, mark_low = legible(canvas, mark_box, mark_side, style.paper, style.ink)
            low = min(low, mark_low)
        d = ImageDraw.Draw(canvas)
        drawn = [draw_block(d, lines, face, (left, head_top), style.paper)]
        drawn.append(pill(d, style, pill_xy, cta_size, style.accent, style.cta))
        if style.brand:
            drawn.append(wordmark(d, style, mark_xy, mark_size, style.paper))
        if offer:
            canvas.alpha_composite(badge(style, diameter), badge_box[:2])
            drawn.append(badge_box)
        return canvas, drawn, low

    return _Plan(boxes, free, draw)


def _poster(ratio: str, words: str, style: Style, side: str, offer: bool) -> _Plan:
    left, top, right, bottom = safe_rect(ratio)
    face, lines = fit_text(words, style.display, right - left, _headline_sizes(ratio, True), 3)
    head_h = block_height(face, lines, 1.0)
    head_box = (left, top, right, top + head_h)
    cta_size = 34 if ratio != "9:16" else 36
    pw, ph = pill_size(style, cta_size, style.cta)
    pill_xy = (right - pw, bottom - ph)
    pill_box = (pill_xy[0], pill_xy[1], right, bottom)
    mark_size = 38
    mark_xy = (left, pill_xy[1] + (ph - round(mark_size * 0.74)) // 2)
    mark_box = (left, pill_xy[1], pill_xy[0] - 24, bottom)
    free = (top + head_h + 40, pill_xy[1] - 30)

    def draw(canvas: Image.Image):
        # Ink words on a light sky, paper words on a dark one; then wash only if needed.
        ink_score = backdrop_contrast(canvas, head_box, style.ink)
        paper_score = backdrop_contrast(canvas, head_box, style.paper)
        color, wash = (style.ink, style.paper) if ink_score > paper_score else \
            (style.paper, style.ink)
        canvas, low = legible(canvas, head_box, "top", color, wash)
        mark_color = style.paper
        if style.brand:
            canvas, mark_low = legible(canvas, mark_box, "bottom", mark_color, style.ink)
            low = min(low, mark_low)
        d = ImageDraw.Draw(canvas)
        drawn = [draw_block(d, lines, face, (left, top), color, tight=1.0)]
        drawn.append(pill(d, style, pill_xy, cta_size, style.accent, style.cta))
        if style.brand:
            drawn.append(wordmark(d, style, mark_xy, mark_size, mark_color))
        return canvas, drawn, low

    return _Plan([head_box, pill_box], free, draw)


def _panel(photo: Image.Image, rect, focus, ratio: str, words: str, style: Style) -> Design:
    """The photo on top, a brand-colour panel with the words below. The words end at the bottom
    of the safe zone (in 9:16, above the Reels buttons) and the panel runs on to the edge."""
    width, height = CREATIVE_SIZES[ratio]
    left, _, right, bottom = safe_rect(ratio)
    pad = 56
    face, lines = fit_text(words, style.display, right - left, _headline_sizes(ratio, False), 3)
    head_h = block_height(face, lines)
    cta_size = 34 if ratio != "9:16" else 36
    ph = pill_size(style, cta_size, style.cta)[1]
    mark_size = 34
    content = round(mark_size * 0.98) + 28 + head_h + 32 + ph
    panel_top = bottom - content - pad
    photo_size = (width, panel_top)
    best = None
    for zoom in (1.0, 1.15):
        crop = crop_box(photo.size, photo_size, focus, (0.5, 0.52), zoom)
        placed = _map(rect, crop, (0, 0), photo_size) if rect else None
        score = (1 - shown(placed, (0, 0, *photo_size)), zoom)
        if best is None or score < best[0]:
            best = (score, crop, placed)
    _, crop, placed = best
    canvas = Image.new("RGBA", (width, height), (*style.panel, 255))
    canvas.paste(photo.convert("RGB").resize(photo_size, Image.Resampling.LANCZOS, box=crop),
                 (0, 0))
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, panel_top, width, panel_top + 6), fill=style.accent)
    color = style.on(style.panel)
    y = panel_top + pad
    drawn = []
    if style.brand:
        drawn.append(wordmark(d, style, (left, y), mark_size, color))
    y += round(mark_size * 0.98) + 28
    drawn.append(draw_block(d, lines, face, (left, y), color))
    y += head_h + 32
    drawn.append(pill(d, style, (left, y), cta_size, color, style.cta, style.panel))
    low = contrast(color, style.panel)
    return Design(canvas.convert("RGB"), "panel", placed, tuple(drawn), low,
                  shown(placed, (0, 0, *photo_size)))
