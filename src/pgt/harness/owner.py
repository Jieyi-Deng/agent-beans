"""The owner's name and picture, as the app shows them.

Set on the first-run screen, in Settings, or with `pgt serve --owner NAME --owner-avatar PATH`,
and kept as `owner.profile` events. No role sees either one: the name is added where the pages
are rendered, so a recorded run can be shown under the name of whoever owns it. Without a
picture the app draws the name's initials.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from pgt.harness.workspace import Workspace

EVENT = "owner.profile"
DEFAULT = "Owner"  # until the owner gives a name
MAX_NAME = 40
AVATAR_PX = 256


class ProfileError(ValueError):
    """A name or picture the app cannot use; the message says why, for the owner."""


def profile(ws: Workspace) -> dict:
    """{"name": str | None, "avatar": path under the media folder | None}."""
    events = ws.events.list(EVENT)
    p = events[-1].payload if events else {}
    return {"name": p.get("name"), "avatar": p.get("avatar")}


def name(ws: Workspace) -> str:
    return profile(ws)["name"] or DEFAULT


def clean_name(text: str) -> str:
    text = " ".join(str(text).split())
    if not text:
        raise ProfileError("Enter the name the team should use for you")
    if len(text) > MAX_NAME:
        raise ProfileError(f"Use a name of up to {MAX_NAME} characters")
    if "@" in text or not text.isprintable():
        raise ProfileError("A name cannot contain @, because the team uses it to mention you")
    return text


def set_name(ws: Workspace, text: str, sim_day: int | None = None) -> dict:
    current = profile(ws)
    new = clean_name(text)
    if new == current["name"]:
        return current
    return _write(ws, {**current, "name": new}, sim_day)


def set_avatar(ws: Workspace, media_dir: str | Path, image: Image.Image | None,
               sim_day: int | None = None) -> dict:
    """Saves `image` cropped to a square as the owner's picture, or clears it with None. The
    caller has already checked that it is an image."""
    current = profile(ws)
    rel = None
    if image is not None:
        side = min(image.width, image.height)
        left, top = (image.width - side) // 2, (image.height - side) // 2
        square = image.convert("RGB").crop((left, top, left + side, top + side))
        square = square.resize((AVATAR_PX, AVATAR_PX), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        square.save(out, "PNG")
        data = out.getvalue()
        rel = f"owner/{hashlib.sha256(data).hexdigest()[:12]}.png"
        path = Path(media_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if rel == current["avatar"]:
        return current
    return _write(ws, {**current, "avatar": rel}, sim_day)


def avatar_path(ws: Workspace, media_dir: str | Path) -> Path | None:
    rel = profile(ws)["avatar"]
    path = Path(media_dir) / rel if rel else None
    return path if path is not None and path.is_file() else None


def _write(ws: Workspace, payload: dict, sim_day: int | None) -> dict:
    ws.events.append(EVENT, "user", payload, sim_day=sim_day)
    return payload
