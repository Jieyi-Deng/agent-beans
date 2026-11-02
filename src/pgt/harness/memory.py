"""Team memory and role memory, kept inside the append-only event log.

Team memory, read by every role:
    charter         the shared facts (owner only; a role can propose a change)
    week records    frozen by code at the end of each weekly review (`week.frozen`)
    account memory  versioned lines written by the Growth Lead once a week (`memory.version`)
    owner notes     Team Room messages and send-back notes, routed to one role (`note.routed`)

Role memory, read only by its own role:
    journal         one `journal.entry` per call, written by code, plus `journal.outcome` events
    lessons         versioned lines written by the role once a week, under the safeguards

Nothing is edited in place: a new version is a new event, and owner edits are events applied on
top of the latest version. An owner line is locked: a role's pass cannot change or drop it, and
any change it tries becomes a proposal the owner accepts or rejects.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel

from pgt.harness.budgets import tokens
from pgt.harness.context import Item, MemorySlice

ROLES = ("analyst", "media_buyer", "growth_lead", "producer", "reviewer", "strategist")
OWNER_READERS = {"owner", "user", "system"}
ACCOUNT, LESSONS = "account", "lessons"
MAX_LESSONS = 8
MAX_ACCOUNT_LINES = 24
NOTE_WINDOW_DAYS = 14
JOURNAL_SHOWN = 12
# Calls about memory itself; they stay out of the journal a role sees and out of quality scores.
MEMORY_KINDS = frozenset({"MemoryPass", "MemoryCheck", "NoteTriage"})
DECIDED_CHARS = 500  # what a call decided, short but complete
EVIDENCE_ID = re.compile(r"\bev-[\w-]*\w")
STUBS_SHOWN = 6

Status = Literal["active", "watch", "inactive", "struck"]


class MemoryAccessError(PermissionError):
    """A role tried to read another role's private memory."""


class Line(BaseModel):
    id: str
    text: str
    sources: list[str]
    author: str  # a role, "owner" or "system"
    locked: bool = False  # owner lines: a role cannot change or drop them
    status: Status = "active"
    created_week: int | None = None
    last_confirmed_week: int | None = None
    flag: str | None = None  # a contradicting outcome, for the role's next lesson pass


class StoreView(BaseModel):
    store: str
    role: str | None
    version: int
    event_id: int | None
    lines: list[Line]

    def active(self) -> list[Line]:
        return [line for line in self.lines if line.status == "active"]

    def watch(self) -> list[Line]:
        return [line for line in self.lines if line.status == "watch"]


def week_of(day: int | None) -> int:
    return max(1, math.ceil((day or 1) / 7))


def period_of(loop_id: str | None, sim_day: int | None = None) -> str:
    """"weekly-w3" -> "week3", "daily-d5" -> "day5"."""
    if loop_id and (m := re.fullmatch(r"weekly-w(\d+)", loop_id)):
        return f"week{m.group(1)}"
    if loop_id and (m := re.fullmatch(r"daily-d(\d+)", loop_id)):
        return f"day{m.group(1)}"
    return f"day{sim_day or 0}"


# --- Versioned stores (account memory, lessons) -----------------------------------------------


def _owner(store: str, role: str | None) -> str:
    return "growth_lead" if store == ACCOUNT else role or "?"


def _matches(event, store: str, role: str | None) -> bool:
    return event.payload.get("store") == store and event.payload.get("role") == role


def versions(ws, store: str, role: str | None = None) -> list:
    role = None if store == ACCOUNT else role
    return [e for e in ws.events.list("memory.version") if _matches(e, store, role)]


def _history_line(ws, store: str, role: str | None, line_id: str) -> Line | None:
    for event in reversed(versions(ws, store, role)):
        for raw in event.payload["lines"]:
            if raw["id"] == line_id:
                return Line(**raw)
    return None


