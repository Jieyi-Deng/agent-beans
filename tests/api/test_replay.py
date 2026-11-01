import json

import pytest
from fastapi.testclient import TestClient

from pgt.api.app import create_app, offline_team
from pgt.replay import data_file, export
from tests.api.test_app import _idle


@pytest.fixture
def store(tmp_path):
    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    team.template = "ecommerce"
    with TestClient(create_app(team, seed_days=14)) as c:
        _idle(c, 120)

        def get(path: str) -> bytes:
            r = c.get(path)
            r.raise_for_status()
            return r.content

        yield get
    team.ws.close()


def test_replay_writes_every_view_with_local_media(store, tmp_path):
    out = tmp_path / "site"
    manifest = export(store, out)
    assert data_file("/api/dashboard?days=7") == "dashboard-7.json"
    assert data_file("/api/calls?topic=week2/brief") == "calls-week2/brief.json"
    for name in ("status", "room", "deliverables", "assets", "access", "settings", "drafts",
                 "dashboard-7", "dashboard-28", "deliverables/week2/test_plan", "memory",
                 "harness", "memory/journal/media_buyer", "memory/week/1",
                 "calls-week2/test_plan"):
        assert (out / "data" / f"{name}.json").is_file(), name
    calls = json.loads((out / "data" / "calls-week2/test_plan.json").read_text())["items"]
    seen = json.loads((out / "data" / "calls" / f"{calls[-1]['event_id']}.json").read_text())
    assert seen["role"] == "growth_lead" and seen["sections"]
    text = "".join(p.read_text() for p in (out / "data").rglob("*.json"))
    assert "/api/media/" not in text
    assets = json.loads((out / "data" / "assets.json").read_text())
    images = [a["image"] for a in assets["items"] if a.get("image")]
    assert images and all((out / i).is_file() for i in images)
    access = json.loads((out / "data" / "access.json").read_text())
    assert access["public"] and access["meta_login"] and access["connection"]["kind"] == "sandbox"
    assert manifest["images"] == len(list((out / "media").rglob("*.*")))


def test_replay_refuses_local_paths(store, tmp_path):
    def leaky(path: str) -> bytes:
        body = store(path)
        return body.replace(b"Northpaw", b"/Users/someone/Northpaw") if path == "/api/room" \
            else body

    with pytest.raises(ValueError, match="synthetic"):
        export(leaky, tmp_path / "site")
    assert not (tmp_path / "site").exists()
