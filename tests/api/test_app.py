"""Web API on the offline demo week: what waits on the owner, approvals, and the room."""

import json
import shutil
import time
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pgt.api.app import create_app, offline_team
from pgt.api.views import day_one, on_day, period
from pgt.harness.events import Event


def _idle(client: TestClient, limit: float = 60) -> dict:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        status = client.get("/api/status").json()
        if not status["busy"]:
            assert status["error"] is None, status["error"]
            return status
        time.sleep(0.05)
    raise AssertionError("team stayed busy")


@pytest.fixture
def client(tmp_path):
    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    with TestClient(create_app(team)) as c:
        _idle(c)
        yield c
    team.ws.close()


def _waiting(client):
    return [d for d in client.get("/api/deliverables").json()["items"] if d["status"] == "wait"]


def test_seeded_week_waits_on_the_test_plan(client):
    items = client.get("/api/deliverables").json()["items"]
    waiting = _waiting(client)
    assert [d["title"] for d in waiting] == ["Test plan · Week 1 · v1"]
    assert waiting[0]["from"]["name"] == "Growth Lead"
    # Empty daily change sets never show; ones with changes do. Days read as dates.
    titles = {d["title"] for d in items}
    day_one = date.fromisoformat(client.get("/api/status").json()["day_one"])
    assert f"Change set · {day_one:%b} {day_one.day} · v1" not in titles
    day3 = day_one + timedelta(days=2)
    assert f"Change set · {day3:%b} {day3.day} · v1" in titles
    change = next(d for d in items if d["title"] == f"Change set · {day3:%b} {day3.day} · v1")
    assert change["status"] == "applied"


def test_day_one_is_the_day_after_the_sandbox_connects():
    connected = Event(id=1, ts="2026-10-31T20:14:11+00:00", sim_day=0, loop_id=None,
                      actor="system", type="account.connected", payload={}, refs=[])
    start = day_one([connected])
    assert start == datetime.fromisoformat(connected.ts).astimezone().date() + timedelta(days=1)
    real = connected.model_copy(update={"id": 2, "payload": {"origin": "2026-10-24"}})
    assert day_one([connected, real]) == date(2026, 10, 24)
    assert on_day(date(2026, 11, 1), 3) == "Nov 3"
    assert on_day(date(2026, 11, 1), 3, weekday=True) == "Tue Nov 3"
    assert on_day(None, 3) == "Day 3"
    assert period("day12/change_set", date(2026, 11, 1)) == "Nov 12"
    assert period("week2/test_plan", date(2026, 11, 1)) == "Week 2"


def test_room_mentions_owner_and_folds_handoffs(client):
    items = client.get("/api/room").json()["items"]
    mentions = [i for i in items if i["kind"] == "msg" and i["mention"]]
    assert mentions and mentions[-1]["text"].startswith("@Owner ")  # until the owner gives a name
    assert mentions[-1]["card"]["status"] == "wait"
    assert any(i["kind"] == "fold" and i["tail"] == "worked on the Week 1 review" for i in items)
    simulated = [i for i in items if i["kind"] == "msg" and i["who"]["actor"] == "system"]
    assert simulated and simulated[0]["text"].startswith("Simulated approval")
    hidden = {"model.call", "evidence.added"}
    assert not any(i.get("type") in hidden for i in items)


def test_approving_plan_then_change_set_creates_paused_ads(client):
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)
    assert [d["title"] for d in _waiting(client)] == ["Change set · Week 1 · v1"]
    detail = client.get("/api/deliverables/week1/change_set").json()
    assert detail["can_approve"] and len(detail["ads"]) == 2
    url = detail["creatives"][0]["images"][0]["url"]
    assert client.get(url).status_code == 200
    assert client.post("/api/approve/week1/change_set").status_code == 200
    _idle(client)
    detail = client.get("/api/deliverables/week1/change_set").json()
    assert detail["status"] == "applied"
    room = client.get("/api/room").json()["items"]
    assert any(i["kind"] == "msg" and i["who"]["actor"] == "operator"
               and "all paused" in i["text"] for i in room)
    # Approving again is refused: nothing waits on the owner any more.
    assert client.post("/api/approve/week1/change_set").status_code == 409


