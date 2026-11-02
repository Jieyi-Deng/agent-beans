"""Owner notes reach roles, the Memory and Harness views, and owner decisions on proposals."""

import json

import pytest
from fastapi.testclient import TestClient

from pgt.api.app import create_app, offline_team
from pgt.harness import memory as mem
from pgt.harness.policy import CostMeter
from pgt.harness.protocol import propose_fact
from pgt.llm.runtime import Scope
from tests.api.test_app import _idle


@pytest.fixture
def client(tmp_path):
    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    with TestClient(create_app(team)) as c:
        _idle(c)
        yield c
    team.ws.close()


def ws_of(client):
    return client.app.state.team.ws


def test_a_note_with_an_at_goes_to_that_role_and_reaches_its_next_call(client):
    ws = ws_of(client)
    sent = client.post("/api/messages", json={"text": "Keep the evening campaign steady.",
                                              "to": "media_buyer"}).json()
    assert sent["to"] == "media_buyer"
    assert client.post("/api/run/day").json()["ok"]
    _idle(client)
    note_id = f"n-{sent['id']}"
    (seen,) = [e for e in ws.events.list("note.seen") if e.payload["note_id"] == note_id]
    assert seen.payload["role"] == "media_buyer"
    entry = ws.events.list("journal.entry", since_id=int(seen.payload["entry_id"][2:]) - 1)[0]
    stored = ws.requests.context(entry.payload["context_hash"])
    assert "## Owner notes for you" in stored["prompt"] and note_id in stored["prompt"]
    assert any(e.payload["note_id"] == note_id for e in ws.events.list("note.done"))
    # The Analyst never saw it.
    analyst = [e for e in ws.events.list("context.assembled") if e.payload["role"] == "analyst"]
    assert all(note_id not in json.dumps(ws.requests.context(a.payload["context_hash"]))
               for a in analyst if a.payload.get("context_hash"))


def test_a_note_without_an_at_goes_to_the_growth_lead_who_hands_it_on_or_answers(client):
    ws = ws_of(client)
    handed = client.post("/api/messages",
                         json={"text": "Lower the budget on the weakest campaign."}).json()
    assert handed["to"] == "growth_lead"
    _idle(client)
    asked = client.post("/api/messages", json={"text": "How is the week going?"}).json()
    _idle(client)
    triaged = {e.payload["note_id"]: e.payload for e in ws.events.list("note.triaged")}
    assert triaged[f"n-{handed['id']}"]["action"] == "handoff"
    assert triaged[f"n-{handed['id']}"]["to"] == "media_buyer"
    handoffs = [e.payload for e in ws.events.list("task.handoff")
                if e.payload["from"] == "growth_lead" and e.payload["to"] == "media_buyer"]
    assert any(f"n-{handed['id']}" in h["task"] for h in handoffs)
    routes = [(r["to"], r["by"]) for n in mem.notes(ws) if n["id"] == f"n-{handed['id']}"
              for r in n["routed"]]
    assert routes == [("growth_lead", "owner"), ("media_buyer", "growth_lead")]
    assert triaged[f"n-{asked['id']}"]["action"] == "answer"
    room = client.get("/api/room").json()["items"]
    thread = [i for i in room if i.get("id") == asked["id"]]
    assert thread and thread[0]["replies"][0]["who"]["actor"] == "growth_lead"


def test_a_send_back_note_goes_to_the_role_that_redoes_the_work(client):
    ws = ws_of(client)
    key = next(d["key"] for d in client.get("/api/deliverables").json()["items"]
               if d["status"] == "wait")
    assert key.endswith("/test_plan")
    assert client.post(f"/api/send-back/{key}", json={"note": "One test only on video."}).is_success
    _idle(client)
    (note,) = [n for n in mem.notes(ws) if n["kind"] == "send_back"]
    assert note["routed"][0]["to"] == "growth_lead"
    assert "growth_lead" in note["seen_by"]


