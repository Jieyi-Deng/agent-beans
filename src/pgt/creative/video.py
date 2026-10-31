"""The 10-second 9:16 ad video, assembled in code from the ad's three scenes.

Meta's model API has no video model, so nothing here is generated video. The Producer writes a
storyboard; Muse Image paints the three scenes; SAM 3.1 finds the real product. This module lays
the storyboard lines over the scenes, adds an end card with the product, the offer and the
button, moves the camera slowly and cross-fades between shots, then encodes an MP4 with ffmpeg.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from pgt.creative.compose import (
    INK,
    SAFE_MARGINS,
    draw_lines,
    fit_headline,
    font,
    place_product,
    scrim,
)
from pgt.schemas import CreativeVideo, Storyboard

SIZE = (1080, 1920)
FPS = 30
SECONDS = 10
# Shot lengths in frames: hook 0-3 s, benefit 3-6 s, proof 6-8.5 s, end card 8.5-10 s.
SHOTS = (90, 90, 75, 45)
FADE = 9  # frames of cross-fade at each cut (0.3 s)
PUSH = 0.05  # how far the camera moves in over one shot


@dataclass
class EndCard:
    """What the last shot says. Taken from the advertiser's creative kit when there is one."""

    brand: str | None = None
    offer: str = ""
    sign_off: str = ""
    colors: dict[str, str] = field(default_factory=dict)


