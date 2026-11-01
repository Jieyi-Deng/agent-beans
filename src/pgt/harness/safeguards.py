"""Write checks and the guards that keep the feedback loop positive.

Every memory line must cite a source that exists. A lesson needs two independent outcomes or one
owner note; with one outcome it goes on the watch list. Lessons can narrow, never loosen: a line
that permits what the charter or house rules forbid is rejected in code before the Reviewer sees
it, and only an owner line may say "never". Text that reads like an instruction to the model is
rejected wherever it came from. Quality is measured by code, never by the role that wrote the
lesson, and a lessons version that hurts quality is rolled back.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from pgt.harness import memory as mem
from pgt.schemas import SharedFacts

IC = re.IGNORECASE
QUALITY_DROP = 0.15  # roll back a lessons version when quality falls by more than this
MIN_WINDOW = 5  # calls after a version goes live before it is judged
EXPIRE_WEEKS = 3

INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instruction|instructions|rules?|"
    r"guidelines|prompt|policy|policies|house rules)\b"
    r"|\bsystem prompt\b|\byou are now\b|\bnew instructions?\b|\bdeveloper mode\b|\bjailbreak\b"
    r"|\bact as (the )?(owner|admin|system)\b",
    IC,
)
PERMIT = re.compile(
    r"\b(ok|okay|fine|allowed|permitted|safe|acceptable|feel free|go ahead|can|may|should)\b",
    IC)
PAST_A_RULE = re.compile(
    r"\b(exceed|exceeding|bypass|skip|skipping|ignore|ignoring|override|regardless of|"
    r"without (the )?(owner'?s? )?(approval|review|sign-?off)|no need to (ask|wait)|"
    r"past the (cap|limit|rule)|above the (cap|limit)|beyond the (cap|limit))\b", IC)
NEVER = re.compile(r"\b(never|under no circumstances|don'?t ever|do not ever)\b", IC)
BUDGET_WORDS = re.compile(r"\b(budget|raise|increase|scale|cut|lower|change|move)\w*", IC)
# A line that orders a budget move ("Raise the budget to ...") permits it as much as "it is fine to".
ORDER = re.compile(r"^\W*(always\s+)?(raise|increase|scale|set|double|triple|lift|push|spend|"
                   r"move|change|put)\b", IC)
MULTIPLE = re.compile(r"\b(double|triple|quadruple|[2-9]x)\b", IC)
PERIOD = re.compile(r"\b(week|weekly|month|monthly|total|in all)\b", IC)


def _event(ws, event_id: int):
    row = ws._conn.execute(
        "SELECT type, actor, payload FROM events WHERE id = ?", (event_id,)).fetchone()
    return (row[0], row[1], json.loads(row[2])) if row else None


def source_problem(ws, source: str, role: str | None = None) -> str | None:
    """None if the source exists; otherwise why not."""
    m = re.fullmatch(r"(wk|j|o|n|e|m)-(\d+)", source)
    if m:
        kind, number = m.group(1), int(m.group(2))
        if kind == "wk":
            return None if mem.week_record(ws, number) else f"no week record {source}"
        if kind == "m":
            return None  # an owner line's own ID
        found = _event(ws, number)
        if found is None:
            return f"no event {source}"
        etype, actor, payload = found
        if kind == "j" and etype != "journal.entry":
            return f"{source} is not a journal entry"
        if kind == "j" and role is not None and payload.get("role") != role:
            return f"{source} is another role's journal entry"
        if kind == "o" and etype != "journal.outcome":
            return f"{source} is not an outcome"
        if kind == "n" and not (actor == "user" and etype in {"message.posted",
                                                              "approval.returned"}):
            return f"{source} is not an owner note"
        return None
    if m := re.fullmatch(r"fact-v(\d+)", source):
        return None if 0 < int(m.group(1)) <= ws.facts.version else f"no fact version {source}"
    if m := re.fullmatch(r"ins-w(\d+)-(\d+)", source):
        record = ws.artifacts.latest(f"week{m.group(1)}/research")
        count = len(record.content.get("insights", [])) if record else 0
        return None if int(m.group(2)) <= count else f"no insight {source}"
    return None if ws.ledger.has(source) else f"unknown source {source}"


def injection(text: str) -> bool:
    return bool(INJECTION.search(text))


def loosening(text: str, facts: SharedFacts) -> str | None:
    """Why this line would permit something the charter or house rules forbid, or None."""
    rules = facts.house_rules
    permits = bool(PERMIT.search(text) or ORDER.search(text))
    if permits and PAST_A_RULE.search(text):
        return "it permits going past a rule or the owner's approval"
    if permits and BUDGET_WORDS.search(text):
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(%|percent)", text, IC):
            if float(m.group(1)) / 100 > rules.max_budget_change_pct + 1e-9:
                return (f"it allows {m.group(1)}% budget moves; the house rule is "
                        f"{rules.max_budget_change_pct:.0%}")
        if MULTIPLE.search(text) and rules.max_budget_change_pct < 1:
            return (f"it allows multiplying a budget; the house rule is "
                    f"{rules.max_budget_change_pct:.0%} a move")
        for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)", text):
            # Campaign budgets are daily here, so an amount counts as daily unless it says not.
            if float(m.group(1).replace(",", "")) > rules.daily_budget_cap and \
                    not PERIOD.search(text):
                return (f"it allows ${m.group(1)} a day; the cap is ${rules.daily_budget_cap:g}")
    if permits and rules.protect_learning_phase and re.search(r"learning", text, IC) and \
            re.search(r"\b(pause|cut|change|touch|edit|raise|lower)\w*", text, IC) and \
            not re.search(r"\b(not|never|avoid|don'?t|wait)\b", text, IC):
        return "it permits changing campaigns still in the learning phase"
    days = rules.min_days_between_budget_changes
    if permits and re.search(r"\b(every day|daily|each day|back-to-back|two days in a row)\b",
                             text, IC) and re.search(r"budget", text, IC) and days > 1:
        return f"it allows budget changes more often than every {days} days"
    for constraint in facts.constraints:
        words = [w for w in re.findall(r"[a-z]{5,}", constraint.lower())][:3]
        if permits and words and all(w in text.lower() for w in words) and \
                re.search(r"\b(ok|fine|allowed|can)\b", text, IC):
            return f"it relaxes the constraint: {constraint}"
    return None


def line_problems(ws, store: str, role: str | None, line: mem.Line,
                  facts: SharedFacts) -> list[str]:
    """Code checks every memory line before it is written."""
    problems = []
    if not line.text.strip():
        problems.append("empty line")
    if len(line.text) > 300:
        problems.append("longer than 300 characters")
    if not line.sources:
        problems.append("no source")
    owner_role = None if store == mem.ACCOUNT else role
    for source in line.sources:
        if (why := source_problem(ws, source, owner_role)) is not None:
            problems.append(why)
    if injection(line.text):
        problems.append("reads like an instruction to the model, not a learning")
    if (why := loosening(line.text, facts)) is not None:
        problems.append(f"loosens a rule: {why}")
    if line.author != "owner" and NEVER.search(line.text):
        problems.append('only the owner can say "never"')
    return problems


def topic_kind(topic: str | None) -> str:
    """"day4/change_set" -> "change_set"; "week2/creative/w2-t1" -> "creative"."""
    if not topic:
        return ""
    parts = topic.split("/")
    return parts[1] if len(parts) > 1 else parts[0]


def support(ws, role: str | None, sources: list[str]) -> tuple[int, bool]:
    """Independent outcomes behind a line (distinct day and topic), and whether an owner note
    backs it."""
    owner = any(s.startswith("n-") for s in sources)
    keys = set()
    outcomes: dict[str, list[dict]] = {}
    if role is not None:
        outcomes = {e["id"]: e["outcomes"] for e in mem.journal(ws, role, reader="system")}
    for source in sources:
        if source.startswith("j-") and outcomes.get(source):
            entry = _event(ws, int(source[2:]))
            if entry is not None:
                keys.add((entry[2].get("day"), entry[2].get("topic")))
        elif source.startswith("o-"):
            found = _event(ws, int(source[2:]))
            if found is not None and found[0] == "journal.outcome" and \
                    not found[2].get("simulated"):
                keys.add((found[2].get("day"), found[2].get("topic")))
    return len(keys), owner


# --- Quality, measured by code ------------------------------------------------------------------


def quality(entries: list[dict]) -> dict:
    """Quality of a role's work over some journal entries, from outcomes only."""
    work = [e for e in entries if e.get("kind") not in mem.MEMORY_KINDS]
    seen_topics: set[tuple] = set()
    first = []
    for e in work:
        key = (e.get("loop_id"), e.get("topic"))
        if key not in seen_topics:
            seen_topics.add(key)
            first.append(e)
    outcomes = [o for e in work for o in e["outcomes"] if not o.get("simulated")]

    def judged(names: set[str]) -> list[dict]:
        return [o for o in outcomes if o["outcome"] in names]

    parts = {}
    reviewed = [e for e in first if e["outcomes"]]
    if reviewed:
        parts["first_try"] = sum(not any(o["outcome"] == "redo" for o in e["outcomes"])
                                 for e in reviewed) / len(reviewed)
    owner = judged({"approved", "sent_back"})
    if owner:
        parts["approval"] = sum(o["outcome"] == "approved" for o in owner) / len(owner)
    change_sets = [e for e in work if topic_kind(e.get("topic")) == "change_set"
                   and e["outcomes"]]
    if change_sets:
        parts["gate"] = 1 - sum(any(o["outcome"] == "gate_blocked" for o in e["outcomes"])
                                for e in change_sets) / len(change_sets)
    tests = judged({"test_result"})
    if tests:
        parts["tests"] = sum(bool(o["good"]) for o in tests) / len(tests)
    score = sum(parts.values()) / len(parts) if parts else None
    return {"score": None if score is None else round(score, 4), "parts": parts,
            "calls": len(work)}


