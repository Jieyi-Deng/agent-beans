"""Read models for the web app, derived from the event log and the artifact store.

Nothing here writes. The room is conversation; Deliverables lists every output with one status.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pgt.harness.artifacts import ArtifactRecord
from pgt.harness.events import Event
from pgt.harness.workspace import Workspace

OWNER = "Jieyi"

# role id -> (avatar code, role name, persona name)
ROLES: dict[str, tuple[str | None, str, str]] = {
    "analyst": ("AN", "Analyst", "Tally"),
    "growth_lead": ("GL", "Growth Lead", "Compass"),
    "strategist": ("CS", "Creative Strategist", "Scout"),
    "producer": ("CP", "Creative Producer", "Pixel"),
    "reviewer": ("RV", "Reviewer", "Philly"),
    "media_buyer": ("MB", "Media Buyer", "Dial"),
    "operator": ("OP", "Operator", "Bolt"),
    "user": ("YOU", OWNER, OWNER),
    "system": (None, "System", "System"),
    "policy": (None, "House rules", "House rules"),
}

# artifact kind -> (type label, Deliverables tab)
TYPES = {
    "TestPlan": ("Test plan", "plans"),
    "ChangeSet": ("Change set", "changes"),
    "CreativeSet": ("Creative", "creative"),
    "Findings": ("Findings", "findings"),
    "MarketResearch": ("Research", "findings"),
    "TestReadout": ("Readout", "findings"),
}

HIDDEN = {
    "model.call", "model.cost", "tool.call", "context.assembled", "evidence.added",
    "loop.finished", "loop.stage", "artifact.status", "approval.simulated",
    "approval.voided", "media.call",
}


def role(actor: str) -> dict:
    code, name, persona = ROLES.get(actor, (None, actor.replace("_", " ").title(), actor))
    return {"actor": actor, "code": code, "name": name, "persona": persona}


def period(key: str) -> str:
    head = key.split("/")[0]
    for prefix, label in (("week", "Week"), ("day", "Day")):
        if head.startswith(prefix) and head[len(prefix):].isdigit():
            return f"{label} {head[len(prefix):]}"
    return head


def title(record: ArtifactRecord) -> str:
    """Names follow `{Type} · {Period} [· {Subject}] · v{n}`."""
    parts = record.key.split("/")
    label = TYPES.get(record.kind, (record.kind, ""))[0]
    subject = [parts[-1]] if len(parts) > 2 else []
    return " · ".join([label, period(record.key), *subject, f"v{record.version}"])


def is_empty_change_set(record: ArtifactRecord) -> bool:
    c = record.content
    return record.kind == "ChangeSet" and not c.get("changes") and not c.get("ad_drafts")


def _money(value: float) -> str:
    return f"${value:,.2f}".replace(".00", "")


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


@dataclass
class Index:
    """One pass over the event log, shared by the room and the Deliverables views."""

    events: list[Event]
    published: dict[str, Event] = field(default_factory=dict)  # artifact_id -> publish event
    requested: dict[str, Event] = field(default_factory=dict)  # key -> latest request
    grants: dict[str, list[Event]] = field(default_factory=dict)
    voided: set[int] = field(default_factory=set)
    applied: dict[str, Event] = field(default_factory=dict)  # artifact_id -> applied event
    returned: dict[str, Event] = field(default_factory=dict)  # key -> latest send-back
    simulated: set[int] = field(default_factory=set)  # grant ids that a simulated user made
    models: dict[tuple[str | None, str], list[str]] = field(default_factory=dict)  # (loop, role)
    spend: dict[tuple[str | None, str], float] = field(default_factory=dict)  # (loop, role)

    @classmethod
    def build(cls, ws: Workspace) -> Index:
        idx = cls(ws.events.list())
        last_simulated: dict[str, int] = {}
        for e in idx.events:
            p = e.payload
            if e.type == "artifact.published":
                idx.published[p["artifact_id"]] = e
            elif e.type == "approval.requested":
                idx.requested[p["key"]] = e
            elif e.type == "approval.simulated":
                last_simulated[p["key"]] = e.id
            elif e.type == "approval.granted":
                idx.grants.setdefault(p["key"], []).append(e)
                if p["key"] in last_simulated:
                    idx.simulated.add(e.id)
                    del last_simulated[p["key"]]
            elif e.type == "approval.voided":
                idx.voided.add(p["approval_id"])
            elif e.type == "changeset.applied" and e.refs:
                idx.applied[e.refs[0]] = e
            elif e.type == "approval.returned":
                idx.returned[p["key"]] = e
            elif e.type == "model.call":
                at = (e.loop_id, p["role"])
                idx.spend[at] = idx.spend.get(at, 0.0) + p.get("cost", 0.0)  # failed calls cost too
                if p.get("ok", True):
                    names = idx.models.setdefault(at, [])
                    if p["model"] not in names:
                        names.append(p["model"])
        return idx

    def valid_grant(self, record: ArtifactRecord) -> Event | None:
        grants = self.grants.get(record.key, [])
        if not grants:
            return None
        g = grants[-1]
        if g.id in self.voided or g.payload["content_hash"] != record.content_hash:
            return None
        return g

    def waiting(self, record: ArtifactRecord) -> Event | None:
        """The open request on this exact version, if the user still has to act on it."""
        req = self.requested.get(record.key)
        if req is None or record.artifact_id not in req.refs:
            return None
        later = [g for g in self.grants.get(record.key, []) if g.id > req.id]
        back = self.returned.get(record.key)
        if later or (back and back.id > req.id):
            return None
        return req

    def status(self, record: ArtifactRecord) -> tuple[str, str]:
        if record.status in ("stale", "superseded"):
            return "stale", "Stale"
        if record.artifact_id in self.applied:
            return "applied", "Applied"
        if self.valid_grant(record):
            return "ok", "Approved"
        if self.waiting(record):
            return "wait", "Waiting on you"
        if record.status == "draft":
            return "draft", "Draft"
        return "review", "In review"


def campaign_names(account) -> dict[str, str]:
    if account is None:
        return {}
    return {c.campaign_id: c.name for c in account.state().campaigns}


def subtitle(ws: Workspace, record: ArtifactRecord, names: dict[str, str]) -> str:
    c = record.content
    if record.kind == "TestPlan":
        tests = c.get("tests", [])
        return (f"{_plural(len(tests), 'test')}, {_money(c.get('available_test_budget', 0))} "
                "test budget for the week")
    if record.kind == "ChangeSet":
        drafts, changes = c.get("ad_drafts", []), c.get("changes", [])
        if drafts:
            camps = sorted({names.get(d["campaign_id"], d["campaign_id"]) for d in drafts})
            return f"{_plural(len(drafts), 'new ad')}, created paused in {', '.join(camps)}"
        blocked = sum(1 for g in c.get("gate", []) if not g.get("allowed", True))
        text = _plural(len(changes), "change")
        return text + (f", {blocked} blocked by house rules" if blocked else "")
    if record.kind == "CreativeSet":
        return f'"{c.get("ad_copy", {}).get("headline", "")}"'
    if record.kind == "Findings":
        items = c.get("findings", [])
        act = sum(1 for f in items if f.get("severity") == "act")
        return f"{_plural(len(items), 'finding')}, {act} to act on"
    if record.kind == "MarketResearch":
        insights = c.get("insights", [])
        sources = sum(len(i.get("sources", [])) for i in insights)
        return f"{_plural(len(insights), 'insight')} from {_plural(sources, 'source')}"
    if record.kind == "TestReadout":
        return f"{c.get('recommendation', '').capitalize()}: {c.get('rationale', '')}"
    return ""


def deliverable(ws: Workspace, idx: Index, record: ArtifactRecord, names: dict[str, str]) -> dict:
    cls, label = idx.status(record)
    pub = idx.published.get(record.artifact_id)
    req = idx.requested.get(record.key)
    anchor = req if req and record.artifact_id in req.refs else pub
    return {
        "key": record.key,
        "artifact_id": record.artifact_id,
        "kind": record.kind,
        "tab": TYPES[record.kind][1],
        "title": title(record),
        "sub": subtitle(ws, record, names),
        "from": role(record.author),
        "status": cls,
        "status_label": label,
        "updated": pub.ts if pub else None,
        "sim_day": record.sim_day,
        "discussed": {"event_id": anchor.id, "sim_day": anchor.sim_day, "ts": anchor.ts}
        if anchor else None,
    }


def deliverables(ws: Workspace, account=None, idx: Index | None = None) -> list[dict]:
    idx = idx or Index.build(ws)
    names = campaign_names(account)
    latest: dict[str, ArtifactRecord] = {}
    for record in ws.artifacts.list():
        if record.kind in TYPES:
            latest[record.key] = record  # list() is in publish order, so the last one wins
    items = [deliverable(ws, idx, r, names) for r in latest.values() if not is_empty_change_set(r)]
    return sorted(items, key=lambda d: d["updated"] or "", reverse=True)


# ---------------------------------------------------------------- room


def _msg(e: Event, actor: str, text: str, mention: bool = False, card: dict | None = None) -> dict:
    return {"kind": "msg", "id": e.id, "ts": e.ts, "sim_day": e.sim_day, "who": role(actor),
            "text": (f"@{OWNER} " if mention else "") + text, "mention": mention,
            "card": card, "replies": [], "loop_id": e.loop_id}


def _card(ws: Workspace, idx: Index, record: ArtifactRecord, names: dict[str, str]) -> dict:
    d = deliverable(ws, idx, record, names)
    note = ""
    if d["status"] == "ok":
        g = idx.valid_grant(record)
        note = "Approved by the simulated owner" if g.id in idx.simulated else f"Approved by {OWNER}"
        d["approved_ts"] = g.ts
    elif d["status"] == "applied":
        r = idx.applied[record.artifact_id].payload
        created = r.get("created_ad_ids") or []
        note = (f"Created {', '.join(created)}, all paused" if created
                else f"Applied {_plural(len(r.get('applied', [])), 'change')}")
    d["note"] = note
    return d


def _fold_line(ws: Workspace, e: Event) -> tuple[str, str] | None:
    """(actor, text) for one step the team took between themselves, or None to hide it."""
    p = e.payload
    if e.type == "task.handoff":
        if p.get("from") == "system":
            return "growth_lead", p["task"]
        return p["from"], f"To {role(p['to'])['name']}: {p['task']}"
    if e.type == "question.asked":
        return p["asker"], f"To {role(p['to'])['name']}: {p['question']}"
    if e.type == "question.answered":
        return "analyst", p["answer"]
    if e.type == "review.redo":
        return p.get("by", "reviewer"), f"Sent {p['subject']} back: " + "; ".join(p["issues"])
    if e.type == "policy.blocked":
        return "policy", (f"Blocked change {p['change_index'] + 1} of {p['change_set_id']}: "
                          + ", ".join(r.replace("_", " ") for r in p["rules"]))
    if e.type in ("stage.refused", "question.refused"):
        return e.actor, p.get("reason") or p.get("question") or e.type
    if e.type == "artifact.kept":
        return e.actor, f"Kept {p['key']}: {p['reason']}"
    if e.type == "creative.reused":
        return e.actor, (f"Reused {p['creative_id']} for {p['test_id']}: it passed review and "
                         "nothing it uses has changed.")
    if e.type == "fact.proposed":
        return e.actor, f"Proposed a change to {p.get('key')}: {p.get('reason', '')}"
    if e.type == "artifact.published":
        record = ws.artifacts.get(p["artifact_id"])
        if record is None:
            return None
        c = record.content
        if record.kind == "ReviewVerdict":
            issues = c.get("issues") or []
            return "reviewer", "Checked today's change set: " + (
                "; ".join(issues) if issues else c.get("verdict", "pass"))
        if record.kind == "CreativeReview":
            issues = c.get("issues") or []
            return "reviewer", f"Reviewed {record.key.split('/')[-1]}: " + (
                "; ".join(issues) if issues else "pass")
        if record.kind == "WeeklyBrief":
            return record.author, c.get("summary") or "Wrote the brief for the week."
        if record.kind in TYPES:
            if is_empty_change_set(record):
                return record.author, "No changes needed today."
            return record.author, f"Posted {title(record)}: {subtitle(ws, record, {})}"
        return None
    return None


def _fold(lines: list[dict], loop_id: str | None) -> dict:
    actors: list[str] = []
    for line in lines:
        if line["who"]["actor"] not in actors and line["who"]["code"]:
            actors.append(line["who"]["actor"])
    names = [role(a)["name"] for a in actors]
    if loop_id and loop_id.startswith("daily-d"):
        tail = f"prepared the Day {loop_id[7:]} check"
    elif loop_id and loop_id.startswith("weekly-w"):
        tail = f"worked on the Week {loop_id[8:]} review"
    elif loop_id and loop_id.startswith("refresh-"):
        tail = "re-checked the work built on the old values"
    elif lines and all(line["type"] == "task.handoff" for line in lines):
        tail = "handed out the work"
    elif lines and all(line["type"].startswith("question.") for line in lines):
        tail = "traded questions and answers"
    else:
        tail = f"posted {_plural(len(lines), 'update')}"
    return {"kind": "fold", "id": lines[0]["id"], "loop_id": loop_id,
            "ids": [line["id"] for line in lines],
            "actors": [role(a) for a in actors], "names": names, "tail": tail,
            "ts_from": lines[0]["ts"], "ts_to": lines[-1]["ts"], "sim_day": lines[0]["sim_day"],
            "items": lines}


SETTING_EVENTS = {"access.level", "account.connected", "account.disconnected",
                  "changeset.handed_off", "fact.changed", "refresh.done", "ads.turned_on"}
LEVEL_TEXT = {
    0: "The team reads and reports, and asks me for nothing.",
    1: "Approved changes come back to me to apply in Ads Manager.",
    2: "The Operator applies what I approve. New ads still start paused.",
}


def _setting(ws: Workspace, idx: Index, e: Event, names) -> tuple[str, str, dict | None] | None:
    """(actor, text, card) for a change the owner made in Settings, or None to hide it."""
    from pgt.harness import access

    p = e.payload
    if e.type == "access.level":
        return e.actor, f"Set the team to {access.describe(p['level'])}. {LEVEL_TEXT[p['level']]}", None
    if e.type == "account.disconnected":
        return e.actor, "Disconnected the ad account.", None
    if e.type == "changeset.handed_off":
        record = ws.artifacts.get(e.refs[0]) if e.refs else None
        parts = [_plural(p.get("changes", 0), "approved change")]
        if p.get("ads"):
            parts.append(_plural(p["ads"], "new ad"))
        text = (f"Nothing written: the team is at L1 Propose. Apply {' and '.join(parts)} "
                "yourself in Ads Manager.")
        return "operator", text, _card(ws, idx, record, names) if record else None
    if e.type == "fact.changed" and p.get("version", 1) > 1 and not p.get("setup"):
        before, after = ws.facts.get(p["version"] - 1), ws.facts.get(p["version"])
        parts = [_fact_change(k, getattr(before, k), getattr(after, k))
                 for k in p.get("changed", [])]
        return e.actor, (f"Changed {'; '.join(parts)}. Work built on the old values is marked "
                         "stale until the team re-checks it."), None
    if e.type == "refresh.done":
        return "growth_lead", " ".join(p.get("lines", [])), None
    if e.type == "ads.turned_on":
        ads = " and ".join(f"{p.get('names', {}).get(a, a)} ({a})" for a in p.get("ad_ids", []))
        return "system", f"Simulated owner turned on {ads} in Ads Manager.", None
    return None


def _connected(p: dict) -> str:
    """'Connected Test (act_42) · real account · L1 Propose'."""
    from pgt.harness import access

    level = p.get("level")
    how = "synthetic data" if p.get("kind") == "sandbox" else "real account"
    parts = [f"Connected {p.get('name')} ({p.get('account_id')})", how]
    if level is not None:
        parts.append(access.describe(level))
    return " · ".join(parts)


def _fact_change(key: str, old, new) -> str:
    """'weekly budget from $385 to $280'; house rules name each rule that moved."""
    label = key.replace("_", " ")
    if key == "house_rules":
        a, b = old.model_dump(exclude={"preset"}), new.model_dump(exclude={"preset"})
        moved = [_fact_change(k, a[k], b[k]) for k in b if a[k] != b[k]]
        return f"house rules ({', '.join(moved)})" if moved else "house rules"
    if isinstance(new, bool):
        return f"{label} {'on' if new else 'off'}"
    if not isinstance(new, int | float):
        return label
    if key.endswith(("_share", "_pct")):
        return f"{label} from {old:.0%} to {new:.0%}"
    if key.endswith(("budget", "_cap")) or key.startswith("target_cost"):
        return f"{label} from ${old:,.2f} to ${new:,.2f}".replace(".00", "")
    return f"{label} from {old:g} to {new:g}"


def room(ws: Workspace, account=None, idx: Index | None = None) -> list[dict]:
    idx = idx or Index.build(ws)
    names = campaign_names(account)
    asked = {ref for e in idx.events if e.type == "approval.requested" for ref in e.refs}
    items: list[dict] = []
    roots: dict[str, dict] = {}  # key -> message that asked for approval
    by_id: dict[int, dict] = {}
    fold: list[dict] = []
    fold_loop: str | None = None
    connected_at = -2

    def flush():
        nonlocal fold
        if fold:
            items.append(_fold(fold, fold[0]["loop"] or fold_loop))
            fold = []

    def add(item: dict):
        flush()
        items.append(item)
        by_id[item["id"]] = item

    def reply(root: dict | None, e: Event, actor: str, text: str):
        r = {"id": e.id, "ts": e.ts, "who": role(actor), "text": text}
        if root is None:
            add(_msg(e, actor, text))
        else:
            root["replies"].append(r)

    for e in idx.events:
        p = e.payload
        if e.type in HIDDEN:
            continue
        if e.type == "loop.started":
            flush()
            fold_loop = e.loop_id
            if p.get("loop") == "weekly_review":
                label = f"Day {e.sim_day} · weekly review · Week {p.get('week')}"
            else:
                label = f"Day {e.sim_day} · daily check"
            items.append({"kind": "day", "id": e.id, "label": label, "sim_day": e.sim_day})
            continue
        if e.type == "artifact.published":
            record = ws.artifacts.get(p["artifact_id"])
            if record is None or record.artifact_id in asked:
                continue
            if record.kind == "DailyBrief":
                c = record.content
                text = c.get("headline", "")
                if c.get("blocked"):
                    text += " " + " ".join(c["blocked"])
                cs = ws.artifacts.latest(f"day{c.get('day')}/change_set")
                card = None
                if cs is not None and not is_empty_change_set(cs):
                    card = _card(ws, idx, cs, names)
                add(_msg(e, record.author, text, card=card))
                continue
        if e.type == "approval.requested":
            record = ws.artifacts.get(e.refs[0]) if e.refs else ws.artifacts.latest(p["key"])
            if record is None:
                continue
            what = p.get("what", "deliverable")
            verb = "are" if what.endswith("s") else "is"
            text = f"The {period(record.key)} {what} {verb} ready. "
            text += record.content.get("summary") or subtitle(ws, record, names)
            if record.kind == "ChangeSet":
                text += " Nothing reaches the account until you approve."
            msg = _msg(e, record.author, text.strip(), mention=True,
                       card=_card(ws, idx, record, names))
            add(msg)
            roots[p["key"]] = msg
            continue
        if e.type == "approval.granted":
            record = ws.artifacts.get(e.refs[0]) if e.refs else None
            name = title(record) if record else p["key"]
            if e.id in idx.simulated:
                n = len(p.get("approved", []))
                text = (f"Simulated approval: approved {_plural(n, 'change')} in {name}, only "
                        "the ones the house rules allowed.") if n else \
                    f"Simulated approval: approved {name}."
                add(_msg(e, "system", text))
            else:
                reply(roots.get(p["key"]), e, "user", f"Approved {name}.")
            continue
        if e.type == "approval.returned":
            text = f"Sent back: {p.get('note', '')}".strip()
            if p.get("redo") is False:
                text += " (This day has passed, so nothing is redone. The next check starts fresh.)"
            reply(roots.get(p["key"]), e, "user", text)
            continue
        if e.type == "changeset.applied":
            record = ws.artifacts.get(e.refs[0]) if e.refs else None
            created = p.get("created_ad_ids") or []
            if created:
                text = f"Created {', '.join(created)} in the sandbox, all paused."
            else:
                text = f"Applied {_plural(len(p.get('applied', [])), 'change')} to the sandbox."
                if p.get("skipped"):
                    text += f" Skipped {_plural(len(p['skipped']), 'change')}."
            add(_msg(e, "operator", text, card=_card(ws, idx, record, names) if record else None))
            continue
        if e.type == "changeset.refused":
            add(_msg(e, "operator", p.get("reason") or "Refused to apply without a valid approval."))
            continue
        if e.type == "escalation.raised":
            add(_msg(e, e.actor, p.get("reason", ""), mention=True))
            continue
        if e.type == "message.posted":
            thread = p.get("thread")
            text = (f"@{role(p['to'])['name']} " if p.get("to") else "") + p["text"]
            if thread and thread in by_id:
                by_id[thread]["replies"].append(
                    {"id": e.id, "ts": e.ts, "who": role(e.actor), "text": text})
            else:
                add(_msg(e, e.actor, text))
            continue
        if e.type == "account.connected":  # a line across the room, with the level set with it
            flush()
            connected_at = e.id if "level" in p else -2
            items.append({"kind": "day", "id": e.id, "label": _connected(p),
                          "sim_day": e.sim_day})
            continue
        if e.type == "access.level" and e.id == connected_at + 1:
            continue
        if e.type in SETTING_EVENTS:
            said = _setting(ws, idx, e, names)
            if said is not None:
                add(_msg(e, said[0], said[1], card=said[2]))
            continue
        line = _fold_line(ws, e)
        if line is None:
            continue
        if fold and (fold[-1]["loop"] or fold_loop) != e.loop_id and e.loop_id is not None:
            flush()
        fold.append({"id": e.id, "ts": e.ts, "sim_day": e.sim_day, "type": e.type,
                     "loop": e.loop_id,
                     "who": role(line[0]), "text": line[1]})
    flush()
    # Each role's spend in a loop shows once: on its own message if it posted one, else on the
    # first fold it appears in.
    shown: set[tuple[str | None, str]] = set()
    for it in items:
        if it["kind"] == "msg":
            at = (it.pop("loop_id"), it["who"]["actor"])
            it["models"] = idx.models.get(at, [])
            it["cost"] = None if at in shown else idx.spend.get(at)
            shown.add(at)
    for it in items:
        if it["kind"] == "fold":
            loop = it.pop("loop_id")
            mine = [(loop, a["actor"]) for a in it["actors"]
                    if (loop, a["actor"]) in idx.spend and (loop, a["actor"]) not in shown]
            shown.update(mine)
            it["models"] = list(dict.fromkeys(m for at in mine for m in idx.models.get(at, [])))
            it["cost"] = round(sum(idx.spend[at] for at in mine), 6) if mine else None
    return items


def where(ws: Workspace) -> str:
    """Eyebrow for the room: the loop the team is in."""
    started = ws.events.list("loop.started")
    if not started:
        return "Getting started"
    p = started[-1].payload
    if p.get("loop") == "weekly_review":
        return f"Weekly review · Week {p.get('week')}"
    return f"Daily check · Day {p.get('day')}"


# ---------------------------------------------------------------- detail


def _evidence(ws: Workspace, ids: list[str]) -> list[dict]:
    out = []
    for eid in ids:
        ev = ws.ledger.get(eid)
        out.append({"id": eid, "note": ev.note if ev else "", "values": ev.values if ev else {}})
    return out


def _checks(ws: Workspace, record: ArtifactRecord) -> list[dict]:
    """What was checked in code, as PASS / FAIL rows."""
    c = record.content
    rows = []
    if record.kind == "ChangeSet":
        for g in c.get("gate", []):
            for r in g.get("results", []):
                rows.append({"passed": r["passed"], "text": f"Change {g['change_index'] + 1}: "
                             f"{r['reason']}", "rule": r["rule"]})
        for d in c.get("ad_drafts", []):
            review = ws.artifacts.latest(f"{record.key.split('/')[0]}/review/{d['test_id']}")
            if review is not None:
                issues = review.content.get("issues") or []
                rows.append({"passed": review.content.get("verdict") == "pass",
                             "text": f"Reviewer checked {d['test_id']}: "
                             + ("; ".join(issues) if issues else "every claim traces to evidence"),
                             "rule": "creative_review", "evidence": d.get("evidence_ids", [])})
    return rows


def _media_url(path: str, media_dir: str | None) -> str | None:
    if not media_dir:
        return None
    from pathlib import Path

    try:
        rel = Path(path).resolve().relative_to(Path(media_dir).resolve())
    except ValueError:
        return None
    return f"/api/media/{rel.as_posix()}"


def _creative(ws: Workspace, key: str, media_dir: str | None) -> dict | None:
    record = ws.artifacts.latest(key)
    if record is None:
        return None
    c = record.content
    return {"key": key, "title": title(record), "ad_copy": c.get("ad_copy", {}),
            "test_id": c.get("test_id"),
            "images": [{**img, "url": _media_url(img["path"], media_dir)}
                       for img in c.get("images", [])],
            "video": {**c["video"], "url": _media_url(c["video"]["path"], media_dir)}
            if c.get("video") else None}


def detail(ws: Workspace, key: str, account=None, media_dir: str | None = None) -> dict | None:
    record = ws.artifacts.latest(key)
    if record is None or record.kind not in TYPES:
        return None
    idx = Index.build(ws)
    names = campaign_names(account)
    item = deliverable(ws, idx, record, names)
    c = record.content
    week = record.key.split("/")[0]
    extra: dict = {}
    if record.kind == "ChangeSet" and c.get("ad_drafts"):
        plan = ws.artifacts.latest(f"{week}/test_plan")
        budgets = {t["test_id"]: t.get("weekly_budget") for t in (plan.content["tests"]
                   if plan else [])}
        rows, creatives = [], []
        for d in c["ad_drafts"]:
            cr = _creative(ws, f"{week}/creative/{d['test_id']}", media_dir)
            creatives.append(cr)
            rows.append({"ad": (cr or {}).get("ad_copy", {}).get("headline") or d["name"],
                         "name": d["name"], "test_id": d["test_id"],
                         "campaign": names.get(d["campaign_id"], d["campaign_id"]),
                         "campaign_id": d["campaign_id"], "budget": budgets.get(d["test_id"])})
        extra = {"ads": rows, "creatives": [x for x in creatives if x]}
    elif record.kind == "ChangeSet":
        extra = {"changes": [{**ch, "campaign": names.get(ch["target_id"], ch["target_id"]),
                              "allowed": next((g["allowed"] for g in c.get("gate", [])
                                               if g["change_index"] == i), True)}
                             for i, ch in enumerate(c.get("changes", []))]}
    elif record.kind == "CreativeSet":
        review = ws.artifacts.latest(f"{week}/review/{c.get('test_id')}")
        extra = {"creative": _creative(ws, key, media_dir),
                 "review": review.content if review else None}
    elif record.kind == "TestPlan":
        extra = {"tests": [{**t, "campaign": names.get(t["campaign_id"], t["campaign_id"])}
                           for t in c.get("tests", [])]}
    req = idx.waiting(record)
    headline = (c.get("summary") or subtitle(ws, record, names)).strip()
    return {
        **item,
        "headline": headline,
        "content": c,
        "content_hash": record.content_hash,
        "checks": _checks(ws, record),
        "evidence": _evidence(ws, record.evidence_ids),
        "can_approve": req is not None,
        "approve_blocked": [r["rule"] for r in _checks(ws, record)
                            if not r["passed"] and r["rule"] != "creative_review"],
        "discussion": discussion(ws, idx, record, account),
        "made_with": made_with(ws, record),
        **extra,
    }


def discussion(ws: Workspace, idx: Index, record: ArtifactRecord, account=None) -> dict:
    """The room message that carried this item, with its thread, plus the related team steps."""
    items = room(ws, account, idx)
    root = item_anchor = None
    for it in items:
        if it["kind"] == "msg" and it.get("card") and it["card"]["key"] == record.key:
            root = it
            break
    pub = idx.published.get(record.artifact_id)
    if root is None and pub is not None:
        for it in items:
            if it["kind"] == "fold" and pub.id in it["ids"]:
                item_anchor = it
                break
    anchor = root or item_anchor
    lines: list[dict] = []
    test_id = record.content.get("test_id")
    for it in items:
        if it["kind"] != "fold":
            continue
        for line in it["items"]:
            mine = pub is not None and line["id"] == pub.id
            if mine or (test_id and test_id in line["text"]):
                lines.append(line)
    return {
        "anchor_id": anchor["id"] if anchor else None,
        "anchor_ts": anchor.get("ts") or anchor.get("ts_from") if anchor else None,
        "anchor_who": (anchor.get("who") or (anchor.get("actors") or [None])[0]) if anchor else None,
        "root": root,
        "lines": lines,
    }


# ---------------------------------------------------------------- models and spend


def provider(model: str) -> str:
    return "Baseten" if model.startswith("meta-models/") else "Meta API"


def _tally(calls: list[Event]) -> dict:
    models: dict[str, int] = {}
    for e in calls:
        models[e.payload["model"]] = models.get(e.payload["model"], 0) + 1
    return {
        "models": [{"model": m, "provider": provider(m), "calls": n} for m, n in models.items()],
        "calls": len(calls),
        "failed": sum(1 for e in calls if not e.payload.get("ok", True)),
        "tokens": sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in calls),
        "searches": sum(e.payload.get("searches", 0) for e in calls),
        "cost": round(sum(e.payload["cost"] for e in calls), 4),
        "seconds": round(sum(e.payload.get("wall_time_s", 0) for e in calls), 1),
    }


def usage(ws: Workspace) -> list[dict]:
    """Model calls per role: which models, on which provider, and what they cost."""
    by_role: dict[str, list[Event]] = {}
    for e in ws.events.list("model.call"):
        by_role.setdefault(e.payload["role"], []).append(e)
    order = list(ROLES)
    return [{"who": role(r), **_tally(calls)}
            for r, calls in sorted(by_role.items(),
                                   key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)]


def made_with(ws: Workspace, record: ArtifactRecord) -> dict | None:
    """The model calls the author made for this version: everything the role called since its
    previous published deliverable. Loops run one at a time, so that span is this version's work
    (for creative it includes the Muse Image and SAM 3.1 calls)."""
    pubs = [e for e in ws.events.list("artifact.published") if e.actor == record.author]
    at = next((i for i, e in enumerate(pubs) if e.payload.get("artifact_id") == record.artifact_id),
              None)
    if at is None:
        return None
    lo, hi = (pubs[at - 1].id if at else 0), pubs[at].id
    calls = [e for e in ws.events.list("model.call", since_id=lo)
             if e.id < hi and e.payload["role"] == record.author]
    return _tally(calls) if calls else None
