"""Closing a week: freeze its record, then update team and role memory under the safeguards.

Runs once per week, after the weekly review (or lazily, when the next review starts). Code
writes the week record; no model touches it. Then the Growth Lead merges it into the account
memory and each role that worked this week updates its own lessons. Every proposed line passes
code checks (sources exist, no instructions, never loosens a rule, enough outcomes behind it)
and then the Reviewer's check before it is kept. A memory call that fails holds its lines back;
it never stops the team's work.
"""

from __future__ import annotations

import asyncio
import re

from pydantic_ai.models import Model

from pgt.harness import memory as mem
from pgt.harness import safeguards as sg
from pgt.harness.policy import CostMeter, SpendCapExceeded
from pgt.harness.workspace import Workspace
from pgt.llm.runtime import Scope
from pgt.roles import memory_passes as passes
from pgt.roles.metrics import week_days
from pgt.schemas import MemoryPass

JOURNAL_FOR_PASS = 30  # journal entries a role sees in its lesson pass
SOURCE_CHARS = 200


# --- The week record -----------------------------------------------------------------------------


def _test_index(ws: Workspace) -> dict[str, dict]:
    tests = {}
    for record in ws.artifacts.list(kind="TestPlan"):
        for t in record.content.get("tests", []):
            tests[t["test_id"]] = t
    return tests


def build_week_record(ws: Workspace, week: int) -> dict:
    """What happened in a week, from artifacts and events only."""
    days = set(week_days(week))
    in_week = [e for e in ws.events.list() if e.sim_day in days]

    headlines = []
    for day in sorted(days):
        brief = ws.artifacts.latest(f"day{day}/brief")
        if brief:
            headlines.append({"day": day, "headline": brief.content["headline"]})

    tests = _test_index(ws)
    latest: dict[str, dict] = {}  # one readout per test: the last one this week
    for record in ws.artifacts.list(kind="TestReadout"):
        m = re.fullmatch(r"day(\d+)/readout/(.+)", record.key)
        if not m or int(m.group(1)) not in days or \
                ws.artifacts.latest(record.key).artifact_id != record.artifact_id:
            continue
        c = record.content
        test = tests.get(c["test_id"], {})
        findings = c.get("findings", [])
        latest[c["test_id"]] = {
            "day": int(m.group(1)), "test_id": c["test_id"], "name": test.get("name", ""),
            "angle": test.get("brief", {}).get("angle", ""),
            "exploratory": test.get("exploratory", False),
            "recommendation": c["recommendation"], "rationale": c["rationale"][:300],
            "metric": findings[0]["metric"] if findings else test.get("metric", ""),
            "evidence_ids": sorted({i for f in findings for i in f["evidence_ids"]}),
        }
    readouts = sorted(latest.values(), key=lambda r: (r["day"], r["test_id"]))

    plan = ws.artifacts.latest(f"week{week}/test_plan")
    planned = [{"test_id": t["test_id"], "name": t["name"], "angle": t["brief"]["angle"],
                "exploratory": t.get("exploratory", False), "campaign_id": t["campaign_id"],
                "weekly_budget": t["weekly_budget"], "metric": t["metric"],
                "evidence_ids": t["evidence_ids"]}
               for t in (plan.content.get("tests", []) if plan else [])]

    changes = []
    for e in in_week:
        if e.type != "changeset.applied":
            continue
        record = ws.artifacts.get(e.refs[0]) if e.refs else None
        content = record.content if record else {}
        items = content.get("changes", [])
        changes.append({
            "change_set_id": e.payload["change_set_id"], "day": e.sim_day,
            "applied": [{k: items[i].get(k) for k in ("kind", "target_id", "new_daily_budget")}
                        for i in e.payload["applied"] if i < len(items)],
            "created_ad_ids": e.payload.get("created_ad_ids", []),
        })

    simulated_refs = {e.refs[0] for e in ws.events.list("approval.simulated") if e.refs}
    owner = {"approved": 0, "sent_back": 0, "simulated": 0, "notes": []}
    for e in in_week:
        if e.type == "approval.granted":
            key = "simulated" if e.refs and e.refs[0] in simulated_refs else "approved"
            owner[key] += 1
        elif e.type == "approval.returned":
            owner["sent_back"] += 1
            owner["notes"].append(f"n-{e.id}")
        elif e.type == "message.posted" and e.actor == "user":
            owner["notes"].append(f"n-{e.id}")

    blocked = [{"change_set_id": e.payload.get("change_set_id"), "rules": e.payload.get("rules")}
               for e in in_week if e.type == "policy.blocked"]
    redos = [{"subject": e.payload.get("subject"), "by": e.payload.get("by", e.actor)}
             for e in in_week if e.type == "review.redo"]
    escalations = [e.payload.get("reason", "")[:200] for e in in_week
                   if e.type == "escalation.raised"]
    evidence = sorted({i for r in readouts for i in r["evidence_ids"]}
                      | {i for t in planned for i in t["evidence_ids"]})
    evidence = [i for i in evidence if ws.ledger.has(i)]

    parts = []
    if readouts:
        parts.append(f"{len(readouts)} readout(s): " + ", ".join(
            f"{r['test_id']} {r['recommendation']}" for r in readouts))
    applied = sum(len(c["applied"]) for c in changes)
    created = sum(len(c["created_ad_ids"]) for c in changes)
    parts.append(f"{applied} change(s) applied" + (f", {created} ad(s) created" if created else ""))
    if planned:
        parts.append(f"{len(planned)} test(s) planned")
    verdicts = f"owner approved {owner['approved']}, sent back {owner['sent_back']}"
    if owner["simulated"]:
        verdicts += f" (plus {owner['simulated']} simulated)"
    parts.append(verdicts)
    if blocked:
        parts.append(f"{len(blocked)} gate block(s)")
    return {
        "week": week, "days": [min(days), max(days)], "stub": "; ".join(parts) + ".",
        "headlines": headlines, "readouts": readouts, "tests_planned": planned,
        "changes": changes, "owner": owner, "gate_blocks": blocked, "redos": redos,
        "escalations": escalations, "evidence_ids": evidence,
    }