def test_send_back_redoes_the_plan_and_asks_again(client):
    room = client.get("/api/room").json()["items"]
    root = next(i for i in room if i["kind"] == "msg" and i["mention"])
    client.post("/api/messages", json={"text": "Why lunch break?", "thread": root["id"]})
    assert client.post("/api/send-back/week1/test_plan", json={"note": " "}).status_code == 400
    res = client.post("/api/send-back/week1/test_plan", json={"note": "Lower the budget"})
    assert res.status_code == 200 and res.json()["redo"] is True
    _idle(client)
    room = client.get("/api/room").json()["items"]
    root = next(i for i in room if i["id"] == root["id"])
    assert [r["text"] for r in root["replies"]] == ["Why lunch break?",
                                                     "Sent back: Lower the budget"]
    # The Growth Lead redrafts with the note, and the new version waits on the owner.
    assert [d["title"] for d in _waiting(client)] == ["Test plan · Week 1 · v2"]
    v2 = client.get("/api/deliverables/week1/test_plan").json()
    assert v2["status"] == "wait"
    assert any("Redo the test plan with the owner's note: Lower the budget" in str(i)
               for i in room if i["kind"] == "fold")
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)


def test_send_back_redoes_the_new_ads_as_new_versions(client):
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)
    before = client.get("/api/deliverables/week1/change_set").json()
    note = "Use a calmer headline"
    assert client.post("/api/send-back/week1/change_set", json={"note": note}).status_code == 200
    _idle(client)
    assert [d["title"] for d in _waiting(client)] == ["Change set · Week 1 · v2"]
    after = client.get("/api/deliverables/week1/change_set").json()
    old_url = before["creatives"][0]["images"][0]["url"]
    new_url = after["creatives"][0]["images"][0]["url"]
    assert "-c1/" in old_url and "-c2/" in new_url
    assert [a["ad"] for a in before["ads"]] != [a["ad"] for a in after["ads"]]
    # Earlier images stay on disk; the redo writes its own.
    assert client.get(old_url).status_code == 200 and client.get(new_url).status_code == 200
    assert client.post("/api/approve/week1/change_set").status_code == 200
    _idle(client)
    assert client.get("/api/deliverables/week1/change_set").json()["status"] == "applied"


def test_media_stays_inside_the_media_folder(client):
    assert client.get("/api/media/../ws.db").status_code == 404


def test_settings_report_models_and_spend_per_role(client):
    s = client.get("/api/settings").json()
    assert s["mode"] == "offline" and s["cap"] is None and s["remaining"] is None
    roles = {u["who"]["actor"]: u for u in s["usage"]}
    assert {"analyst", "growth_lead", "strategist"} <= set(roles)
    assert roles["analyst"]["models"][0] == {
        "model": "muse-spark-1.3", "provider": "Meta API", "calls": roles["analyst"]["calls"]}
    assert sum(u["calls"] for u in s["usage"]) == s["model_calls"]


def test_messages_and_deliverables_name_their_models(client):
    items = client.get("/api/room").json()["items"]
    plan_msg = next(i for i in items if i["kind"] == "msg" and i["mention"])
    assert plan_msg["models"] == ["muse-spark-1.3"]
    d = client.get("/api/deliverables/week1/test_plan").json()
    assert d["made_with"]["calls"] >= 1
    assert d["made_with"]["models"][0]["model"] == "muse-spark-1.3"


def test_room_shows_every_dollar_of_model_spend_once(client):
    items = client.get("/api/room").json()["items"]
    plan_msg = next(i for i in items if i["kind"] == "msg" and i["mention"])
    assert plan_msg["cost"] > 0
    folds = [i for i in items if i["kind"] == "fold" and i["cost"]]
    assert folds and all(f["models"] for f in folds)
    shown = sum(i.get("cost") or 0 for i in items if i["kind"] in ("msg", "fold"))
    assert shown == pytest.approx(client.get("/api/settings").json()["cost"], abs=1e-3)