def evaluate_versions(ws, role: str, week: int, loop_id: str | None = None,
                      sim_day: int | None = None) -> dict | None:
    """Judge the live lessons version once it has a window of calls behind it; roll it back
    if quality fell past the threshold. Each version is judged once."""
    found = [e for e in mem.versions(ws, mem.LESSONS, role)
             if e.payload.get("reason", "").startswith("lesson pass")]
    if not found:
        return None
    live = found[-1]
    version = live.payload["version"]
    if any(e.payload.get("role") == role and e.payload.get("version") == version
           for e in ws.events.list("memory.evaluated")):
        return None
    earlier = [v for v in mem.versions(ws, mem.LESSONS, role) if v.id < live.id]

    def active(event) -> set[str]:
        return {_norm(raw["text"]) for raw in (event.payload["lines"] if event else [])
                if raw.get("status") == "active" and not raw.get("locked")}

    if not active(live) - active(earlier[-1] if earlier else None):
        # Only watch lines or removals: nothing the role acts on changed, so nothing to judge.
        return None
    entries = [e for e in mem.journal(ws, role, reader="system")
               if e.get("kind") not in mem.MEMORY_KINDS]
    after = [e for e in entries if e["event_id"] > live.id]
    before = [e for e in entries if e["event_id"] < live.id][-max(len(after), MIN_WINDOW * 2):]
    weeks_live = week - (live.payload.get("week") or week)
    if len(after) < MIN_WINDOW and weeks_live < 1:
        return None
    q_before, q_after = quality(before), quality(after)
    result = {"role": role, "version": version, "before": q_before, "after": q_after,
              "kept": True}
    # Rounded so a drop of exactly the threshold (0.65 -> 0.50) is kept, not lost to float error.
    if q_before["score"] is not None and q_after["score"] is not None and \
            round(q_before["score"] - q_after["score"], 4) > QUALITY_DROP:
        result["kept"] = False
        previous = [v for v in mem.versions(ws, mem.LESSONS, role)
                    if v.payload["version"] < version]
        lines = [mem.Line(**raw) for raw in previous[-1].payload["lines"]] if previous else []
        current = mem.view(ws, mem.LESSONS, role)
        lines = [line for line in lines if not line.locked]
        mem.write_version(
            ws, mem.LESSONS, role, lines, "system", week,
            f"rolled back: quality fell from {q_before['score']:.2f} to {q_after['score']:.2f}",
            loop_id=loop_id, sim_day=sim_day, base=current)
        ws.events.append(
            "memory.reverted", "system",
            {"role": role, "from_version": version,
             "to_version": previous[-1].payload["version"] if previous else 0,
             "before": q_before, "after": q_after, "threshold": QUALITY_DROP,
             "reason": f"Quality fell from {q_before['score']:.2f} to {q_after['score']:.2f} "
                       f"after lessons version {version} went live."},
            loop_id=loop_id, sim_day=sim_day)
    ws.events.append("memory.evaluated", "system", result, loop_id=loop_id, sim_day=sim_day)
    return result


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def rolled_back(ws, role: str, week: int) -> dict[str, int]:
    """Lessons taken out by a rollback in the last EXPIRE_WEEKS weeks: normalized text -> the
    week of the rollback. They may not come straight back."""
    found = {}
    by_version = {e.payload["version"]: e for e in mem.versions(ws, mem.LESSONS, role)}
    for event in ws.events.list("memory.reverted"):
        if event.payload.get("role") != role:
            continue
        when = mem.week_of(event.sim_day)
        if week - when >= EXPIRE_WEEKS:
            continue
        version = by_version.get(event.payload["from_version"])
        for raw in (version.payload["lines"] if version else []):
            if not raw.get("locked"):
                found[_norm(raw["text"])] = when
    return found