def hex_rgb(value: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if not value or len(value.lstrip("#")) != 6:
        return default
    raw = value.lstrip("#")
    return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def end_card(product: Image.Image, cta: str, card: EndCard) -> Image.Image:
    """Light card: brand name, the product, the offer, a button and the sign-off, all inside
    the 9:16 safe area so the Reels buttons cover none of it."""
    paper = hex_rgb(card.colors.get("paper"), (246, 242, 234))
    ink = hex_rgb(card.colors.get("ink"), INK)
    brand = hex_rgb(card.colors.get("brand"), (40, 72, 64))
    image = Image.new("RGBA", SIZE, (*paper, 255))
    draw = ImageDraw.Draw(image)
    width, height = SIZE
    top, right, bottom, left = SAFE_MARGINS["9:16"]
    middle = width // 2

    # Lay out from the bottom of the safe area up; the product gets the room that is left.
    blocks: list[tuple[str, object]] = []
    y = height - bottom
    if card.sign_off:
        face = font(36, bold=False)
        y -= sum(face.getmetrics())
        blocks.append(("sign_off", (face, y)))
        y -= 36
    if cta:
        face = font(46, bold=True)
        pill_h = 104
        y -= pill_h
        blocks.append(("cta", (face, y, pill_h)))
        y -= 40
    if card.offer:
        face, lines = fit_headline(card.offer, width - left - right - 40, sizes=range(60, 35, -4))
        step = sum(face.getmetrics()) + 8
        y -= step * len(lines)
        blocks.append(("offer", (face, lines, y, step)))
        y -= 24
    room_bottom = y
    room_top = top + 20
    if card.brand:
        label = font(40, bold=True)
        draw.text((middle, top + 20), card.brand.upper(), font=label, fill=(*brand, 255),
                  anchor="mt")
        room_top += sum(label.getmetrics()) + 40

    scale = min(0.72 * (width - left - right) / product.width,
                (room_bottom - room_top) / product.height)
    size = (max(1, int(product.width * scale)), max(1, int(product.height * scale)))
    shown = product.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    place_product(image, shown, ((width - shown.width) // 2,
                                 room_top + (room_bottom - room_top - shown.height) // 2))

    for kind, spec in blocks:
        if kind == "sign_off":
            face, y = spec
            draw.text((middle, y), card.sign_off, font=face, fill=(*ink, 200), anchor="mt")
        elif kind == "cta":
            face, y, pill_h = spec
            pill_w = int(draw.textlength(cta, font=face) + 120)
            x0 = middle - pill_w // 2
            draw.rounded_rectangle((x0, y, x0 + pill_w, y + pill_h), radius=pill_h // 2,
                                   fill=(*brand, 255))
            draw.text((middle, y + pill_h // 2), cta, font=face, fill=(255, 255, 255, 255),
                      anchor="mm")
        else:
            face, lines, y, step = spec
            for line in lines:
                draw.text((middle, y), line, font=face, fill=(*ink, 255), anchor="mt")
                y += step
    return image.convert("RGB")


@dataclass
class _Shot:
    scene: Image.Image  # the scene, already fitted to the frame
    words: Image.Image  # transparent layer: brand label and the storyboard line


class Cut:
    """The video as layers. In the three scene shots the product stays where it is while the
    scene and the words change around it; the end card follows."""

    def __init__(self, scenes: list[Image.Image], product: Image.Image, story: Storyboard,
                 headline: str, card: EndCard) -> None:
        if not scenes:
            raise ValueError("A video needs at least one scene")
        width, height = SIZE
        top, right, bottom, left = SAFE_MARGINS["9:16"]
        safe_width = width - left - right
        ink = hex_rgb(card.colors.get("ink"), INK)
        lines = [story.hook or headline, story.benefit or headline, story.proof or headline]

        label = font(34, bold=False) if card.brand else None
        first = top + (sum(label.getmetrics()) + 16 if label else 0)
        self.shots: list[_Shot] = []
        text_bottom = first
        for i, line in enumerate(lines):
            words = Image.new("RGBA", SIZE, (0, 0, 0, 0))
            if label:
                ImageDraw.Draw(words).text((left, top), card.brand.upper(), font=label,
                                           fill=(255, 255, 255, 225))
            face, wrapped = fit_headline(line, safe_width - 8, sizes=range(112, 59, -4))
            text_bottom = max(text_bottom, draw_lines(words, wrapped, face, (left, first)))
            scene = ImageOps.fit(scenes[i % len(scenes)].convert("RGB"), SIZE,
                                 method=Image.Resampling.LANCZOS)
            self.shots.append(_Shot(scene, words))

        self.shade = scrim(SIZE, text_bottom + round(0.2 * height), ink)
        product_top = text_bottom + 60
        room = height - bottom - product_top
        scale = min(0.78 * safe_width / product.width, 0.92 * room / product.height)
        size = (max(1, int(product.width * scale)), max(1, int(product.height * scale)))
        shown = product.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
        self.product = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        place_product(self.product, shown, (left + (safe_width - shown.width) // 2,
                                            product_top + round((room - shown.height) * 0.55)))
        self.end = end_card(product, story.cta, card)
        # Everything that sits still over the moving scene, merged once so each frame does
        # one composite: the shade and the product, and that plus each shot's words.
        self.still = Image.alpha_composite(self.shade, self.product)
        self.covers = [Image.alpha_composite(self.still, shot.words) for shot in self.shots]

    def _scene(self, i: int, progress: float, cover: Image.Image | None = None) -> Image.Image:
        frame = _zoom(self.shots[i].scene, progress, push_out=i == 0).convert("RGBA")
        frame.alpha_composite(self.still if cover is None else cover)
        return frame

    def _with_words(self, frame: Image.Image, i: int, strength: float) -> Image.Image:
        if strength >= 1:
            frame.alpha_composite(self.shots[i].words)
        elif strength > 0:
            words = self.shots[i].words.copy()
            words.putalpha(words.getchannel("A").point(lambda v: round(v * strength)))
            frame.alpha_composite(words)
        return frame

    def shot(self, i: int, progress: float) -> Image.Image:
        if i < len(self.shots):
            return self._scene(i, progress, self.covers[i]).convert("RGB")
        return _zoom(self.end, progress, push=0.02)

    def keyframes(self) -> list[Image.Image]:
        """One still per shot, in order, as the Reviewer sees them."""
        return [self.shot(i, 0.5) for i in range(len(SHOTS))]

    def _cut(self, i: int, progress: float, mix: float) -> Image.Image:
        """A frame inside the cut from shot i to shot i + 1. Scenes cross-fade under the
        product, which stays put; the old words fade out before the new ones fade in. The
        end card comes in through its own paper colour."""
        if i + 1 < len(self.shots):
            frame = Image.blend(self._scene(i, progress), self._scene(i + 1, 0.0), mix)
            frame = self._with_words(frame, i, 1 - 2 * mix)
            return self._with_words(frame, i + 1, 2 * mix - 1).convert("RGB")
        paper = Image.new("RGB", SIZE, self.end.getpixel((4, 4)))
        if mix < 0.5:
            return Image.blend(self.shot(i, progress), paper, mix * 2)
        return Image.blend(paper, self.shot(i + 1, 0.0), mix * 2 - 1)

    def frames(self):
        """Every frame of the video, in order."""
        starts = [sum(SHOTS[:i]) for i in range(len(SHOTS))]
        for n in range(sum(SHOTS)):
            i = max(k for k, s in enumerate(starts) if s <= n)
            progress = (n - starts[i]) / SHOTS[i]
            if i + 1 < len(SHOTS) and n >= starts[i + 1] - FADE:
                yield self._cut(i, progress, (n - (starts[i + 1] - FADE) + 1) / (FADE + 1))
            else:
                yield self.shot(i, progress)


def _zoom(still: Image.Image, progress: float, push_out: bool = False,
          push: float = PUSH) -> Image.Image:
    """The still seen through a camera that moves in (or out) over the shot."""
    zoom = 1 + push * ((1 - progress) if push_out else progress)
    width, height = still.size
    crop_w, crop_h = width / zoom, height / zoom
    left, top = (width - crop_w) / 2, (height - crop_h) * 0.4
    return still.resize(SIZE, Image.Resampling.BILINEAR,
                        box=(left, top, left + crop_w, top + crop_h))


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


def contact_sheet(squares: list[Image.Image], stills: list[Image.Image]) -> Image.Image:
    """What the Reviewer looks at: the square image for each scene on top, the video's shots
    in order below."""
    cell = 360
    top = len(squares)
    tall = round(cell * SIZE[1] / SIZE[0] * 0.75)
    still_w = round(tall * SIZE[0] / SIZE[1])
    width = max(top * cell, len(stills) * still_w) + 16 * (max(top, len(stills)) + 1)
    sheet = Image.new("RGB", (width, cell + tall + 48), (255, 255, 255))
    for i, image in enumerate(squares):
        sheet.paste(image.convert("RGB").resize((cell, cell)), (16 + i * (cell + 16), 16))
    for i, image in enumerate(stills):
        sheet.paste(image.convert("RGB").resize((still_w, tall)),
                    (16 + i * (still_w + 16), cell + 32))
    return sheet