def test_dashboard_sums_the_insight_rows_and_pins_team_actions(client):
    d = client.get("/api/dashboard").json()
    assert d["ready"] and (d["start"], d["end"]) == (1, 7)
    kpis = {k["key"]: k for k in d["kpis"]}
    series_spend = round(sum(s["spend"] for s in d["series"]), 2)
    assert kpis["spend"]["value"] == pytest.approx(series_spend)
    assert kpis["results"]["value"] == sum(s["results"] for s in d["series"])
    assert kpis["cpr"]["hero"] and kpis["cpr"]["tone"] == "good"
    assert d["evidence"]["rows"] == 28 and d["evidence"]["first"] == "ev-d1-ad_1"
    flag, change = d["actions"][0], d["actions"][1]
    assert (flag["day"], flag["who"]["name"], flag["verb"]) == (3, "Analyst", "flagged")
    assert "ev-d3-ad_1-ctr" in flag["evidence"]
    assert change["who"]["name"] == "Operator" and change["link"]["key"] == "day3/change_set"
    assert "paused Streak" in change["text"] and "simulated owner" in change["text"]
    assert d["after"].startswith("Cost per trial was")
    streak = next(a for c in d["campaigns"] for a in c["ads"] if a["name"] == "Streak")
    assert streak["status_label"] == "Paused day 3"
    assert client.get("/api/dashboard?days=14").json()["short"] is True


@pytest.mark.video
def test_assets_trace_each_creative_and_launch(client):
    assert [a["kind"] for a in client.get("/api/assets").json()["items"]] == []
    client.post("/api/approve/week1/test_plan")
    _idle(client)
    items = client.get("/api/assets").json()["items"]
    made = [a for a in items if a["kind"] == "made"]
    assert len(made) == 2 and all(a["status_label"] == "In change set" for a in made)
    steps = [s["title"] for s in made[0]["steps"]]
    assert steps[1] == "Cut out the phone" and steps[-1].startswith("Reviewer passed")
    assert made[0]["steps"][1]["model"]["model"] == "SAM 3.1"
    assert made[0]["steps"][3]["model"]["model"] == "Muse Image"
    assert made[0]["image"].startswith("/api/media/week1/")
    assert len(made[0]["images"]) == 9 and len(made[0]["scenes"]) == 3
    if shutil.which("ffmpeg"):
        assert made[0]["video"].endswith("/video.mp4")
        assert "10-second video, assembled in code" in steps
        assert client.get(made[0]["video"]).headers["content-type"] == "video/mp4"
    assert any(a["kind"] == "source" for a in items)
    client.post("/api/approve/week1/change_set")
    _idle(client)
    made = [a for a in client.get("/api/assets").json()["items"] if a["kind"] == "made"]
    assert {a["status_label"] for a in made} == {"Created paused"}
    assert all(dict(a["facts"])["Ad"].startswith("ad_") for a in made)
    ops = client.get("/api/dashboard").json()["actions"]
    assert "created 2 paused ads" in ops[-1]["text"].lower() and "Owner approved" in ops[-1]["text"]


def test_upload_accepts_images_only(client):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    r = client.post("/api/assets/upload?name=../../shot.jpg", content=buf.getvalue())
    assert r.status_code == 200
    up = next(a for a in client.get("/api/assets").json()["items"] if a["kind"] == "upload")
    assert up["title"] == "shot.jpg" and up["after"] == "40 x 30"
    assert client.get(up["image"]).status_code == 200
    assert client.post("/api/assets/upload", content=b"<svg/>").status_code == 415
    assert client.post("/api/assets/upload", content=b"").status_code == 400