# --- Expiry, confirmation and contradiction ------------------------------------------------------


def _sign(ws, role: str, line: mem.Line, outcomes: dict[str, list[dict]]) -> bool | None:
    votes = [o["good"] for s in line.sources for o in outcomes.get(s, [])
             if o.get("good") is not None and not o.get("simulated")]
    return None if not votes else sum(votes) * 2 > len(votes)


def refresh_lessons(ws, role: str, week: int, loop_id: str | None = None,
                    sim_day: int | None = None) -> dict:
    """Confirm, flag and expire lessons from this week's outcomes. Writes a new version only if
    something changed. Owner lines never expire."""
    entries = mem.journal(ws, role, reader="system")
    by_id = {e["id"]: e for e in entries}
    outcomes = {e["id"]: e["outcomes"] for e in entries}
    current = mem.view(ws, mem.LESSONS, role)
    this_week = [(e, o) for e in entries for o in e["outcomes"]
                 if o.get("week") == week and not o.get("simulated")
                 and e.get("kind") not in mem.MEMORY_KINDS]
    changed, confirmed, flagged, expired = False, [], [], []
    lines = []
    for line in current.lines:
        if line.locked or line.status not in {"active", "watch"}:
            lines.append(line)
            continue
        kinds = {topic_kind(by_id[s].get("topic")) for s in line.sources if s in by_id}
        subjects = {x for s in line.sources if s in by_id for x in by_id[s].get("subjects", [])}
        sign = _sign(ws, role, line, outcomes)
        update = {}
        for entry, outcome in this_week:
            if entry["id"] in line.sources or topic_kind(entry.get("topic")) not in kinds:
                continue
            if subjects and not subjects & set(entry.get("subjects", [])):
                continue
            if sign is None or outcome["good"] is None:
                continue
            if outcome["good"] == sign:
                update["last_confirmed_week"] = week
            else:
                update["flag"] = (f"{outcome['id']} went the other way ({outcome['outcome']} "
                                  f"on {entry.get('topic')})")
        last = update.get("last_confirmed_week", line.last_confirmed_week or line.created_week
                          or week)
        if line.status in {"active", "watch"} and week - last >= EXPIRE_WEEKS:
            update["status"] = "inactive"
            expired.append(line.id)
        if "last_confirmed_week" in update:
            confirmed.append(line.id)
        if "flag" in update:
            flagged.append(line.id)
        if update:
            changed = True
            line = line.model_copy(update=update)
        lines.append(line)
    if changed:
        mem.write_version(ws, mem.LESSONS, role, lines, "system", week,
                          "refreshed from outcomes", loop_id=loop_id, sim_day=sim_day,
                          base=current)
    return {"confirmed": confirmed, "flagged": flagged, "expired": expired}


