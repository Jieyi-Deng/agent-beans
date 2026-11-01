"""Owner notes reach roles. A Team Room message with @Role goes to that role; a reply in a thread
goes to the role that started it; anything else goes to the Growth Lead, which hands it on (a
logged handoff) or answers it in the thread. A send-back note goes to the role that redoes the
work. A role sees its notes in its next call, and the note is marked done once it has."""

from __future__ import annotations

from pydantic_ai.models import Model

from pgt.harness import memory as mem
from pgt.harness.policy import CostMeter, SpendCapExceeded
from pgt.harness.protocol import handoff
from pgt.harness.workspace import Workspace
from pgt.llm.runtime import Scope
from pgt.roles.memory_passes import run_note_triage


def _event(ws: Workspace, event_id: int):
    found = ws.events.list(since_id=event_id - 1)
    return found[0] if found and found[0].id == event_id else None


def route_message(ws: Workspace, event, to: str | None, thread: int | None) -> str:
    """Route a Team Room message from the owner. Returns the role it went to."""
    if to not in mem.ROLES and thread:
        root = _event(ws, thread)
        if root is not None and root.actor in mem.ROLES:
            to = root.actor
    return mem.route_note(ws, event, to, by="owner")


SEND_BACK_TO = {"test_plan": "growth_lead", "brief": "growth_lead", "findings": "analyst",
                "research": "strategist"}


def route_send_back(ws: Workspace, event, key: str, author: str | None) -> str:
    """A send-back note goes to the role that redoes the work: the Producer for a week's new
    ads, the Media Buyer for a day's change set, otherwise the author."""
    period, _, kind = key.partition("/")
    if kind == "change_set":
        to = "producer" if period.startswith("week") else "media_buyer"
    else:
        to = SEND_BACK_TO.get(kind.split("/")[0], author)
    return mem.route_note(ws, event, to, by="owner")


def waiting(ws: Workspace) -> list[dict]:
    """Owner messages routed to the Growth Lead by the owner, not yet triaged or acted on."""
    triaged = {e.payload["note_id"] for e in ws.events.list("note.triaged")}
    return [n for n in mem.notes(ws)
            if n["kind"] == "message" and n["id"] not in triaged and not n["done"]
            and any(r["to"] == "growth_lead" and r["by"] == "owner" for r in n["routed"])]


async def triage_notes(ws: Workspace, model: Model, meter: CostMeter | None = None,
                       sim_day: int | None = None) -> list[dict]:
    """The Growth Lead reads each waiting note once: hands it to a role or answers it."""
    done = []
    facts, fv = ws.facts.current(), ws.facts.version
    for note in waiting(ws):
        scope = Scope(ws, meter or CostMeter(ws.events), f"note-{note['id']}", sim_day,
                      memory=False)
        try:
            triage = await run_note_triage(scope, model, facts, fv,
                                           {"id": note["id"], "text": note["text"],
                                            "day": note["day"]})
        except SpendCapExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - the note stays waiting for the next try
            ws.events.append("note.triage_failed", "system",
                             {"note_id": note["id"], "error": f"{type(exc).__name__}: {exc}"[:300]},
                             sim_day=sim_day, loop_id=scope.loop_id)
            continue
        event = _event(ws, note["event_id"])
        if triage.action == "handoff" and triage.to != "growth_lead":
            handoff(scope, "growth_lead", triage.to, triage.answer)
            mem.route_note(ws, event, triage.to, by="growth_lead")
        else:
            ws.events.append("message.posted", "growth_lead",
                             {"text": triage.answer, "to": None, "thread": note["event_id"]},
                             sim_day=sim_day, loop_id=scope.loop_id)
            mem.mark_note_done(ws, note["id"], "growth_lead", "answered in the Team Room")
        result = {"note_id": note["id"], "action": triage.action, "to": triage.to,
                  "reason": triage.reason}
        ws.events.append("note.triaged", "growth_lead", result, sim_day=sim_day,
                         loop_id=scope.loop_id)
        done.append(result)
    return done
