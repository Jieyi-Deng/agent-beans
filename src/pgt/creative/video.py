"""The 10-second 9:16 ad video, assembled in code from the ad's three shots.

Meta's model API has no video model, so nothing here is generated video. The Producer writes a
storyboard; Muse Image renders the three shots with the real product in them; SAM 3.1 finds the
product. This module moves the camera toward the product in each shot, slides the storyboard
lines in over a wash that keeps them readable (or on a brand strip when the product is too
big to clear them), cross-fades between shots and ends on a packshot card with the offer and
the button, all in the brand's type. ffmpeg encodes the MP4.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from pgt.creative.compose import Box, place_product
from pgt.creative.design import (
    FIT_COVERED,
    Style,
    _map,
    block_height,
    covered,
    crop_box,
    draw_block,
    fit_text,
    pill,
    pill_size,
    product_rect,
    safe_rect,
    shown,
    type_face,
    wash_for,
    wordmark,
)
from pgt.schemas import CreativeVideo, Storyboard

SIZE = (1080, 1920)
FPS = 30
SECONDS = 10
# Shot lengths in frames: hook 0-3 s, benefit 3-6 s, proof 6-8.5 s, end card 8.5-10 s.
SHOTS = (90, 90, 75, 45)
FADE = 9  # frames of cross-fade at each cut (0.3 s)
RISE = 12  # frames a line takes to slide and fade in
STAGGER = 4  # frames between lines
LIFT = 36  # pixels a line slides up as it comes in
# The camera in each shot: (zoom at start, zoom at end, sideways drift as a share of the frame).
MOVES = ((1.0, 1.1, 0.0), (1.12, 1.12, 0.08), (1.12, 1.02, 0.0))


def _ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


class _Line:
    """One set line of a storyboard caption, kept as its own layer so it can move."""

    def __init__(self, text: str, face, color, xy: tuple[int, int]) -> None:
        width = round(face.getlength(text)) + 8
        height = block_height(face, [text]) + 8
        self.layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_block(ImageDraw.Draw(self.layer), [text], face, (0, 0), color)
        self.xy = xy

    def draw(self, frame: Image.Image, amount: float) -> None:
        if amount <= 0:
            return
        layer = self.layer
        if amount < 1:
            layer = layer.copy()
            layer.putalpha(layer.getchannel("A").point(lambda v: round(v * amount)))
        frame.alpha_composite(layer, (self.xy[0], self.xy[1] + round(LIFT * (1 - amount))))


class _Shot:
    """One moving shot. Its words sit at the bottom of the safe zone, or just under the wordmark
    when the product would be behind them at the bottom; the camera aims the product at the
    space the words leave free. When the product is too big to clear the words either way, the
    wordmark and words move onto a brand-colour strip across the top and the photo runs below;
    `strip` puts them there whenever the product fits, so a video keeps one caption style."""

    def __init__(self, photo: Image.Image, box: Box | None, move, text: str, style: Style,
                 strip: bool = False) -> None:
        self.photo = photo.convert("RGB")
        self.rect = product_rect(box, self.photo.size)
        self.focus = ((self.rect[0] + self.rect[2]) / 2,
                      (self.rect[1] + self.rect[3]) / 2) if self.rect else None
        self.move = move
        self.style = style
        left, top, right, bottom = safe_rect("9:16")
        face, lines = fit_text(text, style.display, right - left, range(104, 59, -4), 3)
        height = block_height(face, lines)
        low_box = (left, bottom - height, right, bottom)
        high_top = top + 90  # under the wordmark
        high_box = (left, high_top, right, high_top + height)
        top_strip = high_box[3] + 56
        options = [  # (words, where they sit, top of the photo, where the product aims)
            (low_box, "bottom", 0, (high_top + low_box[1] - 40) / 2 / SIZE[1]),
            (high_box, "top", 0, (high_box[3] + 40 + bottom) / 2 / SIZE[1]),
        ]
        # Under the strip, the product aims at the open photo above the Reels buttons; a big
        # one aims lower, so that none of it slips up behind the strip.
        options += [(high_box, "strip", top_strip, aim)
                    for aim in ((bottom - top_strip) / 2 / (SIZE[1] - top_strip), 0.4, 0.5)]
        scores = [self._misfit(words, aim, photo_top) for words, _, photo_top, aim in options]
        pick = 1 if scores[1] < scores[0] - 0.05 else 0
        under = min(range(2, len(options)), key=lambda i: (round(scores[i], 2), i))
        if scores[pick] > FIT_COVERED and scores[under] < scores[pick] - 0.02:
            pick = under
        elif strip and scores[under] <= max(FIT_COVERED, scores[pick]):
            pick = under  # the rest of the video is on the strip; this shot fits there too
        words_box, side, self.top, self.aim = options[pick]
        self.covered = self._fit(words_box, self.aim, self.top)[0]
        color = style.on(style.panel) if side == "strip" else style.paper
        step = round(face.size * 1.08)
        y = words_box[1]
        self.lines = []
        for line in lines:
            self.lines.append(_Line(line, face, color, (left, y)))
            y += step
        self.washes = []
        if side == "strip":
            layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            d.rectangle((0, 0, SIZE[0], self.top), fill=style.panel)
            d.rectangle((0, self.top - 6, SIZE[0], self.top), fill=style.accent)
            if style.brand:
                wordmark(d, style, (left, top), 40, color)
            self.washes.append(layer)
            return
        mark_box = (left, top, right, top + 40)
        middle = self.camera(0.5).convert("RGBA")
        washes = ((words_box, side, 0.3),) if side == "top" else \
            ((words_box, "bottom", 0.3), (mark_box, "top", 0.0))
        for rect, edge, start in washes:
            covers = (rect[0], min(rect[1], mark_box[1]), rect[2], rect[3]) if edge == "top" \
                else rect
            layer, _ = wash_for(middle, covers, edge, style.paper, style.ink, start)
            if layer is not None:
                middle = Image.alpha_composite(middle, layer)
                self.washes.append(layer)

    @property
    def strip(self) -> bool:
        """True when the words sit on the brand strip, which carries its own wordmark."""
        return self.top > 0

    def _crop(self, progress: float, aim: float | None = None,
              top: int | None = None) -> tuple[float, float, float, float]:
        start, end, drift = self.move
        zoom = start + (end - start) * _ease(progress)
        target = (0.5 + drift * (0.5 - progress), self.aim if aim is None else aim)
        region = (SIZE[0], SIZE[1] - (self.top if top is None else top))
        return crop_box(self.photo.size, region, self.focus, target, zoom)

    def _fit(self, words: tuple[int, int, int, int], aim: float,
             top: int) -> tuple[float, float]:
        """The most of the product the words cover at any point in the shot, and the least of
        it in the picture."""
        if self.rect is None:
            return 0.0, 1.0
        region = (SIZE[0], SIZE[1] - top)
        placed = [_map(self.rect, self._crop(t, aim, top), (0, top), region)
                  for t in (0.0, 0.5, 1.0)]
        return (max(covered(p, [words]) for p in placed),
                min(shown(p, (0, top, *SIZE)) for p in placed))

    def _misfit(self, words: tuple[int, int, int, int], aim: float, top: int) -> float:
        under, seen = self._fit(words, aim, top)
        return under + 2 * (1 - seen)

    def camera(self, progress: float) -> Image.Image:
        region = (SIZE[0], SIZE[1] - self.top)
        photo = self.photo.resize(region, Image.Resampling.BILINEAR, box=self._crop(progress))
        if not self.top:
            return photo
        frame = Image.new("RGB", SIZE, self.style.panel)
        frame.paste(photo, (0, self.top))
        return frame


class Cut:
    """The video as layers: three moving shots with their storyboard lines, then the end card."""

    def __init__(self, scenes: list[Image.Image], boxes: list[Box | None], product: Image.Image,
                 story: Storyboard, headline: str, style: Style, offer: str = "",
                 sign_off: str = "") -> None:
        if not scenes:
            raise ValueError("A video needs at least one scene")
        lines = [story.hook or headline, story.benefit or headline, story.proof or headline]
        self.style = style
        self.shots = [self._shot(scenes, boxes, lines, i, style) for i in range(3)]
        if any(shot.strip for shot in self.shots):  # one caption style for the whole video
            self.shots = [shot if shot.strip else self._shot(scenes, boxes, lines, i, style, True)
                          for i, shot in enumerate(self.shots)]
        self.mark = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        if style.brand:
            left, top, _, _ = safe_rect("9:16")
            wordmark(ImageDraw.Draw(self.mark), style, (left, top), 40, style.paper)
        self.card = _EndCard(product, story.cta or style.cta, style, offer, sign_off)

    @staticmethod
    def _shot(scenes, boxes, lines, i: int, style: Style, strip: bool = False) -> _Shot:
        return _Shot(scenes[i % len(scenes)], boxes[i % len(boxes)] if boxes else None, MOVES[i],
                     lines[i], style, strip)

    def covered(self) -> list[float]:
        """For each shot, the most of the product its caption covers."""
        return [shot.covered for shot in self.shots]

    def _scene(self, i: int, t: int) -> Image.Image:
        shot = self.shots[i]
        length = SHOTS[i]
        frame = shot.camera(t / length).convert("RGBA")
        for layer in shot.washes:
            frame.alpha_composite(layer)
        if not shot.strip:
            frame.alpha_composite(self.mark)
        out = 1 - max(0, t - (length - FADE)) / FADE
        for k, line in enumerate(shot.lines):
            line.draw(frame, _ease((t - 6 - k * STAGGER) / RISE) * out)
        return frame.convert("RGB")

    def frame(self, i: int, t: int) -> Image.Image:
        if i < len(self.shots):
            return self._scene(i, t)
        return self.card.frame(t)

    def keyframes(self) -> list[Image.Image]:
        """One still per shot, words in place, as the Reviewer sees them."""
        return [self._still(i) for i in range(len(SHOTS))]

    def _still(self, i: int) -> Image.Image:
        if i >= len(self.shots):
            return self.card.frame(SHOTS[-1] - 1)
        t = 6 + RISE + STAGGER * 3
        return self._scene(i, t)

    def frames(self):
        """Every frame of the video, in order."""
        starts = [sum(SHOTS[:i]) for i in range(len(SHOTS))]
        for n in range(sum(SHOTS)):
            i = max(k for k, s in enumerate(starts) if s <= n)
            frame = self.frame(i, n - starts[i])
            if i + 1 < len(SHOTS) and n >= starts[i + 1] - FADE:
                mix = (n - (starts[i + 1] - FADE) + 1) / (FADE + 1)
                frame = Image.blend(frame, self.frame(i + 1, 0), mix)
            yield frame


class _EndCard:
    """Paper card: wordmark, the original product photo easing in, the offer, the button and
    the sign-off, inside the 9:16 safe area."""

    def __init__(self, product: Image.Image, cta: str, style: Style, offer: str,
                 sign_off: str) -> None:
        width = SIZE[0]
        left, top, right, bottom = safe_rect("9:16")
        self.base = Image.new("RGBA", SIZE, (*style.paper, 255))
        draw = ImageDraw.Draw(self.base)
        if style.brand:
            wordmark(draw, style, (left, top), 44, style.ink, align="center", width=right - left)
        y = bottom
        self.texts = []  # (layer, xy) shown after the product lands
        if sign_off:
            face = type_face(style.text, 34)
            y -= block_height(face, [sign_off])
            layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
            draw_block(ImageDraw.Draw(layer), [sign_off], face, (left, y), (*style.ink, 190),
                       align="center", width=right - left)
            self.texts.append(layer)
            y -= 40
        if cta:
            pw, ph = pill_size(style, 42, cta)
            y -= ph
            layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
            pill(ImageDraw.Draw(layer), style, ((width - pw) // 2, y), 42, style.accent, cta)
            self.texts.insert(0, layer)
            y -= 44
        if offer:
            face, lines = fit_text(offer, style.display, right - left, range(60, 35, -4), 2)
            y -= block_height(face, lines)
            layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
            draw_block(ImageDraw.Draw(layer), lines, face, (left, y), style.ink, align="center",
                       width=right - left)
            self.texts.insert(0, layer)
            y -= 40
        room_top = top + (90 if style.brand else 10)
        room = (room_top, y)
        scale = min(0.86 * (right - left) / product.width, (room[1] - room[0]) / product.height)
        self.product = product.convert("RGBA")
        self.scale = scale
        self.room = room

    def frame(self, t: int) -> Image.Image:
        frame = self.base.copy()
        grow = 0.92 + 0.08 * _ease(t / 18)
        size = (max(1, int(self.product.width * self.scale * grow)),
                max(1, int(self.product.height * self.scale * grow)))
        shown = self.product.resize(size, Image.Resampling.BILINEAR)
        x = (SIZE[0] - shown.width) // 2
        y = self.room[0] + (self.room[1] - self.room[0] - shown.height) // 2
        place_product(frame, shown, (x, y))
        for k, layer in enumerate(self.texts):
            amount = _ease((t - 6 - k * STAGGER) / RISE)
            if amount <= 0:
                continue
            if amount < 1:
                layer = layer.copy()
                layer.putalpha(layer.getchannel("A").point(lambda v, a=amount: round(v * a)))
            frame.alpha_composite(layer, (0, round(LIFT * (1 - amount))))
        return frame.convert("RGB")


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def encode(cut: Cut, path: str | Path) -> CreativeVideo | None:
    """Write the MP4 (H.264, silent, starts playing before it finishes loading). Returns None
    when ffmpeg is not installed, so a run without it still makes the images."""
    binary = ffmpeg_path()
    if binary is None:
        return None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = SIZE
    command = [
        binary, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(FPS),
        "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-map_metadata", "-1", "-fflags", "+bitexact",
        "-flags:v", "+bitexact", str(path),
    ]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        assert proc.stdin is not None
        try:
            for frame in cut.frames():
                proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()
        error = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {error.strip()[:300]}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return CreativeVideo(width=width, height=height, seconds=SECONDS, path=str(path),
                         sha256=digest)


def contact_sheet(squares: list[Image.Image], stills: list[Image.Image],
                  reference: Image.Image | None = None) -> Image.Image:
    """What the Reviewer looks at: the real product photo first, then the square image for each
    scene on top, the video's shots in order below."""
    cell = 360
    top_row = ([reference] if reference is not None else []) + list(squares)
    tall = round(cell * SIZE[1] / SIZE[0] * 0.75)
    still_w = round(tall * SIZE[0] / SIZE[1])
    width = max(len(top_row) * (cell + 16), len(stills) * (still_w + 16)) + 16
    sheet = Image.new("RGB", (width, cell + tall + 48), (255, 255, 255))
    for i, image in enumerate(top_row):
        tile = image.convert("RGB")
        if i == 0 and reference is not None:
            tile = tile.copy()
            tile.thumbnail((cell, cell))
            framed = Image.new("RGB", (cell, cell), (255, 255, 255))
            framed.paste(tile, ((cell - tile.width) // 2, (cell - tile.height) // 2))
            tile = framed
        sheet.paste(tile.resize((cell, cell)), (16 + i * (cell + 16), 16))
    for i, image in enumerate(stills):
        sheet.paste(image.convert("RGB").resize((still_w, tall)),
                    (16 + i * (still_w + 16), cell + 32))
    return sheet