def view(ws, store: str, role: str | None = None, reader: str | None = None) -> StoreView:
    """The current lines of a store: the latest version plus owner edits made since.
    Lessons are private: `reader` must be the role itself or the owner."""
    role = None if store == ACCOUNT else role
    if store == LESSONS and reader is not None and reader != role and reader not in OWNER_READERS:
        ws.events.append("memory.access_refused", reader,
                         {"reader": reader, "store": store, "role": role})
        raise MemoryAccessError(f"{reader} may not read the {role} lessons")
    found = versions(ws, store, role)
    base = found[-1] if found else None
    lines = [Line(**raw) for raw in base.payload["lines"]] if base else []
    since = base.id if base else 0
    for edit in ws.events.list("memory.owner_edit", since_id=since):
        if _matches(edit, store, role):
            lines = _apply_edit(ws, lines, edit)
    for done in ws.events.list("memory.proposal_resolved", since_id=since):
        if _matches(done, store, role) and done.payload.get("accepted"):
            lines = _apply_edit(ws, lines, done)
    return StoreView(store=store, role=role, version=base.payload["version"] if base else 0,
                     event_id=base.id if base else None, lines=lines)


def _apply_edit(ws, lines: list[Line], edit) -> list[Line]:
    p = edit.payload
    action, line_id = p["action"], p.get("line_id")
    week = p.get("week")
    if action == "add":
        return [*lines, Line(id=f"m-{edit.id}", text=p["text"], sources=[f"e-{edit.id}"],
                             author="owner", locked=True, created_week=week,
                             last_confirmed_week=week)]
    out, seen = [], False
    for line in lines:
        if line.id == line_id:
            seen = True
            line = _edited(line, action, p, edit.id)
        out.append(line)
    if not seen and action in {"restore", "edit"}:
        old = _history_line(ws, p["store"], p.get("role"), line_id)
        if old is not None:
            out.append(_edited(old, action, p, edit.id))
    return out


def _edited(line: Line, action: str, p: dict, event_id: int) -> Line:
    sources = [*line.sources, f"e-{event_id}"]
    if action == "edit":
        return line.model_copy(update={"text": p["text"], "author": "owner", "locked": True,
                                       "sources": sources, "status": "active", "flag": None})
    if action == "strike":
        return line.model_copy(update={"status": "struck", "locked": True, "sources": sources})
    if action == "restore":
        return line.model_copy(update={"status": "active", "locked": True, "sources": sources})
    return line


def owner_edit(ws, store: str, role: str | None, action: str, line_id: str | None = None,
               text: str = "", week: int | None = None) -> int:
    """The owner adds, edits, strikes or restores a line. The line becomes the owner's and is
    locked. Returns the event ID."""
    role = None if store == ACCOUNT else role
    if store not in {ACCOUNT, LESSONS} or (store == LESSONS and role not in ROLES):
        raise ValueError("Unknown memory store")
    if action not in {"add", "edit", "strike", "restore"}:
        raise ValueError(f"Unknown action {action}")
    if action in {"add", "edit"} and not text.strip():
        raise ValueError("Say what the line should say")
    if action != "add":
        current = {line.id for line in view(ws, store, role).lines}
        if line_id not in current and _history_line(ws, store, role, line_id) is None:
            raise LookupError(f"No line {line_id}")
    event = ws.events.append(
        "memory.owner_edit", "user",
        {"store": store, "role": role, "action": action, "line_id": line_id,
         "text": text.strip(), "week": week},
    )
    return event.id


def resolve_proposal(ws, proposal_id: int, accepted: bool) -> None:
    """The owner accepts or rejects a role's proposed change to an owner line."""
    found = [e for e in ws.events.list("memory.proposed", since_id=proposal_id - 1)
             if e.id == proposal_id]
    if not found:
        raise LookupError(f"No proposal {proposal_id}")
    p = found[0].payload
    if any(e.payload.get("proposal_id") == proposal_id
           for e in ws.events.list("memory.proposal_resolved")):
        raise ValueError("Already resolved")
    action = "strike" if not p.get("text") else "edit"
    ws.events.append("memory.proposal_resolved", "user",
                     {"proposal_id": proposal_id, "store": p["store"], "role": p.get("role"),
                      "accepted": accepted, "action": action, "line_id": p["line_id"],
                      "text": p.get("text", "")})


def open_proposals(ws) -> list:
    resolved = {e.payload["proposal_id"] for e in ws.events.list("memory.proposal_resolved")}
    return [e for e in ws.events.list("memory.proposed") if e.id not in resolved]


