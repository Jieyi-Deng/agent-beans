"""Harness and Memory endpoints: what each call cost and saw, and the team's memory, which the
owner can edit. Owner edits win and lock the line; a role can only propose a change to it."""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from pgt.harness import memory as mem
from pgt.harness.report import harness_report
from pgt.harness.requests import inspect_call
from pgt.loops.close import closed_weeks


class MemoryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: Literal["account", "lessons"]
    role: str | None = None
    action: Literal["add", "edit", "strike", "restore"]
    line_id: str | None = None
    text: str = ""


class Decision(BaseModel):
    accept: bool


def _lines(view: mem.StoreView) -> dict:
    return {"version": view.version, "lines": [line.model_dump(mode="json") for line in view.lines]}


def _history(ws, store: str, role: str | None) -> list[dict]:
    out = []
    for e in mem.versions(ws, store, role):
        out.append({"version": e.payload["version"], "week": e.payload.get("week"),
                    "reason": e.payload.get("reason", ""), "author": e.actor, "ts": e.ts,
                    "lines": len(e.payload["lines"])})
    return out


def _fact_proposals(ws) -> list[dict]:
    resolved = {e.payload["proposal_id"]: e.payload
                for e in ws.events.list("fact.proposal_resolved")}
    return [{"id": e.id, "ts": e.ts, "sim_day": e.sim_day, **e.payload,
             "resolved": resolved.get(e.id)} for e in ws.events.list("fact.proposed")]


def memory_state(ws) -> dict:
    lessons = {}
    for role in mem.ROLES:
        view = mem.view(ws, mem.LESSONS, role, reader="owner")
        lessons[role] = {**_lines(view), "history": _history(ws, mem.LESSONS, role)}
    proposals = [{"id": e.id, "ts": e.ts, **e.payload} for e in mem.open_proposals(ws)]
    return {
        "account": {**_lines(mem.view(ws, mem.ACCOUNT)),
                    "history": _history(ws, mem.ACCOUNT, None)},
        "lessons": lessons,
        "weeks": [{"week": r["week"], "stub": r["stub"], "event_id": r["event_id"]}
                  for r in mem.week_records(ws)],
        "closed": sorted(closed_weeks(ws)),
        "notes": mem.notes(ws),
        "proposals": proposals,
        "fact_proposals": _fact_proposals(ws),
        "charter": ws.facts.current().model_dump(mode="json") if ws.facts.version else None,
        "charter_version": ws.facts.version,
        "rejected": [{"id": e.id, "ts": e.ts, **e.payload}
                     for e in ws.events.list("memory.rejected")][-40:],
        "narrowed": [{"id": e.id, "ts": e.ts, **e.payload}
                     for e in ws.events.list("memory.narrowed")][-40:],
        "reverted": [{"id": e.id, "ts": e.ts, **e.payload}
                     for e in ws.events.list("memory.reverted")],
        "collapse": [{"id": e.id, **e.payload} for e in ws.events.list("memory.collapse")],
        "failed": [{"id": e.id, **e.payload} for e in ws.events.list("memory.pass_failed")],
        "empty": [{"id": e.id, **e.payload} for e in ws.events.list("memory.pass_empty")],
        "sent_back": [{"id": e.id, "role": e.payload.get("role"), "topic": e.payload["topic"],
                       "reason": e.payload.get("reason", "")}
                      for e in ws.events.list("output.sent_back")
                      if "/memory/" in (e.payload.get("topic") or "")][-40:],
    }


def calls_for(ws, topic: str | None = None, loop_id: str | None = None) -> list[dict]:
    keep = ("role", "model", "topic", "attempt", "failure", "input_tokens", "cached_tokens",
            "output_tokens", "cost", "wall_time_s", "context_hash")
    out = []
    for e in ws.events.list("model.call", loop_id=loop_id):
        if topic is not None and e.payload.get("topic") != topic:
            continue
        out.append({"event_id": e.id, "ts": e.ts, "loop_id": e.loop_id, "sim_day": e.sim_day,
                    **{k: e.payload.get(k) for k in keep},
                    "inspectable": bool(e.payload.get("context_hash"))})
    return out


def add_memory_routes(app: FastAPI, team, refresh) -> None:
    """`refresh(before)` redoes stale work after a fact change, as the Settings endpoints do."""

    def busy():
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}. Try again after.")

    @app.get("/api/harness")
    async def harness(loop: str | None = None):
        report = harness_report(team.ws, loop)
        report["recent"] = calls_for(team.ws, loop_id=loop)[-60:]
        return report

    @app.get("/api/calls")
    async def calls(topic: str | None = None, loop: str | None = None):
        return {"items": calls_for(team.ws, topic, loop)}

    @app.get("/api/calls/{event_id}")
    async def call(event_id: int):
        try:
            return inspect_call(team.ws, event_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/memory")
    async def memory():
        return memory_state(team.ws)

    @app.get("/api/memory/journal/{role}")
    async def journal(role: str):
        if role not in mem.ROLES:
            raise HTTPException(404, f"No role {role}")
        return {"role": role, "entries": mem.journal(team.ws, role, reader="owner")[-80:]}

    @app.get("/api/memory/week/{week}")
    async def week(week: int):
        record = mem.week_record(team.ws, week)
        if record is None:
            raise HTTPException(404, f"Week {week} has no frozen record yet")
        return record

    @app.post("/api/memory/edit")
    async def edit(body: MemoryEdit):
        busy()
        week = mem.week_of(team.account.day) if team.account else None
        try:
            event_id = mem.owner_edit(team.ws, body.store, body.role, body.action, body.line_id,
                                      body.text, week)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "event_id": event_id}

    @app.post("/api/memory/proposals/{proposal_id}")
    async def decide_memory(proposal_id: int, body: Decision):
        busy()
        try:
            mem.resolve_proposal(team.ws, proposal_id, body.accept)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/facts/proposals/{proposal_id}")
    async def decide_fact(proposal_id: int, body: Decision):
        """The owner accepts or rejects a role's proposed charter change."""
        busy()
        ws = team.ws
        found = [p for p in _fact_proposals(ws) if p["id"] == proposal_id]
        if not found:
            raise HTTPException(404, f"No proposal {proposal_id}")
        proposal = found[0]
        if proposal["resolved"]:
            raise HTTPException(409, "Already decided")
        before = version = ws.facts.version
        if body.accept:
            try:
                facts = ws.facts.current().model_copy(update={proposal["key"]: proposal["value"]})
                version = ws.facts.set(type(facts).model_validate(facts.model_dump()), "user")
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        ws.events.append("fact.proposal_resolved", "user",
                         {"proposal_id": proposal_id, "accepted": body.accept,
                          "key": proposal["key"], "value": proposal["value"],
                          "fact_version": version})
        return {"ok": True, "fact_version": version,
                "refresh": refresh(before) if version != before else False}