def freeze_week(ws: Workspace, week: int) -> dict:
    """Write the week record once. A frozen record is never rewritten."""
    found = mem.week_record(ws, week)
    if found is not None:
        return found
    record = build_week_record(ws, week)
    ws.events.append("week.frozen", "system", record, refs=record["evidence_ids"],
                     sim_day=max(week_days(week)), loop_id=f"close-w{week}")
    return mem.week_record(ws, week)


# --- Merging a memory pass -----------------------------------------------------------------------


def _next_number(ws: Workspace, store: str, role: str | None, prefix: str) -> int:
    top = 0
    for event in mem.versions(ws, store, role):
        for raw in event.payload["lines"]:
            if m := re.fullmatch(rf"{prefix}(\d+)", raw["id"]):
                top = max(top, int(m.group(1)))
    return top + 1


def _candidates(ws: Workspace, store: str, role: str | None, current: mem.StoreView,
                out: MemoryPass, author: str, week: int) -> list[mem.Line]:
    """New and changed lines from a pass, with IDs. Unchanged lines are left out."""
    prefix = "a" if store == mem.ACCOUNT else "l"
    by_id = {line.id: line for line in current.lines}
    n = _next_number(ws, store, role, prefix)
    found = []
    for draft in out.lines:
        sources = list(dict.fromkeys(s.strip() for s in draft.sources if s.strip()))
        old = by_id.get(draft.id)
        if old is not None and old.text == draft.text.strip() and old.sources == sources:
            continue
        if old is None:
            line = mem.Line(id=f"{prefix}{n}", text=draft.text.strip(), sources=sources,
                            author=author, created_week=week, last_confirmed_week=week)
            n += 1
        else:
            line = old.model_copy(update={"text": draft.text.strip(), "sources": sources,
                                          "flag": None, "last_confirmed_week": week,
                                          "status": "active"})
        found.append(line)
    return found


def _code_check(ws: Workspace, store: str, role: str | None, line: mem.Line, facts,
                week: int) -> tuple[mem.Line, list[str]]:
    problems = sg.line_problems(ws, store, role, line, facts)
    if store == mem.LESSONS and (when := sg.rolled_back(ws, role, week).get(sg._norm(line.text))):
        problems.append(f"rolled back in week {when} after quality fell; it may come back once "
                        f"{sg.EXPIRE_WEEKS} weeks have passed")
    if store == mem.LESSONS and not problems:
        independent, owner_note = sg.support(ws, role, line.sources)
        if not owner_note and independent == 0:
            problems.append("no outcome behind it: cite journal entries with outcomes, or an "
                            "owner note")
        elif not owner_note and independent < 2:
            line = line.model_copy(update={"status": "watch"})
    return line, problems


def _source_text(ws: Workspace, source: str) -> str:
    if m := re.fullmatch(r"wk-(\d+)", source):
        record = mem.week_record(ws, int(m.group(1)))
        return f"Week {m.group(1)}: {record['stub']}" if record else "missing"
    if m := re.fullmatch(r"(j|o|n|e)-(\d+)", source):
        found = sg._event(ws, int(m.group(2)))
        if found is None:
            return "missing"
        etype, _actor, p = found
        if etype == "journal.entry":
            return f"{p.get('topic')}: {p.get('decided', '')}"[:SOURCE_CHARS]
        if etype == "journal.outcome":
            return f"{p.get('outcome')} on {p.get('topic')}: {p.get('detail', '')}"[:SOURCE_CHARS]
        if etype in {"message.posted", "approval.returned"}:
            return f"Owner: {p.get('text') or p.get('note', '')}"[:SOURCE_CHARS]
        return etype
    if ws.ledger.has(source):
        item = ws.ledger.get(source)
        return str(getattr(item, "values", item))[:SOURCE_CHARS]
    return source