def write_version(ws, store: str, role: str | None, lines: list[Line], author: str,
                  week: int | None, reason: str, loop_id: str | None = None,
                  sim_day: int | None = None, base: StoreView | None = None,
                  dropped: list[str] = ()) -> StoreView:
    """Store a new version holding `lines`. Owner lines from `base` come back verbatim whatever
    `lines` says; a change to one, or dropping one, becomes a proposal for the owner. Lines are
    assumed already checked (see safeguards)."""
    role = None if store == ACCOUNT else role
    base = base or view(ws, store, role)
    locked = {line.id: line for line in base.lines if line.locked}
    given = {line.id: line for line in lines}
    for line_id, owner_line in locked.items():
        mine = given.get(line_id)
        changed = mine is not None and mine.text != owner_line.text
        if changed or (line_id in dropped and owner_line.status != "struck"):
            ws.events.append(
                "memory.proposed", author,
                {"store": store, "role": role, "line_id": line_id,
                 "text": mine.text if changed else "", "was": owner_line.text, "by": author,
                 "sources": mine.sources if changed else []},
                sim_day=sim_day, loop_id=loop_id,
            )
    final = [locked.get(line.id, line) for line in lines]
    final += [line for line_id, line in locked.items() if line_id not in given]
    version = base.version + 1
    ws.events.append(
        "memory.version", author,
        {"store": store, "role": role, "version": version, "week": week, "reason": reason,
         "lines": [line.model_dump(mode="json") for line in final]},
        refs=sorted({s for line in final for s in line.sources}),
        sim_day=sim_day, loop_id=loop_id,
    )
    return view(ws, store, role)


# --- Week records ------------------------------------------------------------------------------


def week_record(ws, week: int) -> dict | None:
    for event in ws.events.list("week.frozen"):
        if event.payload["week"] == week:
            return {"event_id": event.id, **event.payload}
    return None


def week_records(ws) -> list[dict]:
    return [{"event_id": e.id, **e.payload} for e in ws.events.list("week.frozen")]


# --- Journal -------------------------------------------------------------------------------------

SUBJECT_KEYS = ("subject_id", "campaign_id", "ad_id", "target_id", "test_id", "replaces_ad_id")