def test_seeded_history_starts_connected_and_reconnects_with_a_level_and_rules(client):
    a = client.get("/api/access").json()
    assert a["connection"]["name"] == "Beans' Active sandbox" and a["level"] == 2
    assert client.get("/api/room").json()["items"][0]["label"].startswith(
        "Connected Beans' Active sandbox")
    assert a["meta_login"]  # no Meta app configured in tests
    r = client.post("/api/access/connect",
                    json={"kind": "sandbox", "level": 1, "rules": {"daily_budget_cap": 80}})
    assert r.status_code == 200
    a = client.get("/api/access").json()
    assert a["connection"]["account_id"] == "act_sandbox_0001"
    assert a["level"] == 1 and a["rules"]["daily_budget_cap"] == 80
    assert a["rules"]["preset"] == "custom" and a["fact_version"] == 2
    items = client.get("/api/room").json()["items"]
    texts = [i["text"] for i in items if i["kind"] == "msg"]
    lines = [i["label"] for i in items if i["kind"] == "day"]
    assert lines[-1].startswith("Connected Beans' Active sandbox (act_sandbox_0001)") and lines[-1].endswith("L1 Propose")
    assert not any(t.startswith("Set the team to") for t in texts)  # said in the line above
    assert any(t.startswith("Changed house rules (daily budget cap from $") and "to $80)" in t
               for t in texts)
    # A real account cannot be connected without the Meta login.
    assert client.post("/api/access/connect", json={"kind": "meta", "account_id": "act_1"}
                       ).status_code == 409
    assert client.get("/api/meta/login", follow_redirects=False).status_code == 409


def test_house_rules_reject_values_out_of_range(client):
    assert client.post("/api/house-rules", json={"daily_budget_cap": -5}).status_code == 422
    assert client.post("/api/house-rules", json={"max_budget_change_pct": 3}).status_code == 422
    assert client.post("/api/house-rules", json={"anything": 1}).status_code == 422
    r = client.post("/api/house-rules", json={"preset": "conservative"}).json()
    rules = client.get("/api/access").json()["rules"]
    assert r["fact_version"] == 2 and rules["preset"] == "conservative"
    assert rules["max_budget_change_pct"] == 0.10 and rules["daily_budget_cap"] == 60


def test_levels_gate_approvals_and_the_operator(client):
    client.post("/api/access/level", json={"level": 0})
    assert client.post("/api/approve/week1/test_plan").status_code == 409
    assert client.post("/api/run/week").status_code == 409
    client.post("/api/access/level", json={"level": 1})
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)
    r = client.post("/api/approve/week1/change_set").json()
    assert r["handed_off"]
    _idle(client)
    detail = client.get("/api/deliverables/week1/change_set").json()
    assert detail["status"] == "ok"  # approved, not applied
    room = client.get("/api/room").json()["items"]
    assert any(i["kind"] == "msg" and i["who"]["actor"] == "operator"
               and i["text"].startswith("Nothing written: the team is at L1 Propose") for i in room)


def test_operator_refuses_below_l2_even_with_an_approval(tmp_path):
    from pgt.harness import access
    from pgt.roles.operator import apply_change_set

    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    with TestClient(create_app(team)) as c:
        _idle(c)
        key = team.ws.events.list("approval.granted")[0].payload["key"]  # a simulated day
        access.set_level(team.ws, 1)
        with pytest.raises(PermissionError, match="L1 Propose"):
            apply_change_set(team.ws, team.account, key)
        with pytest.raises(PermissionError):
            access.set_level(team.ws, 2, actor="operator")
    team.ws.close()


def test_budget_change_resplits_the_plan_and_says_what_reran(client):
    assert client.post("/api/facts", json={"weekly_budget": -5}).status_code == 422
    calls = client.get("/api/settings").json()["model_calls"]
    res = client.post("/api/facts", json={"weekly_budget": 280})
    assert res.status_code == 200 and res.json()["refresh"] is True
    _idle(client)
    settings = client.get("/api/settings").json()
    assert settings["facts"]["weekly_budget"] == 280 and settings["model_calls"] == calls
    assert [d["title"] for d in _waiting(client)] == ["Test plan · Week 1 · v2"]
    msgs = [i for i in client.get("/api/room").json()["items"] if i["kind"] == "msg"]
    texts = [m["text"] for m in msgs]
    assert any(t.startswith("Changed weekly budget from $385 to $280.") for t in texts)
    assert any("re-split $77 to $56" in t and "no model was called" in t for t in texts)
    # The re-split plan was made in code, so its message names no model.
    (v2,) = [m for m in msgs if (m.get("card") or {}).get("title") == "Test plan · Week 1 · v2"]
    assert v2["models"] == []
    # Saving the same values again changes nothing and re-runs nothing.
    res = client.post("/api/facts", json={"weekly_budget": 280})
    assert res.json()["refresh"] is False


