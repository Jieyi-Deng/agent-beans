"""The 10-second 9:16 ad video, assembled in code from the ad's three shots.

Meta's model API has no video model, so nothing here is generated video. The Producer writes a
storyboard; Muse Image renders the three shots with the real product in them; SAM 3.1 finds the
product. This module moves the camera toward the product in each shot, slides the storyboard
lines in over a wash that keeps them readable, cross-fades between shots and ends on a packshot
card with the offer and the button, all in the brand's type. ffmpeg encodes the MP4.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from pgt.creative.compose import Box, place_product
from pgt.creative.design import (
    Style,
    block_height,
    crop_box,
    draw_block,
    fit_text,
    pill,
    pill_size,
    product_rect,
    safe_rect,
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
    def __init__(self, photo: Image.Image, box: Box | None, move, text: str, style: Style) -> None:
        self.photo = photo.convert("RGB")
        rect = product_rect(box, self.photo.size)
        self.focus = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2) if rect else None
        self.move = move
        left, top, right, bottom = safe_rect("9:16")
        face, lines = fit_text(text, style.display, right - left, range(104, 59, -4), 3)
        step = round(face.size * 1.08)
        y = bottom - block_height(face, lines)
        self.lines = []
        for line in lines:
            self.lines.append(_Line(line, face, style.paper, (left, y)))
            y += step
        words_box = (left, bottom - block_height(face, lines), right, bottom)
        mark_box = (left, top, right, top + 40)
        middle = self.camera(0.5).convert("RGBA")
        self.washes = []
        for rect, side, start in ((words_box, "bottom", 0.3), (mark_box, "top", 0.0)):
            layer, _ = wash_for(middle, rect, side, style.paper, style.ink, start)
            if layer is not None:
                middle = Image.alpha_composite(middle, layer)
                self.washes.append(layer)

    def camera(self, progress: float) -> Image.Image:
        start, end, drift = self.move
        zoom = start + (end - start) * _ease(progress)
        target = (0.5 + drift * (0.5 - progress), 0.45)
        crop = crop_box(self.photo.size, SIZE, self.focus, target, zoom)
        return self.photo.resize(SIZE, Image.Resampling.BILINEAR, box=crop)


class Cut:
    """The video as layers: three moving shots with their storyboard lines, then the end card."""

    def __init__(self, scenes: list[Image.Image], boxes: list[Box | None], product: Image.Image,
                 story: Storyboard, headline: str, style: Style, offer: str = "",
                 sign_off: str = "") -> None:
        if not scenes:
            raise ValueError("A video needs at least one scene")
        lines = [story.hook or headline, story.benefit or headline, story.proof or headline]
        self.style = style
        self.shots = [
            _Shot(scenes[i % len(scenes)], boxes[i % len(boxes)] if boxes else None, MOVES[i],
                  lines[i], style)
            for i in range(3)
        ]
        self.mark = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        if style.brand:
            left, top, _, _ = safe_rect("9:16")
            wordmark(ImageDraw.Draw(self.mark), style, (left, top), 40, style.paper)
        self.card = _EndCard(product, story.cta or style.cta, style, offer, sign_off)

    def _scene(self, i: int, t: int) -> Image.Image:
        shot = self.shots[i]
        length = SHOTS[i]
        frame = shot.camera(t / length).convert("RGBA")
        for layer in shot.washes:
            frame.alpha_composite(layer)
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