def subjects_of(data) -> list[str]:
    """The ads, campaigns and tests a call's inputs are about."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in SUBJECT_KEYS and isinstance(value, str) and value:
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return list(dict.fromkeys(found))[:40]


def _fit(variants) -> str:
    """The first variant that fits DECIDED_CHARS, else the last one cut to fit."""
    text = ""
    for text in variants:
        if len(text) <= DECIDED_CHARS:
            return text
    return text[:DECIDED_CHARS]


def summarize(output) -> str:
    """What a call decided, from its typed output: short, but with the reasons and evidence IDs
    a later check needs."""
    name = type(output).__name__
    if hasattr(output, "changes") and hasattr(output, "summary"):
        changes = [f"{c.kind} {c.target_id}" + (f" to ${c.new_daily_budget:g}"
                                                 if c.new_daily_budget is not None else "")
                   for c in output.changes]
        return (f"{len(changes)} change(s): " + "; ".join(changes) if changes
                else f"no changes: {output.summary}")[:DECIDED_CHARS]
    if hasattr(output, "recommendation") and hasattr(output, "test_id"):
        return f"{output.test_id}: {output.recommendation}. {output.rationale}"[:DECIDED_CHARS]
    if hasattr(output, "findings"):
        fs = output.findings
        return _fit(f"{len(fs)} finding(s): " + "; ".join(
            f"{f.subject_id} {f.severity}" + (f": {f.claim[:width]}" if width else "")
            for f in fs) for width in (140, 70, 0))
    if hasattr(output, "tests") and hasattr(output, "summary"):
        def test(t, width):
            ids = f" [{', '.join(t.evidence_ids[:3])}]" if t.evidence_ids else ""
            why = f": {t.hypothesis[:width]}" if width else ""
            return f"{t.name} ({t.brief.product}, {t.brief.angle}){why}{ids}"
        return _fit(f"{len(output.tests)} test(s): " + "; ".join(test(t, w) for t in output.tests)
                    for w in (160, 80, 0))
    if hasattr(output, "verdict") and hasattr(output, "issues"):
        checked = getattr(output, "checked", [])
        return (f"{output.verdict}" + (": " + "; ".join(output.issues) if output.issues else "")
                + (". Checked: " + "; ".join(checked) if checked else ""))[:DECIDED_CHARS]
    if hasattr(output, "headline") and hasattr(output, "concept"):
        layouts = sorted({s.layout for s in getattr(output, "scenes", [])})
        return (f"{output.concept} | {output.headline}"
                + (f" | layouts {', '.join(layouts)}" if layouts else ""))[:DECIDED_CHARS]
    if hasattr(output, "focus") and hasattr(output, "summary"):
        return f"focus: {'; '.join(output.focus)}"[:DECIDED_CHARS]
    if hasattr(output, "insights"):
        return (f"{len(output.insights)} insight(s): " + "; ".join(
            i.angle for i in output.insights))[:DECIDED_CHARS]
    if hasattr(output, "answer"):
        return str(output.answer)[:DECIDED_CHARS]
    for field in ("summary", "text", "action"):
        if isinstance(getattr(output, field, None), str):
            return f"{name}: {getattr(output, field)}"[:DECIDED_CHARS]
    return name


def cited_evidence(output) -> list[str]:
    """Evidence IDs a deliverable cites: every `evidence_ids` list, and IDs written in its text."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "evidence_ids" and isinstance(value, list):
                    found.extend(str(v) for v in value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            found.extend(EVIDENCE_ID.findall(node))

    walk(output.model_dump(mode="json") if isinstance(output, BaseModel) else output)
    return list(dict.fromkeys(found))[:20]


def record_entry(ws, role: str, pack, output, note: str, *, sim_day: int | None,
                 loop_id: str | None, notes_seen: list[str] = ()) -> int:
    """Code writes one journal entry per role call. Returns its event ID."""
    source = pack.source() or {}
    event = ws.events.append(
        "journal.entry", role,
        {"role": role, "topic": pack.topic, "context_hash": pack.context_hash,
         "kind": type(output).__name__, "decided": summarize(output),
         "evidence_ids": cited_evidence(output),
         "note_to_self": note, "subjects": subjects_of(source.get("data", {})),
         "week": week_of(sim_day), "day": sim_day, "notes": list(notes_seen)},
        refs=pack.inputs, sim_day=sim_day, loop_id=loop_id,
    )
    for note_id in notes_seen:
        ws.events.append("note.seen", role, {"note_id": note_id, "role": role,
                                             "entry_id": f"j-{event.id}"},
                         sim_day=sim_day, loop_id=loop_id)
        if type(output).__name__ not in MEMORY_KINDS:
            mark_note_done(ws, note_id, role, f"acted on in j-{event.id}")
    return event.id


def journal(ws, role: str, reader: str) -> list[dict]:
    """A role's journal with outcomes attached. Only the role itself or the owner may read it."""
    if reader != role and reader not in OWNER_READERS:
        ws.events.append("memory.access_refused", reader,
                         {"reader": reader, "store": "journal", "role": role})
        raise MemoryAccessError(f"{reader} may not read the {role} journal")
    outcomes: dict[str, list[dict]] = defaultdict(list)
    for o in ws.events.list("journal.outcome"):
        if o.payload["role"] == role:
            outcomes[o.payload["entry_id"]].append({"id": f"o-{o.id}", **o.payload})
    return [
        {"id": f"j-{e.id}", "event_id": e.id, "loop_id": e.loop_id, **e.payload,
         "outcomes": outcomes.get(f"j-{e.id}", [])}
        for e in ws.events.list("journal.entry") if e.payload["role"] == role
    ]


# --- Outcomes ------------------------------------------------------------------------------------

# review_failed is neutral: the Reviewer's call failed, so nothing was judged.
GOOD = {"passed": True, "approved": True, "redo": False, "sent_back": False,
        "gate_blocked": False}


def _passed_detail(ws, topic: str) -> str:
    """"published", plus what the review checked when a creative passed review. Nothing is
    added that did not happen."""
    if (m := re.fullmatch(r"week(\d+)/creative/(.+)", topic)) is not None:
        review = ws.artifacts.latest(f"week{m.group(1)}/review/{m.group(2)}")
        checked = review.content.get("checked") if review is not None else None
        if review is not None and review.content.get("verdict") == "pass" and checked:
            return "published; review checked: " + "; ".join(checked)
    return "published"


def _readout_detail(ws, record) -> str:
    """A test readout as an outcome: the call, then cost per result against target and the
    results, from the readout's evidence."""
    c = record.content
    head = f"{c.get('test_id')}: {c.get('recommendation')}"
    ids = list(dict.fromkeys([*(i for f in c.get("findings", []) if isinstance(f, dict)
                                for i in f.get("evidence_ids", [])), *record.evidence_ids]))
    found = [(i, item.values) for i in ids
             if (item := ws.ledger.get(i)) is not None and "cost_per_result" in item.values]
    # The test's own totals (with a target) first, then any ad's.
    found.sort(key=lambda f: "target_cost_per_result" not in f[1])
    if found:
        evidence_id, v = found[0]
        target = (f" vs target {v['target_cost_per_result']:g}"
                  if "target_cost_per_result" in v else "")
        results = f", {v['results']:g} results" if "results" in v else ""
        return f"{head}. Cost per result {v['cost_per_result']:g}{target}{results} [{evidence_id}]"
    return f"{head}. {c.get('rationale', '')}".strip(". ")


def record_outcomes(ws) -> int:
    """Attach outcomes the harness logged to the journal entries they judge. Idempotent: each
    (entry, source) pair is recorded once. Returns how many were added."""
    entries = ws.events.list("journal.entry")
    if not entries:
        return 0
    done = {(o.payload["entry_id"], o.payload["source"]) for o in ws.events.list("journal.outcome")}
    added = 0

    def add(entry, outcome: str, source: str, detail: str = "", good: bool | None = None,
            simulated: bool = False):
        nonlocal added
        key = (f"j-{entry.id}", source)
        if key in done:
            return
        done.add(key)
        ws.events.append(
            "journal.outcome", "system",
            {"entry_id": f"j-{entry.id}", "role": entry.payload["role"], "outcome": outcome,
             "good": GOOD.get(outcome) if good is None else good, "detail": detail[:300],
             "source": source, "topic": entry.payload.get("topic"),
             "day": entry.payload.get("day"), "week": entry.payload.get("week"),
             "simulated": simulated},
            sim_day=entry.sim_day, loop_id=entry.loop_id,
        )
        added += 1

    by_topic: dict[str, list] = defaultdict(list)
    for e in entries:
        if e.payload.get("topic"):
            by_topic[e.payload["topic"]].append(e)
    returned = ws.events.list("approval.returned")
    redos = ws.events.list("review.redo")

    # Redone or passed: the next entry on the same topic means this one was redone, unless the
    # owner sent it back in between (that is a send-back, recorded below).
    for topic, items in by_topic.items():
        for i, entry in enumerate(items):
            later = items[i + 1] if i + 1 < len(items) else None
            if later is not None:
                sent_back = any(entry.id < r.id < later.id and r.payload.get("key") == topic
                                for r in returned)
                if not sent_back:
                    why = next((r for r in redos if entry.id < r.id < later.id
                                and r.loop_id == entry.loop_id), None)
                    add(entry, "redo", f"e-{why.id if why else later.id}",
                        "; ".join(why.payload.get("issues", [])) if why else "redone")
                continue
            record = ws.artifacts.latest(topic)
            if record is not None and record.author == entry.payload["role"]:
                add(entry, "passed", record.artifact_id, _passed_detail(ws, topic))

    def latest_before(topic: str, event_id: int):
        items = [e for e in by_topic.get(topic, []) if e.id < event_id]
        return items[-1] if items else None

    def judged(key: str, event_id: int) -> list:
        """Entries an owner decision on `key` judges: the author of that deliverable, and for
        a week's new ads, each creative and its review."""
        found = [latest_before(key, event_id)]
        if (m := re.fullmatch(r"week(\d+)/change_set", key)):
            for topic in by_topic:
                if re.fullmatch(rf"week{m.group(1)}/(creative|review)/.+", topic):
                    found.append(latest_before(topic, event_id))
        return [e for e in found if e is not None]

    # The simulation and the seeded history stand in for the owner: each logs
    # approval.simulated, then the grant itself. Those count as simulated, never as the owner's.
    simulated = {e.refs[0] for e in ws.events.list("approval.simulated") if e.refs}
    for kind, outcome in (("approval.granted", "approved"), ("approval.simulated", "approved"),
                          ("approval.returned", "sent_back")):
        for a in ws.events.list(kind):
            if kind == "approval.granted" and a.refs and a.refs[0] in simulated:
                continue
            key = a.payload.get("key", "")
            for entry in judged(key, a.id):
                add(entry, outcome, f"e-{a.id}", a.payload.get("note", ""),
                    simulated=kind == "approval.simulated")

    for blocked in ws.events.list("policy.blocked"):
        m = re.fullmatch(r"cs-d(\d+)", blocked.payload.get("change_set_id", ""))
        if m and (entry := latest_before(f"day{m.group(1)}/change_set", blocked.id)):
            add(entry, "gate_blocked", f"e-{blocked.id}",
                ", ".join(blocked.payload.get("rules", [])))

    # Test results: a readout judges the plan that proposed the test and the creative made for it.
    for topic, items in list(by_topic.items()):
        m = re.fullmatch(r"day\d+/readout/(w(\d+)-t\d+)", topic)
        record = ws.artifacts.latest(topic) if m else None
        if record is None:
            continue
        rec = record.content.get("recommendation")
        good = rec in {"continue", "scale"}
        test_id, week = m.group(1), m.group(2)
        for judged_topic in (f"week{week}/test_plan", f"week{week}/creative/{test_id}"):
            if (entry := latest_before(judged_topic, 10**12)) is not None:
                add(entry, "test_result", record.artifact_id, _readout_detail(ws, record),
                    good=good)

    for failed in ws.events.list("review.failed"):
        if (entry := latest_before(failed.payload.get("topic", ""), failed.id)) is not None:
            add(entry, "review_failed", f"e-{failed.id}", failed.payload.get("failure", ""))
    return added


# --- Owner notes ---------------------------------------------------------------------------------


def route_note(ws, note_event, to: str | None, by: str = "owner") -> str:
    """Send an owner note to one role. Without an @ it goes to the Growth Lead."""
    to = to if to in ROLES else "growth_lead"
    ws.events.append("note.routed", by,
                     {"note_id": f"n-{note_event.id}", "to": to, "by": by},
                     sim_day=note_event.sim_day)
    return to


def note_text(event) -> str:
    if event.type == "approval.returned":
        return f"Sent back {event.payload.get('key')}: {event.payload.get('note', '')}"
    return event.payload.get("text", "")


def notes(ws) -> list[dict]:
    """Every owner note with where it went and whether it was acted on."""
    events = {e.id: e for e in [*ws.events.list("message.posted"),
                                *ws.events.list("approval.returned")] if e.actor == "user"}
    routed: dict[str, list[dict]] = defaultdict(list)
    for r in ws.events.list("note.routed"):
        routed[r.payload["note_id"]].append({"to": r.payload["to"], "by": r.payload["by"],
                                             "event_id": r.id})
    seen: dict[str, set[str]] = defaultdict(set)
    for s in ws.events.list("note.seen"):
        seen[s.payload["note_id"]].add(s.payload["role"])
    done: dict[str, dict] = {}
    for d in ws.events.list("note.done"):
        done[d.payload["note_id"]] = d.payload
    out = []
    for event_id in sorted(events):
        e = events[event_id]
        note_id = f"n-{event_id}"
        if e.type == "approval.returned" and not e.payload.get("note"):
            continue
        out.append({"id": note_id, "event_id": event_id, "text": note_text(e),
                    "kind": "send_back" if e.type == "approval.returned" else "message",
                    "to": e.payload.get("to"), "day": e.sim_day, "routed": routed.get(note_id, []),
                    "seen_by": sorted(seen.get(note_id, set())), "done": done.get(note_id)})
    return out


def notes_for(ws, role: str, day: int | None) -> list[dict]:
    """Notes routed to this role, open ones and those from the last two weeks."""
    out = []
    for note in notes(ws):
        if not any(r["to"] == role for r in note["routed"]):
            continue
        recent = day is None or note["day"] is None or note["day"] >= (day - NOTE_WINDOW_DAYS)
        if recent or not note["done"]:
            out.append(note)
    return out


def unrouted_notes(ws) -> list:
    routed = {r.payload["note_id"] for r in ws.events.list("note.routed")}
    return [e for e in ws.events.list("message.posted")
            if e.actor == "user" and f"n-{e.id}" not in routed]


def mark_note_done(ws, note_id: str, role: str, how: str) -> None:
    if any(d.payload["note_id"] == note_id for d in ws.events.list("note.done")):
        return
    ws.events.append("note.done", role, {"note_id": note_id, "role": role, "how": how})


# --- What one role may read for one call ---------------------------------------------------------


def _line_text(line: Line) -> str:
    owner = "[owner] " if line.author == "owner" else ""
    return f"{owner}{line.text} ({', '.join(line.sources[:4])}) [{line.id}]"


def slice_for(ws, role: str, *, day: int | None, subjects: list[str] = (),
              topic: str | None = None) -> MemorySlice:
    """Memory a role may read for one call. Code decides; the prompt never asks. A role sees
    team memory, notes routed to it, and only its own journal and lessons."""
    week = week_of(day)
    account = view(ws, ACCOUNT)
    account_items = [
        Item(id=line.id, text=_line_text(line),
             rank=[1 if line.locked else 0, line.last_confirmed_week or 0, n])
        for n, line in enumerate(account.active())
    ]
    stubs = [
        Item(id=f"wk-{r['week']}", text=f"Week {r['week']}: {r['stub']} [wk-{r['week']}]",
             rank=[r["week"]])
        for r in week_records(ws) if r["week"] < week
    ][-STUBS_SHOWN:]
    lessons = view(ws, LESSONS, role, reader=role)
    lesson_items = [
        Item(id=line.id, text=_line_text(line) + (f" Flag: {line.flag}" if line.flag else ""),
             rank=[2, 1 if line.locked else 0, line.last_confirmed_week or 0])
        for line in lessons.active()
    ]
    watch_items = [
        Item(id=line.id, text="Watch (one outcome so far, not a lesson yet): "
             + _line_text(line), rank=[1, line.last_confirmed_week or 0])
        for line in lessons.watch()
    ]
    note_items = []
    for note in notes_for(ws, role, day):
        state = "done" if note["done"] else "open"
        text = note["text"].replace("\n", " ")
        note_items.append(Item(id=note["id"], text=f"[{note['id']}, day {note['day']}, {state}] "
                               f"{text}", rank=[0 if note["done"] else 1, note["event_id"]]))
    wanted = set(subjects)
    entries = [e for e in journal(ws, role, reader=role) if e.get("kind") not in MEMORY_KINDS]
    same = [e for e in entries if wanted & set(e.get("subjects", []))
            or (topic and e.get("topic") and e["topic"].split("/", 1)[-1].split("/")[0]
                == topic.split("/", 1)[-1].split("/")[0])]
    shown = (same or entries)[-JOURNAL_SHOWN:]
    journal_items = []
    for e in shown:
        outcome = "; ".join(f"{o['outcome']}" + (f" ({o['detail'][:80]})" if o["detail"] else "")
                            for o in e["outcomes"][-3:]) or "no outcome yet"
        note = f" Note to self: {e['note_to_self']}" if e.get("note_to_self") else ""
        on_subject = 1 if wanted & set(e.get("subjects", [])) else 0
        journal_items.append(Item(
            id=e["id"], text=f"[{e['id']}] day {e.get('day')} {e.get('topic')}: {e['decided']} "
                             f"-> {outcome}.{note}",
            rank=[on_subject, e["event_id"]]))
    return MemorySlice(
        account_memory=account_items, account_version=account.version or None,
        week_stubs=stubs, lessons=lesson_items, lessons_version=lessons.version or None,
        watch=watch_items, owner_notes=note_items, journal=journal_items,
    )


def recall_week(ws, week: int) -> dict:
    record = week_record(ws, week)
    if record is None:
        return {"error": f"Week {week} has no frozen record yet."}
    return record


def recall_journal(ws, role: str, subject: str | None = None, day: int | None = None,
                   limit: int = 20) -> list[dict]:
    """The caller's own journal, filtered by subject or day. Bound to the calling role in code."""
    entries = journal(ws, role, reader=role)
    if subject:
        entries = [e for e in entries if subject in e.get("subjects", [])
                   or subject in (e.get("topic") or "")]
    if day is not None:
        entries = [e for e in entries if e.get("day") == day]
    keep = ("id", "topic", "day", "decided", "note_to_self", "outcomes")
    return [{k: e.get(k) for k in keep} for e in entries[-limit:]]


def memory_tokens(memory: MemorySlice) -> int:
    return sum(tokens(i.text) for group in (memory.account_memory, memory.week_stubs,
                                            memory.lessons, memory.watch, memory.owner_notes,
                                            memory.journal) for i in group)