def _keep(lines: list[mem.Line], cap: int) -> list[mem.Line]:
    """At most `cap` active lines and `cap` watch lines; the oldest-confirmed role lines go."""
    out = list(lines)
    for status in ("active", "watch"):
        movable = sorted((line for line in out if line.status == status and not line.locked),
                         key=lambda line: line.last_confirmed_week or line.created_week or 0)
        fixed = sum(1 for line in out if line.status == status and line.locked)
        extra = len(movable) + fixed - cap
        drop = {line.id for line in movable[:max(0, extra)]}
        out = [line for line in out if line.id not in drop]
    return out


def _merged(current: mem.StoreView, accepted: list[mem.Line], drop: list[str],
            cap: int) -> list[mem.Line]:
    changed = {line.id: line for line in accepted}
    locked = {line.id for line in current.lines if line.locked}
    lines = [changed.pop(line.id, line) for line in current.lines
             if line.id not in drop or line.id in locked]
    return _keep(lines + list(changed.values()), cap)


# --- Close the week ------------------------------------------------------------------------------


def closed_weeks(ws: Workspace) -> set[int]:
    return {e.payload["week"] for e in ws.events.list("week.closed")}


async def close_open_weeks(ws: Workspace, model: Model, before: int,
                           meter: CostMeter | None = None) -> None:
    """Close every week before `before` that ran but was not closed yet."""
    done = closed_weeks(ws)
    days = {e.sim_day for e in ws.events.list("loop.started") if e.sim_day}
    for week in range(1, before):
        if week not in done and days & set(week_days(week)):
            await close_week(ws, model, week, meter)