def test_real_account_runs_in_its_own_workspace_at_l2(client, tmp_path):
    from tests.unit.test_ads_mcp import DRAFT_SCHEMA, FakeMcp
    from tests.unit.test_meta_account import TOKEN, FakeGraph

    _idle(client)
    team = client.app.state.team
    sandbox_ws, sandbox_day = team.ws, team.account.day
    graph = FakeGraph()
    client.app.state.graph_transport = graph
    client.app.state.mcp_transport = FakeMcp(graph, schema=DRAFT_SCHEMA)
    team.meta = {"token": TOKEN, "accounts": [{"kind": "meta", "account_id": "act_42",
                                               "name": "Test", "currency": "USD"}]}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pgt.ads.meta_login.unavailable", lambda public: None)
        mp.setenv("META_ADSET_IDS", "111")
        r = client.post("/api/access/connect",
                        json={"kind": "meta", "account_id": "act_42", "level": 2,
                              "rules": {"daily_budget_cap": 100}})
        assert r.status_code == 200, r.text
        _idle(client)
    # A cold start: one line for the connect, then the Growth Lead says what it read.
    room = client.get("/api/room").json()["items"]
    assert [i["kind"] for i in room] == ["day", "msg"]
    assert room[0]["label"] == ("Connected Test (act_42) · real account · "
                                "L2 Apply after approval")
    kick = room[1]
    assert kick["who"]["actor"] == "growth_lead"
    assert "Live in the scope you set: 1 ad set with 2 ads, $210.50 spent so far." in kick["text"]
    assert "2 campaigns, 1 ad set and 2 ads (Spring launch, Retargeting)" in kick["text"]
    assert team.ws.facts.version == 1  # the setup rules are the first facts, not a change
    assert team.ws.facts.current().house_rules.daily_budget_cap == 100
    d = client.get("/api/drafts").json()
    assert d["available"] and d["counts"] == {"campaign": 2, "ad set": 1, "ad": 2}
    assert team.real and team.ws is not sandbox_ws
    assert (tmp_path / "meta-act_42.db").exists()
    a = client.get("/api/access").json()
    assert a["connection"]["account_id"] == "act_42" and a["level"] == 2
    assert a["connection"]["origin"] and "token" not in json.dumps(a)
    assert [c.campaign_id for c in team.account.state().campaigns] == ["111"]
    reads = client.get("/api/access").json()["reads"]
    assert reads["mcp"] and reads["reads"]["ad_sets"] == "mcp"
    mcp = client.get("/api/meta/mcp").json()
    assert "ads_get_ad_entities" in mcp["tools"] and mcp["ads_get_ad_entities"]["required"]
    # The offline stand-in only knows the sandbox, so it does not run on real data.
    assert client.post("/api/run/day").status_code == 409

    assert client.post("/api/access/disconnect").status_code == 200
    assert not team.real and team.ws is sandbox_ws and team.account.day == sandbox_day
    assert client.get("/api/access").json()["reads"] is None
    assert client.get("/api/meta/mcp").status_code == 409