def test_memory_view_owner_edits_and_the_call_inspector(client):
    ws = ws_of(client)
    state = client.get("/api/memory").json()
    assert set(state) >= {"account", "lessons", "weeks", "notes", "proposals", "charter"}
    assert set(state["lessons"]) == set(mem.ROLES)
    r = client.post("/api/memory/edit", json={"store": "account", "action": "add",
                                              "text": "The owner prefers video over stills."})
    assert r.is_success
    line = client.get("/api/memory").json()["account"]["lines"][-1]
    assert line["author"] == "owner" and line["locked"]
    assert client.post("/api/memory/edit", json={"store": "account", "action": "strike",
                                                 "line_id": "nope"}).status_code == 404
    assert client.post("/api/memory/edit", json={"store": "lessons", "role": "nobody",
                                                 "action": "add", "text": "x"}).status_code == 400

    harness = client.get("/api/harness").json()
    roles = {r["role"] for r in harness["roles"]}
    assert {"analyst", "growth_lead", "media_buyer"} <= roles
    assert all("cached_tokens" in r for r in harness["roles"])
    calls = client.get("/api/calls", params={"topic": "week1/test_plan"}).json()["items"]
    assert calls and calls[-1]["role"] == "growth_lead"
    seen = client.get(f"/api/calls/{calls[-1]['event_id']}").json()
    assert seen["instructions"].startswith("You are the Growth Lead")
    assert "## Task" in seen["prompt"] and seen["sections"]
    assert client.get("/api/calls/1").status_code == 404
    journal = client.get("/api/memory/journal/analyst").json()["entries"]
    assert journal and all(e["role"] == "analyst" for e in journal)
    assert ws.events.list("memory.access_refused") == []


def test_the_owner_accepts_or_rejects_a_fact_proposal(client):
    team = client.app.state.team
    ws = team.ws
    scope = Scope(ws, CostMeter(ws.events), "weekly-w1", team.account.day)
    before = ws.facts.current().weekly_budget
    first = propose_fact(scope, "growth_lead", "weekly_budget", before + 100, "More room.",
                         was=before)
    second = propose_fact(scope, "analyst", "long_term_goal", "Reach $9 per trial by Q1.",
                          "The trend supports it.", was="")
    r = client.post(f"/api/facts/proposals/{first}", json={"accept": True}).json()
    assert r["ok"] and ws.facts.current().weekly_budget == before + 100
    assert client.post(f"/api/facts/proposals/{first}", json={"accept": True}).status_code == 409
    assert client.post(f"/api/facts/proposals/{second}", json={"accept": False}).is_success
    assert ws.facts.current().long_term_goal == ""
    decided = {p["id"]: p["resolved"]["accepted"]
               for p in client.get("/api/memory").json()["fact_proposals"]}
    assert decided == {first: True, second: False}


def test_the_owner_approves_or_declines_a_loosening_line_from_proposals(client):
    ws = ws_of(client)
    asked = {}
    for text in ("Scale winners earlier once cost per result is under target.",
                 "Skip the second look at a change set when the last two were approved."):
        e = ws.events.append("memory.proposed", "media_buyer", {
            "kind": "loosening", "store": "lessons", "role": "media_buyer", "line_id": "l9",
            "new": True, "text": text, "was": "", "by": "media_buyer", "sources": ["wk-1"],
            "status": "active", "flags": [{"by": "code", "why": "it says it"}],
            "checker": "reviewer", "week": 1})
        asked[text] = e.id
    waiting = client.get("/api/memory").json()["proposals"]
    assert {p["id"] for p in waiting if p.get("kind") == "loosening"} == set(asked.values())
    yes, no = asked.values()
    assert client.post(f"/api/memory/proposals/{yes}", json={"accept": True}).is_success
    assert client.post(f"/api/memory/proposals/{no}", json={"accept": False}).is_success
    assert client.post(f"/api/memory/proposals/{no}", json={"accept": True}).status_code == 409
    state = client.get("/api/memory").json()
    assert not any(p.get("kind") == "loosening" for p in state["proposals"])
    assert {p["id"]: p["accepted"] for p in state["decided"]} == {yes: True, no: False}
    owner = [x for x in state["lessons"]["media_buyer"]["lines"] if x["author"] == "owner"]
    assert [x["text"] for x in owner] == [next(iter(asked))] and owner[0]["locked"]
