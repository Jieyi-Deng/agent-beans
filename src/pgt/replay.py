"""Records a running team as a static, read-only replay for GitHub Pages.

    pgt replay --source http://127.0.0.1:8003 --out replay

Fetches every read endpoint the web app uses and saves each response under data/, named the way
the web app's replay build looks for it. Copies the images the pages show into media/ (re-encoded
as JPEG unless they need transparency) and points the JSON at the copies.

The site is public, so the export refuses anything that is not synthetic sandbox data: a local
file path, something shaped like a key or token, or a real ad account id.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path

from PIL import Image

RANGES = (7, 14, 28)
MEDIA = "/api/media/"
UNSAFE = re.compile(
    r"/Users/|/home/|[A-Za-z]:\\\\|\bsk-[A-Za-z0-9]{8}|\bBearer\s|\bEAA[A-Za-z0-9]{20}"
    r"|\bact_(?!sandbox_)\d+"
)
READ_ONLY = ("This is a recorded replay of a live run, so nothing changes here. "
             "Run Agent Beans locally to connect an account.")

Get = Callable[[str], bytes]


def data_file(path: str) -> str:
    """'/api/dashboard?days=7' -> 'dashboard-7.json', '/api/calls?topic=week1/brief' ->
    'calls-week1/brief.json'. web/src/api.ts maps paths the same way."""
    return re.sub(r"\?\w+=", "-", path.removeprefix("/api/"), count=1) + ".json"


def _paths(get_json: Callable[[str], dict]) -> list[str]:
    """Every GET the pages make: one detail per deliverable the room or list shows, the memory
    view's journals and week records, and each stored request the Harness view can open."""
    fixed = ["/api/status", "/api/room", "/api/deliverables", "/api/settings", "/api/access",
             "/api/drafts", "/api/assets", *(f"/api/dashboard?days={d}" for d in RANGES),
             "/api/memory", "/api/harness"]
    keys = [d["key"] for d in get_json("/api/deliverables")["items"]]
    for item in get_json("/api/room")["items"]:
        card = item.get("card")
        if card and card.get("key") not in keys:
            keys.append(card["key"])
    memory = get_json("/api/memory")
    roles = sorted(memory["lessons"])
    calls = {c["event_id"] for c in get_json("/api/harness")["recent"] if c["inspectable"]}
    for key in keys:
        calls |= {c["event_id"] for c in get_json(f"/api/calls?topic={key}")["items"]
                  if c["inspectable"]}
    return (fixed + [f"/api/deliverables/{k}" for k in keys]
            + [f"/api/calls?topic={k}" for k in keys]
            + [f"/api/calls/{i}" for i in sorted(calls)]
            + [f"/api/memory/journal/{r}" for r in roles]
            + [f"/api/memory/week/{w['week']}" for w in memory["weeks"]])


def _media(get: Get, url: str, out: Path) -> str:
    """Copies one image or video the pages show and returns its path inside the site. Images
    without transparency are saved as JPEG to keep the site small; videos are copied as they
    are."""
    rel = url.removeprefix(MEDIA)
    if rel.endswith(".mp4"):
        target = Path("media") / rel
        (out / target).parent.mkdir(parents=True, exist_ok=True)
        (out / target).write_bytes(get(url))
        return target.as_posix()
    image = Image.open(io.BytesIO(get(url)))
    clear = image.mode in ("RGBA", "LA", "P") and image.convert("RGBA").getextrema()[3][0] < 255
    target = Path("media") / (rel if clear else str(Path(rel).with_suffix(".jpg")))
    (out / target).parent.mkdir(parents=True, exist_ok=True)
    if clear:
        image.save(out / target, optimize=True)
    else:
        image.convert("RGB").save(out / target, quality=85, optimize=True)
    return target.as_posix()


def _rewrite(value, fix: Callable[[str], str]):
    if isinstance(value, dict):
        return {k: _rewrite(v, fix) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, fix) for v in value]
    if isinstance(value, str) and value.startswith(MEDIA):
        return fix(value)
    return value


def _public(path: str, body: dict) -> dict:
    """A replay is never busy and offers no Meta login."""
    if path == "/api/status":
        return {**body, "busy": None, "error": None}
    if path == "/api/access":
        return {**body, "public": True, "meta_login": READ_ONLY, "meta_accounts": [],
                "reads": None}
    return body


def export(get: Get, out: Path) -> dict:
    """Writes the replay into `out` (replacing what was there) and returns its manifest."""
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    copied: dict[str, str] = {}

    def fix(url: str) -> str:
        if url not in copied:
            copied[url] = _media(get, url, out)
        return copied[url]

    def get_json(path: str) -> dict:
        return json.loads(get(path))

    paths = _paths(get_json)
    for path in paths:
        body = _public(path, _rewrite(get_json(path), fix))
        text = json.dumps(body, separators=(",", ":"))
        bad = UNSAFE.search(text)
        if bad:
            shutil.rmtree(out)
            raise ValueError(f"{path} holds {bad.group(0)!r}; a public replay takes synthetic "
                             "sandbox data only")
        target = out / "data" / data_file(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    settings = get_json("/api/settings")
    manifest = {"recorded": date.today().isoformat(), "mode": settings.get("mode"),
                "cost": settings.get("cost"), "model_calls": settings.get("model_calls"),
                "files": len(paths), "images": len(copied)}
    (out / "data" / "replay.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def http_get(base: str) -> Get:
    def get(path: str) -> bytes:
        with urllib.request.urlopen(base.rstrip("/") + path, timeout=60) as res:
            return res.read()
    return get
