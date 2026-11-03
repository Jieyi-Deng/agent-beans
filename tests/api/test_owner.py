"""The owner's name and picture: set in the app or on the command line, shown only on the pages."""

import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pgt.api.app import create_app, offline_team
from pgt.harness import owner
from pgt.harness.requests import inspect_call


def _idle(client: TestClient, limit: float = 60) -> dict:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        status = client.get("/api/status").json()
        if not status["busy"]:
            assert status["error"] is None, status["error"]
            return status
        time.sleep(0.05)
    raise AssertionError("team stayed busy")


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "teal").save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def team(tmp_path):
    t = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    yield t
    t.ws.close()


@pytest.fixture
def client(team):
    with TestClient(create_app(team)) as c:
        _idle(c)
        yield c


def test_names_are_cleaned_and_checked():
    assert owner.clean_name("  Rosa   Diaz ") == "Rosa Diaz"
    assert owner.clean_name("José") == "José"
    for bad in ("", "   ", "a@b", "x" * 41, "Rosa\x00"):
        with pytest.raises(owner.ProfileError):
            owner.clean_name(bad)


def test_the_owner_is_owner_until_named_then_their_name_shows_on_the_pages(client):
    status = client.get("/api/status").json()
    assert (status["owner"], status["owner_set"], status["owner_avatar"]) == ("Owner", False, None)
    assert client.post("/api/owner", json={"name": "a@b"}).status_code == 400
    assert client.post("/api/owner", json={"name": ""}).status_code == 400
    r = client.post("/api/owner", json={"name": "  Rosa   Diaz "})
    assert r.status_code == 200 and r.json()["owner"] == "Rosa Diaz"
    status = client.get("/api/status").json()
    assert status["owner"] == "Rosa Diaz" and status["owner_set"] is True
    items = client.get("/api/room").json()["items"]
    mentions = [i for i in items if i["kind"] == "msg" and i["mention"]]
    assert mentions[-1]["text"].startswith("@Rosa Diaz ")
    client.post("/api/approve/week1/test_plan")
    _idle(client)
    texts = json.dumps(client.get("/api/room").json())
    assert "Approved by Rosa Diaz" in texts


def test_the_name_never_reaches_a_prompt(client, team):
    client.post("/api/owner", json={"name": "Rosa Diaz"})
    client.post("/api/run/day")
    _idle(client)
    calls = [e for e in team.ws.events.list("model.call") if e.payload.get("context_hash")]
    assert calls
    for e in calls:
        assert "Rosa Diaz" not in json.dumps(inspect_call(team.ws, e.id))
    others = [e for e in team.ws.events.list(since_id=0) if e.type != owner.EVENT]
    assert not any("Rosa Diaz" in json.dumps(e.payload) for e in others)


def test_a_picture_is_cropped_square_and_can_be_cleared(client):
    r = client.post("/api/owner/avatar", content=_jpeg(300, 200))
    assert r.status_code == 200
    url = r.json()["owner_avatar"]
    assert url.startswith("/api/media/owner/") and url.endswith(".png")
    picture = Image.open(io.BytesIO(client.get(url).content))
    assert picture.size == (owner.AVATAR_PX, owner.AVATAR_PX)
    assert client.get("/api/status").json()["owner_avatar"] == url
    assert client.post("/api/owner/avatar", content=b"<svg/>").status_code == 415
    assert client.post("/api/owner/avatar", content=b"").status_code == 400
    assert client.delete("/api/owner/avatar").json()["owner_avatar"] is None
    assert client.get("/api/status").json()["owner_avatar"] is None


def test_the_command_line_sets_name_and_picture(team, tmp_path):
    from pgt.cli import _set_owner

    photo = tmp_path / "me.jpg"
    photo.write_bytes(_jpeg(120, 160))
    _set_owner(team, "Jieyi", str(photo))
    assert owner.name(team.ws) == "Jieyi"
    assert owner.avatar_path(team.ws, team.media_dir).is_file()
    _set_owner(team, "Jieyi", None)  # unchanged: nothing new is written
    assert len(team.ws.events.list(owner.EVENT)) == 2
    with pytest.raises(SystemExit):
        _set_owner(team, "a@b", None)