# --- Collapse watch ----------------------------------------------------------------------------


def collapse_flags(ws, week: int) -> list[dict]:
    """Roles that shrank to doing nothing, or to one option, while the account had signals."""
    flags = []
    days = range(7 * week - 6, 7 * week + 1)
    quiet = 0
    acting_days = 0
    for day in days:
        findings = ws.artifacts.latest(f"day{day}/findings")
        change_set = ws.artifacts.latest(f"day{day}/change_set")
        if findings is None or change_set is None:
            continue
        if any(f.get("severity") == "act" for f in findings.content.get("findings", [])):
            acting_days += 1
            if not change_set.content.get("changes"):
                quiet += 1
    if acting_days >= 2 and quiet >= max(2, acting_days - 1):
        flags.append({"role": "media_buyer", "week": week,
                      "reason": f"No changes on {quiet} of {acting_days} days that had act "
                                "signals."})
    plan = ws.artifacts.latest(f"week{week}/test_plan")
    if plan is not None and len(plan.content.get("tests", [])) <= 1:
        flags.append({"role": "growth_lead", "week": week,
                      "reason": "The test plan had one test or none."})
    layouts = Counter()
    for test in (plan.content.get("tests", []) if plan is not None else []):
        record = ws.artifacts.latest(f"week{week}/creative/{test.get('test_id')}")
        for scene in (record.content.get("ad_copy", {}).get("scenes", []) if record else []):
            layouts[scene.get("layout")] += 1
    if sum(layouts.values()) >= 6 and len(layouts) <= 1:
        flags.append({"role": "producer", "week": week,
                      "reason": f"Every scene used one layout ({next(iter(layouts))})."})
    return flags