def test_a_cold_team_starts_with_an_empty_room_and_reads_the_sandbox_on_connect(tmp_path):
    team = offline_team(str(tmp_path / "w.db"), str(tmp_path / "media"))
    with TestClient(create_app(team, seed_days=0)) as c:
        assert c.get("/api/room").json()["items"] == []
        assert not c.get("/api/dashboard").json()["waiting"]
        assert c.get("/api/drafts").json()["available"] is False
        assert c.post("/api/run/day").json()["detail"] == "Connect an ad account first"
        r = c.post("/api/access/connect",
                   json={"kind": "sandbox", "level": 1, "rules": {"daily_budget_cap": 80}})
        assert r.status_code == 200, r.text
        _idle(c)
        room = c.get("/api/room").json()["items"]
        assert [i["kind"] for i in room] == ["day", "msg"]  # setup rules are not a change
        assert room[1]["who"]["actor"] == "growth_lead"
        assert room[1]["text"].startswith("I read Beans' Active sandbox, a synthetic account.")
        assert team.ws.facts.current().house_rules.daily_budget_cap == 80
        d = c.get("/api/drafts").json()
        assert d["available"] and d["counts"] == {"campaign": 1, "ad set": 1, "ad": 3}
        assert c.get("/api/dashboard").json()["waiting"]
        assert c.post("/api/run/day").status_code == 200
        _idle(c)
        assert c.get("/api/dashboard").json()["ready"]
    team.ws.close()


def test_the_seeded_sandbox_shows_its_drafts_without_a_kickoff(client):
    d = client.get("/api/drafts").json()
    assert d["available"] and d["drafts"][0]["name"] == "New Year resolutions"
    client.post("/api/access/connect", json={"kind": "sandbox", "level": 1})
    _idle(client)
    texts = [i.get("text", "") for i in client.get("/api/room").json()["items"]]
    assert not any(t.startswith("I read Beans' Active sandbox") for t in texts)

def test_the_page_is_never_cached_but_assets_are_served(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<script src='/assets/index-x.js'></script>")
    (dist / "assets" / "index-x.js").write_text("1")
    team = offline_team(str(tmp_path / "w.db"), str(tmp_path / "media"))
    with TestClient(create_app(team, dist)) as c:
        page = c.get("/dashboard")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
        assert c.get("/assets/index-x.js").text == "1"
    team.ws.close()


def test_store_template_seeds_two_weeks_with_revenue(tmp_path):
    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    team.template = "ecommerce"
    with TestClient(create_app(team, seed_days=14)) as c:
        _idle(c, 120)
        waiting = _waiting(c)
        assert [d["title"] for d in waiting] == ["Test plan · Week 2 · v1"]
        assert team.account.day == 14
        # Week 1 was approved by the simulated owner, so its ads exist, paused.
        made = [a for a in c.get("/api/assets").json()["items"] if a["kind"] == "made"]
        assert made
        assert made[0]["steps"][1]["title"] == "Cut out the dog harness"
        assert made[0]["steps"][0]["sub"].startswith("Studio photo")
        board = c.get("/api/dashboard").json()
        assert board["account_name"] == "Northpaw Supply sandbox"
        keys = [k["key"] for k in board["kpis"]]
        assert keys == ["spend", "revenue", "roas", "ctr", "cpm", "results", "cpr"]
        roas = next(k for k in board["kpis"] if k["key"] == "roas")["value"]
        assert 1.5 < roas < 5
        access = c.get("/api/access").json()
        assert access["sandbox"]["name"] == "Northpaw Supply sandbox"
        assert c.get("/api/deliverables").json()["account_name"] == "Northpaw Supply sandbox"
    team.ws.close()


def test_ad_titles_drop_a_leading_test_id():
    from pgt.api.board import _ad_name

    assert _ad_name("w1-t2 Fountain - Ceramic not plastic", "w1-t2") == "Fountain - Ceramic not plastic"
    assert _ad_name("T1 - Time-fit for busy days", "w1-t1") == "Time-fit for busy days"
    assert _ad_name("W1 - Harness padded comfort", "w1-t3") == "Harness padded comfort"
    assert _ad_name("Tiny steps", "w1-t1") == "Tiny steps"
    assert _ad_name("Week one", "w1-t1") == "Week one"
    assert _ad_name(None, "w1-t1") == "w1-t1"