async def close_week(ws: Workspace, model: Model, week: int, meter: CostMeter | None = None,
                     sim_day: int | None = None) -> dict | None:
    if week in closed_weeks(ws):
        return None
    sim_day = sim_day or max(week_days(week))
    loop_id = f"close-w{week}"
    scope = Scope(ws, meter or CostMeter(ws.events), loop_id, sim_day, memory=False)
    log = ws.events.append

    added = mem.record_outcomes(ws)
    record = freeze_week(ws, week)
    facts, fv = ws.facts.current(), ws.facts.version

    evaluated = [r for role in mem.ROLES
                 if (r := sg.evaluate_versions(ws, role, week, loop_id, sim_day))]
    refreshed = {role: sg.refresh_lessons(ws, role, week, loop_id, sim_day)
                 for role in mem.ROLES}
    collapse = sg.collapse_flags(ws, week)
    for flag in collapse:
        log("memory.collapse", "system", flag, sim_day=sim_day, loop_id=loop_id)

    async def guarded(what: str, coro):
        try:
            return await coro
        except SpendCapExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - logged; memory never blocks the team's work
            log("memory.pass_failed", "system", {"pass": what, "week": week,
                                                 "error": f"{type(exc).__name__}: {exc}"[:300]},
                sim_day=sim_day, loop_id=loop_id)
            return None

    # The Growth Lead's merge of the week into the account memory.
    account = mem.view(ws, mem.ACCOUNT)
    week_notes = [{"id": n["id"], "text": n["text"][:300]} for n in mem.notes(ws)
                  if n["day"] is not None and n["day"] in week_days(week)]
    jobs = {"account": passes.run_account_pass(
        scope, model, facts, fv, week, record,
        [{"id": line.id, "text": line.text, "sources": line.sources, "owner": line.locked}
         for line in account.active()],
        week_notes, mem.MAX_ACCOUNT_LINES)}

    # Each role that worked looks back at its own journal.
    lessons: dict[str, mem.StoreView] = {}
    for role in mem.ROLES:
        entries = [e for e in mem.journal(ws, role, reader="system")
                   if e.get("kind") not in mem.MEMORY_KINDS]
        recent = [e for e in entries if (e.get("week") or 0) >= week - 1
                  or any(o.get("week") == week for o in e["outcomes"])]
        flags = [f"{line.id}: {line.flag}" for line in mem.view(ws, mem.LESSONS, role).lines
                 if line.flag and line.status in {"active", "watch"}]
        flags += [f"collapse: {f['reason']}" for f in collapse if f["role"] == role]
        if not any(e.get("week") == week for e in recent) and not flags:
            continue
        current = mem.view(ws, mem.LESSONS, role)
        lessons[role] = current
        shown = [{"id": e["id"], "day": e.get("day"), "topic": e.get("topic"),
                  "decided": e["decided"][:200], "note_to_self": e.get("note_to_self", ""),
                  "outcomes": [{"id": o["id"], "outcome": o["outcome"], "good": o["good"],
                                "detail": (o.get("detail") or "")[:120]}
                               for o in e["outcomes"] if not o.get("simulated")]}
                 for e in recent[-JOURNAL_FOR_PASS:]]
        notes = [{"id": n["id"], "text": n["text"][:300], "day": n["day"]}
                 for n in mem.notes_for(ws, role, sim_day)]
        jobs[role] = passes.run_lesson_pass(
            scope, model, facts, fv, role, week, shown,
            [{"id": line.id, "text": line.text, "sources": line.sources, "status": line.status,
              "owner": line.locked, "flag": line.flag}
             for line in current.lines if line.status in {"active", "watch"}],
            notes, flags, mem.MAX_LESSONS)

    results = dict(zip(jobs, await asyncio.gather(
        *(guarded(name, job) for name, job in jobs.items())), strict=True))

    # Code checks first; then one Reviewer call for everything that passed.
    pending: dict[str, tuple[str, str | None, mem.Line]] = {}
    rejected = []
    drops: dict[str, list[str]] = {}
    for name, out in results.items():
        if out is None:
            continue
        store, role = (mem.ACCOUNT, None) if name == "account" else (mem.LESSONS, name)
        current = account if store == mem.ACCOUNT else lessons[role]
        author = "growth_lead" if store == mem.ACCOUNT else role
        drops[name] = [i for i in out.drop if any(line.id == i for line in current.lines)]
        for line in _candidates(ws, store, role, current, out, author, week):
            line, problems = _code_check(ws, store, role, line, facts, week)
            if problems:
                rejected.append({"store": store, "role": role, "line_id": line.id,
                                 "text": line.text, "sources": line.sources,
                                 "problems": problems, "by": "code"})
            else:
                pending[f"{name}:{line.id}"] = (store, role, line)

    accepted: dict[str, list[mem.Line]] = {name: [] for name in results}
    failed = sorted(k for k, v in results.items() if v is None)
    if pending:
        check = await guarded("check", passes.run_memory_check(
            scope, model, facts, fv, week,
            [{"line_id": key, "store": store, "role": role or "team", "text": line.text,
              "status": line.status,
              "sources": {s: _source_text(ws, s) for s in line.sources[:6]}}
             for key, (store, role, line) in pending.items()]))
        if check is None:
            failed.append("check")
            log("escalation.raised", "reviewer",
                {"role": "reviewer", "reason": f"The memory check for week {week} could not "
                 f"run, so {len(pending)} new memory line(s) were held back."},
                sim_day=sim_day, loop_id=loop_id)
        else:
            verdicts = {v.line_id: v for v in check.verdicts}
            for key, (store, role, line) in pending.items():
                v = verdicts.get(key)
                if v is not None and v.ok:
                    accepted[key.split(":")[0]].append(line)
                else:
                    rejected.append({"store": store, "role": role, "line_id": line.id,
                                     "text": line.text, "sources": line.sources,
                                     "problems": [v.reason if v else "no verdict"],
                                     "by": "reviewer"})
    for r in rejected:
        log("memory.rejected", "system", {**r, "week": week}, sim_day=sim_day, loop_id=loop_id)

    written = {}
    for name, out in results.items():
        if out is None:
            continue
        drop = drops.get(name, [])
        if not accepted[name] and not drop:
            continue
        if name == "account":
            lines = _merged(account, accepted[name], drop, mem.MAX_ACCOUNT_LINES)
            view = mem.write_version(ws, mem.ACCOUNT, None, lines, "growth_lead", week,
                                     f"account pass, week {week}: {out.summary}"[:300],
                                     loop_id=loop_id, sim_day=sim_day, base=account,
                                     dropped=drop)
        else:
            lines = _merged(lessons[name], accepted[name], drop, mem.MAX_LESSONS)
            view = mem.write_version(ws, mem.LESSONS, name, lines, name, week,
                                     f"lesson pass, week {week}: {out.summary}"[:300],
                                     loop_id=loop_id, sim_day=sim_day, base=lessons[name],
                                     dropped=drop)
        written[name] = view.version

    summary = {
        "week": week, "record": f"wk-{week}", "outcomes_added": added,
        "passes": sorted(results), "failed": failed,
        "versions": written, "rejected": len(rejected),
        "watch": sum(1 for lines in accepted.values() for line in lines
                     if line.status == "watch"),
        "rolled_back": [r["role"] for r in evaluated if not r["kept"]],
        "refreshed": {k: v for k, v in refreshed.items() if any(v.values())},
        "collapse": collapse,
    }
    log("week.closed", "system", summary, sim_day=sim_day, loop_id=loop_id)
    return summary
