"""Read models for the Dashboard and Creative Assets pages.

Both read the sandbox the way the Analyst does (Insights-shaped rows, one per ad per day) and the
workspace (what the team did, and how each image was made). Nothing here writes.
"""

from __future__ import annotations

import re
from pathlib import Path

from pgt.api.views import OWNER, Index, _media_url, period, provider, role, title
from pgt.harness.workspace import Workspace
from pgt.llm.meta import IMAGE_MODEL, SAM_MODEL
from pgt.loops.weekly import shot_file
from pgt.sandbox import product_names

SANDBOX_ACCOUNT = "act_sandbox_0001"
RANGES = (7, 14, 28)
MEDIA_NAMES = {SAM_MODEL: "SAM 3.1", IMAGE_MODEL: "Muse Image"}


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _sum(rows) -> dict:
    values = [r.purchase_value for r in rows if r.purchase_value is not None]
    return {
        "spend": round(sum(r.spend for r in rows), 2),
        "impressions": sum(r.impressions for r in rows),
        "clicks": sum(r.clicks for r in rows),
        "results": sum(r.results for r in rows),
        # Only accounts that track purchase value have revenue; None keeps ROAS off the board.
        "revenue": round(sum(values), 2) if values else None,
    }


def _ratios(t: dict) -> dict:
    imp, clicks, results, spend = t["impressions"], t["clicks"], t["results"], t["spend"]
    return {
        "ctr": clicks / imp if imp else None,
        "cpc": spend / clicks if clicks else None,
        "cpm": spend / imp * 1000 if imp else None,
        "cpr": spend / results if results else None,
        "roas": t["revenue"] / spend if t["revenue"] is not None and spend else None,
    }


def _metrics(rows, target: float) -> dict:
    t = _sum(rows)
    r = _ratios(t)
    tone = ""
    if r["cpr"] is not None:
        tone = "under" if r["cpr"] <= target else "over"
    return {**t, "ctr": r["ctr"], "cpr": r["cpr"], "roas": r["roas"], "tone": tone}


def _change(a: float | None, b: float | None) -> float | None:
    return (a - b) / b if a is not None and b else None


def _kpis(rows, prior, ndays: int, facts) -> list[dict]:
    t, r = _sum(rows), _ratios(_sum(rows))
    p = _sum(prior) if prior else None
    pr = _ratios(p) if p else None
    target = facts.target_cost_per_result
    cap = facts.house_rules.daily_budget_cap
    name = facts.result_name
    vs = f"vs prior {ndays} days"

    def delta(now, before, good_when_up: bool, kind: str = "pct") -> tuple[str, str]:
        if before is None or now is None:
            return "", ""
        if kind == "pt":
            d = (now - before) * 100
            text = f"{d:+.2f} pt {vs}"
        elif kind == "count":
            d = now - before
            text = f"{d:+,} {vs}"
        else:
            d = _change(now, before)
            if d is None:
                return "", ""
            text = f"{d:+.0%} {vs}"
        if abs(d) < 1e-9:
            return f"flat {vs}", ""
        return text, "good" if (d > 0) == good_when_up else "bad"

    per_day = t["spend"] / ndays if ndays else 0
    imp_sub, imp_tone = delta(t["impressions"], p and p["impressions"], True)
    ctr_sub, ctr_tone = delta(r["ctr"], pr and pr["ctr"], True, "pt")
    cpm_sub, cpm_tone = delta(r["cpm"], pr and pr["cpm"], False)
    res_sub, res_tone = delta(t["results"], p and p["results"], True, "count")
    rev_sub, rev_tone = delta(t["revenue"], p and p["revenue"], True)
    roas_sub, roas_tone = delta(r["roas"], pr and pr["roas"], True)
    cpr = r["cpr"]
    if cpr is None:
        hero_sub, hero_tone = f"no {name}s yet", ""
    elif cpr <= target:
        hero_sub, hero_tone = f"{_money(target - cpr)} under target", "good"
    else:
        hero_sub, hero_tone = f"{_money(cpr - target)} over target", "bad"
    tiles = [
        {"key": "spend", "label": "Spend", "value": t["spend"], "fmt": "money",
         "sub": f"{_money(per_day)}/day, cap {_money(cap)}", "tone": "", "formula": "sum of spend"},
        {"key": "impressions", "label": "Impressions", "value": t["impressions"], "fmt": "int",
         "sub": imp_sub or f"over {ndays} days", "tone": imp_tone,
         "formula": "sum of impressions"},
        {"key": "ctr", "label": "CTR", "value": r["ctr"], "fmt": "pct",
         "sub": ctr_sub or f"{t['clicks']:,} clicks", "tone": ctr_tone,
         "formula": "clicks / impressions"},
        {"key": "cpc", "label": "CPC", "value": r["cpc"], "fmt": "money",
         "sub": f"{t['clicks']:,} clicks", "tone": "", "formula": "spend / clicks"},
        {"key": "cpm", "label": "CPM", "value": r["cpm"], "fmt": "money",
         "sub": cpm_sub or "per 1,000 impressions", "tone": cpm_tone,
         "formula": "spend / impressions x 1,000"},
        {"key": "results", "label": f"{name.capitalize()}s", "value": t["results"], "fmt": "int",
         "sub": res_sub or f"{name}s started", "tone": res_tone, "formula": f"sum of {name}s"},
        {"key": "cpr", "label": f"Cost per {name}", "value": cpr, "fmt": "money",
         "sub": hero_sub, "tone": hero_tone, "hero": True, "formula": f"spend / {name}s"},
    ]
    if t["revenue"] is None:
        return tiles
    # A store that tracks purchase value: revenue and ROAS take the places of impressions and CPC.
    money = [
        {"key": "revenue", "label": "Revenue", "value": t["revenue"], "fmt": "money",
         "sub": rev_sub or "purchase value", "tone": rev_tone, "formula": "sum of purchase value"},
        {"key": "roas", "label": "ROAS", "value": r["roas"], "fmt": "ratio",
         "sub": roas_sub or "revenue per $1 spent", "tone": roas_tone,
         "formula": "purchase value / spend"},
    ]
    keep = [k for k in tiles if k["key"] not in {"impressions", "cpc"}]
    return keep[:1] + money + keep[1:]


def _ad_names(account) -> dict[str, str]:
    return {a.ad_id: a.name for a in account.state().ads}


def _describe(change: dict, campaigns: dict[str, str], ads: dict[str, str]) -> str:
    target = change["target_id"]
    if change["kind"] == "set_budget":
        return f"set {campaigns.get(target, target)} to {_money(change['new_daily_budget'])}/day"
    if change["kind"] == "pause_ad":
        return f"paused {ads.get(target, target)}"
    return f"turned on {ads.get(target, target)}"


def _actions(ws: Workspace, account, idx: Index, start: int, end: int) -> list[dict]:
    """What the team did in the range: the Analyst's flags and every change the Operator made."""
    campaigns = {c.campaign_id: c.name for c in account.state().campaigns}
    ads = _ad_names(account)
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for rec in ws.artifacts.list(kind="DailyBrief"):
        day = rec.content.get("day", rec.sim_day)
        if not (start <= day <= end):
            continue
        for f in rec.content.get("findings", []):
            if f.get("severity") != "act" or (day, f["subject_id"]) in seen:
                continue
            seen.add((day, f["subject_id"]))
            out.append({"day": day, "who": role("analyst"), "verb": "flagged",
                        "text": f["claim"], "evidence": f.get("evidence_ids", []), "link": None,
                        "order": 0})
    for e in idx.events:
        if e.type != "changeset.applied" or not e.refs or not (start <= (e.sim_day or 0) <= end):
            continue
        rec = ws.artifacts.get(e.refs[0])
        if rec is None:
            continue
        p = e.payload
        changes = rec.content.get("changes", [])
        parts = [_describe(changes[i], campaigns, ads) for i in p.get("applied", [])
                 if i < len(changes)]
        created = p.get("created_ad_ids") or []
        if created:
            n = len(created)
            parts.append(f"created {n} paused {'ad' if n == 1 else 'ads'} "
                         f"({', '.join(ads.get(a, a) for a in created)})")
        if not parts:
            continue
        grants = [g for g in idx.grants.get(rec.key, []) if g.id < e.id]
        by = "the simulated owner" if grants and grants[-1].id in idx.simulated else OWNER
        evidence = sorted({i for ch in changes for i in ch.get("evidence_ids", [])})
        text = "; ".join(parts)
        out.append({"day": e.sim_day, "who": role("operator"), "verb": "",
                    "text": text[0].upper() + text[1:] + f", after {by} approved",
                    "evidence": evidence, "link": {"key": rec.key, "title": title(rec)},
                    "order": 1})
    out.sort(key=lambda a: (a["day"], a["order"]))
    for n, a in enumerate(out, 1):
        a["n"] = n
        del a["order"]
    return out


def _before_after(series: list[dict], actions: list[dict], result_name: str) -> str | None:
    """Cost per result on the two days up to the first applied change and the two days after.
    Stated side by side, without claiming the change caused the difference."""
    first = next((a for a in actions if a["link"]), None)
    if first is None:
        return None
    d = first["day"]
    by_day = {s["day"]: s for s in series}

    def window(days):
        rows = [by_day[x] for x in days if x in by_day]
        spend, results = sum(r["spend"] for r in rows), sum(r["results"] for r in rows)
        return (spend / results, rows) if results and len(rows) == len(days) else (None, rows)

    before, _ = window([d - 1, d])
    after, _ = window([d + 1, d + 2])
    if before is None or after is None:
        return None
    return (f"Cost per {result_name} was {_money(before)} on days {d - 1} to {d} and {_money(after)} "
            f"on days {d + 1} to {d + 2}.")


def _table(account, rows, target: float) -> list[dict]:
    state = account.state()
    by_ad: dict[str, list] = {}
    for r in rows:
        by_ad.setdefault(r.ad_id, []).append(r)
    paused_on: dict[str, int] = {}
    created: set[str] = set()
    for ch in account.changes():
        if ch["kind"] == "ad_status" and ch["status"] == "PAUSED":
            paused_on[ch["ad_id"]] = ch["day"]
        elif ch["kind"] == "ad_status":
            paused_on.pop(ch["ad_id"], None)
        elif ch["kind"] == "ad_created":
            created.add(ch["ad_id"])
    out = []
    for c in state.campaigns:
        ads = [a for a in state.ads if a.campaign_id == c.campaign_id]
        crow = [r for a in ads for r in by_ad.get(a.ad_id, [])]
        ad_rows = []
        for a in ads:
            mine = by_ad.get(a.ad_id, [])
            waits = a.status == "PAUSED" and a.ad_id in created and not mine
            if a.status == "ACTIVE":
                status = ("live", "Active")
            elif waits:
                status = ("wait", "Created paused")
            elif a.ad_id in paused_on:
                status = ("paused", f"Paused day {paused_on[a.ad_id]}")
            else:
                status = ("paused", "Paused")
            ad_rows.append({"id": a.ad_id, "name": a.name, "status": status[0],
                            "status_label": status[1], "waits": waits,
                            "evidence": mine[-1].evidence_id if mine else None,
                            **_metrics(mine, target)})
        out.append({"id": c.campaign_id, "name": c.name,
                    "status": "live" if c.status == "ACTIVE" else "paused",
                    "status_label": "Active" if c.status == "ACTIVE" else "Paused",
                    "in_learning": c.in_learning, "budget": c.daily_budget,
                    "test_id": c.test_id, "rows": len(crow), **_metrics(crow, target),
                    "ads": ad_rows})
    return out


def _which(ws: Workspace, account=None) -> dict:
    """The connected account as the dashboard names it: the sandbox unless a real one is in use."""
    from pgt.harness import access

    conn = access.connection(ws) or {}
    if conn.get("kind") == "meta":
        return {"account": conn.get("account_id"), "account_name": conn.get("name"), "real": True}
    scenario = getattr(account, "scenario", None)
    name = scenario.name if scenario is not None else access.SANDBOX["name"]
    return {"account": SANDBOX_ACCOUNT, "account_name": name, "real": False}


def dashboard(ws: Workspace, account, days: int = 7) -> dict:
    facts = ws.facts.current()
    if account is None or account.day == 0:
        # `waiting`: the account is connected but no day has run yet (a cold start).
        return {"ready": False, "waiting": account is not None, **_which(ws, account)}
    days = days if days in RANGES else 7
    end = account.day
    start = max(1, end - days + 1)
    rows = account.insights(start, end)
    prior_start = start - days
    prior = account.insights(prior_start, start - 1) if prior_start >= 1 else []
    ndays = end - start + 1
    target = facts.target_cost_per_result
    series = []
    for d in range(start, end + 1):
        t = _sum([r for r in rows if r.day == d])
        series.append({"day": d, "spend": t["spend"], "results": t["results"],
                       "cpr": round(t["spend"] / t["results"], 2) if t["results"] else None})
    idx = Index.build(ws)
    actions = _actions(ws, account, idx, start, end)
    pulls = ws.events.list("loop.started")
    used = [r for r in ws.artifacts.list() if r.key.endswith("/findings")]
    state = account.state()
    return {
        "ready": True,
        **_which(ws, account),
        "currency": state.currency,
        "days": days,
        "start": start,
        "end": end,
        "short": days > end,  # fewer days have run than the range asks for
        "pulled": {"day": pulls[-1].sim_day, "ts": pulls[-1].ts} if pulls else None,
        "target": target,
        "result_name": facts.result_name,
        "kpis": _kpis(rows, prior, ndays, facts),
        "evidence": {
            "rows": len(rows),
            "first": rows[0].evidence_id if rows else None,
            "last": rows[-1].evidence_id if rows else None,
            "fields": "spend, impressions, clicks, results"
                      + (", purchase value" if any(r.purchase_value is not None for r in rows)
                         else ""),
            "level": "ad",
            "used_in": {"key": used[-1].key, "title": title(used[-1])} if used else None,
        },
        "series": series,
        "actions": actions,
        "after": _before_after(series, actions, facts.result_name),
        "campaigns": _table(account, rows, target),
        "daily_total": sum(c.daily_budget for c in state.campaigns if c.status == "ACTIVE"),
        "daily_cap": facts.house_rules.daily_budget_cap,
    }


# ---------------------------------------------------------------- creative assets


def _calls(ws: Workspace, loop_id: str, model: str | None = None, role_: str = "producer"):
    return [e for e in ws.events.list("model.call", loop_id=loop_id)
            if e.payload.get("ok", True) and e.payload["role"] == role_
            and (model is None or e.payload["model"] == model)]


def _step_model(calls) -> dict | None:
    if not calls:
        return None
    model = calls[0].payload["model"]
    return {"model": MEDIA_NAMES.get(model, model.replace("meta-models/", "")),
            "provider": provider(model), "calls": len(calls),
            "cost": round(sum(e.payload["cost"] for e in calls), 4)}


def _creative_status(ws: Workspace, idx: Index, account, record, cs) -> tuple[str, str, str | None]:
    """(status, label, ad_id) for one creative, from the change set that carries it."""
    if record.status in ("stale", "superseded"):
        return "stale", "Stale", None
    c = record.content
    drafts = cs.content.get("ad_drafts", []) if cs else []
    at = next((i for i, d in enumerate(drafts) if d["creative_id"] == c.get("creative_id")), None)
    if cs is None or at is None:
        review = ws.artifacts.latest(f"{record.key.split('/')[0]}/review/{c.get('test_id')}")
        if review and review.content.get("verdict") == "redo":
            return "draft", "Sent back", None
        return "review", "In review", None
    status, _ = idx.status(cs)
    if status == "applied":
        applied = idx.applied[cs.artifact_id].payload.get("created_ad_ids") or []
        ad_id = applied[at] if at < len(applied) else None
        ad = account.state().ad(ad_id) if account and ad_id else None
        if ad and ad.status == "ACTIVE":
            return "live", "Live", ad_id
        return "applied", "Created paused", ad_id
    if status == "wait":
        return "wait", "In change set", None
    if status == "ok":
        return "ok", "Approved", None
    return "review", "In change set", None


def _after(account, ad_id: str | None, target: float, result_name: str) -> tuple[str, bool]:
    if ad_id is None:
        return "Not launched", False
    rows = account.insights(ad_ids=[ad_id]) if account else []
    if not rows:
        ad = account.state().ad(ad_id) if account else None
        state = "on" if ad and ad.status == "ACTIVE" else "paused"
        return f"{ad_id}, {state}, no delivery yet", False
    m = _metrics(rows, target)
    cpr = f"{_money(m['cpr'])}/{result_name}" if m["cpr"] is not None else f"no {result_name}s"
    return f"CTR {m['ctr']:.2%} · {cpr}", m["tone"] == "over"


def _scene_prompts(copy: dict) -> list[str]:
    """Scene prompts of a saved ad; ads saved before scenes have one image_prompt."""
    if copy.get("scenes"):
        return [s.get("prompt", "") for s in copy["scenes"]]
    return [copy["image_prompt"]] if copy.get("image_prompt") else []


def _ad_name(name: str | None, test_id: str) -> str:
    """The model sometimes starts the ad name with its test ID; the card shows that ID already."""
    if not name:
        return test_id
    week, _, short = test_id.rpartition("-")  # "w1-t2" is also written "T2" or "W1"
    ids = "|".join(re.escape(x) for x in (test_id, short, week) if x)
    lead = rf"^\s*(?:{ids})\b\s*[-:·]?\s*"
    rest = re.sub(lead, "", name, flags=re.IGNORECASE)
    return rest or name


def _made(ws, idx, account, media_dir, record, plans, facts) -> dict:
    c = record.content
    week = record.key.split("/")[0]
    loop = f"weekly-w{week[4:]}"
    test_id = c.get("test_id")
    copy = c.get("ad_copy", {})
    cs = ws.artifacts.latest(f"{week}/change_set")
    status, label, ad_id = _creative_status(ws, idx, account, record, cs)
    after, bad = _after(account, ad_id, facts.target_cost_per_result, facts.result_name)
    versions = [r for r in ws.artifacts.list(kind="CreativeSet") if r.key == record.key]
    prompts = {p for v in versions for p in _scene_prompts(v.content.get("ad_copy", {}))}
    sam = _step_model(_calls(ws, loop, SAM_MODEL))
    # Scene shots log the full shot note, which carries the scene prompt inside it.
    image = _step_model([e for e in _calls(ws, loop, IMAGE_MODEL)
                         if any(p and p in (e.payload.get("prompt") or "") for p in prompts)])
    text_calls = [e for e in _calls(ws, loop)
                  if e.payload["model"] not in (SAM_MODEL, IMAGE_MODEL)]
    text_models = sorted({e.payload["model"] for e in text_calls})
    test = next((t for t in plans.get(week, []) if t["test_id"] == test_id), None)
    scenario = getattr(account, "scenario", None)
    names = product_names(scenario)
    what = (test or {}).get("brief", {}).get("product")
    what = what if what in names else names[0]
    shot = Path(media_dir) / week / shot_file(what, names[0])
    creatives_in_week = len({r.key for r in ws.artifacts.list(kind="CreativeSet")
                             if r.key.startswith(f"{week}/creative/")})
    photo = ("Studio photo made once with Muse Image for this synthetic store"
             if scenario and scenario.product_photo else "Synthetic stand-in for an uploaded photo")
    steps = [
        {"title": "Product photo", "sub": photo, "model": None, "final": False},
        {"title": f"Cut out the {what}", "model": sam, "final": False,
         "sub": f"Once for {period(record.key)}, shared by {creatives_in_week} creatives"
         if creatives_in_week > 1 else "Once for the week"},
        {"title": f"Copy written, {len(versions)} {'draft' if len(versions) == 1 else 'drafts'}",
         "sub": f'"{copy.get("headline", "")}"',
         "model": {"model": " + ".join(m.replace("meta-models/", "") for m in text_models),
                   "provider": provider(text_models[0]), "calls": None, "cost": None}
         if text_models else None, "final": False},
        {"title": "Three scenes shot with the product, laid out in three sizes", "model": image,
         "final": False,
         "sub": " · ".join(s.get("angle", "") for s in copy.get("scenes", []))
         or copy.get("image_prompt", "")},
    ]
    if c.get("video"):
        story = copy.get("video", {})
        steps.append({"title": "10-second video, assembled in code", "model": None,
                      "final": False,
                      "sub": " / ".join(story.get(k, "") for k in ("hook", "benefit", "proof"))
                      + f" / {story.get('cta', '')} end card"})
    returned = [e for e in ws.events.list("approval.returned")
                if e.payload["key"] == f"{week}/change_set"]
    redos = ws.events.list("review.redo", loop_id=loop)
    for e in sorted([*redos, *returned], key=lambda e: e.id):
        p = e.payload
        if e.type == "approval.returned":
            steps.append({"title": "Owner sent the new ads back", "sub": p.get("note", ""),
                          "model": None, "final": False, "by": OWNER})
        elif str(p.get("subject", "")).startswith(f"{test_id}-c"):
            steps.append({"title": f"Sent back draft {p.get('attempt', '')}".strip(),
                          "sub": "; ".join(p.get("issues", [])), "model": None, "final": False,
                          "by": role(p.get("by", "reviewer") if p.get("by") != "code"
                                     else "reviewer")["name"]
                          + (" (code check)" if p.get("by") == "code" else "")})
    review = ws.artifacts.latest(f"{week}/review/{test_id}")
    if review and review.content.get("verdict") == "pass":
        steps.append({"title": f"Reviewer passed draft {record.version}",
                      "sub": "Every claim traces to evidence", "model": None, "final": True})
    images = [{**img, "url": _media_url(img["path"], media_dir)} for img in c.get("images", [])]
    square = next((i["url"] for i in images if i["ratio"] == "1:1"), None)
    video = c.get("video")
    video_url = _media_url(video["path"], media_dir) if video else None
    pub = idx.published.get(record.artifact_id)
    return {
        "id": record.key, "kind": "made", "title": _ad_name(copy.get("name"), test_id),
        "hook": copy.get("headline", ""), "test_id": test_id, "week": period(record.key),
        "status": status, "status_label": label, "version": record.version,
        "updated": pub.ts if pub else None,
        "models": [s["model"]["model"] for s in steps if s["model"]],
        "after": after, "bad": bad, "image": square, "images": images,
        "video": video_url, "scenes": [s.get("angle", "") for s in copy.get("scenes", [])],
        "source": _media_url(str(shot), media_dir) if shot.is_file() else None,
        "steps": steps,
        "facts": [
            ["Test", f"{test_id} · {period(record.key)}"],
            ["Hook", (test or {}).get("brief", {}).get("hook") or copy.get("headline", "")],
            ["Ad", ad_id or "Not created yet"],
            ["After launch", after],
        ],
        "used_in": {"key": cs.key, "title": title(cs)} if cs else None,
        "deliverable": record.key,
    }


def assets(ws: Workspace, account, media_dir: str) -> dict:
    """Every image the team made, each week's product photo, and anything the owner uploaded."""
    idx = Index.build(ws)
    facts = ws.facts.current()
    plans = {r.key.split("/")[0]: r.content.get("tests", [])
             for r in ws.artifacts.list(kind="TestPlan")}
    latest: dict[str, object] = {}
    for r in ws.artifacts.list(kind="CreativeSet"):
        latest[r.key] = r
    items = [_made(ws, idx, account, media_dir, r, plans, facts) for r in latest.values()]
    items.sort(key=lambda a: (a["week"], a["updated"] or ""), reverse=True)
    root = Path(media_dir)
    scenario = getattr(account, "scenario", None)
    names = product_names(scenario)

    def hook(what: str) -> str:
        if scenario is None or not scenario.product_photo:
            return "Synthetic phone mock-up drawn in code. SAM 3.1 cuts the phone out of it."
        return (f"Studio photo of the {what}, made once with Muse Image for this synthetic "
                f"store. SAM 3.1 cuts the {what} out of it, so every ad shows the real product.")

    def shows(test: dict) -> str:
        what = test.get("brief", {}).get("product")
        return what if what in names else names[0]

    made = {(a["id"].split("/")[0], a["test_id"]) for a in items}
    weeks = sorted({p.name for p in root.glob("week*") if p.is_dir()}, reverse=True) \
        if root.is_dir() else []
    for week in weeks:
        for what in names:
            shot = root / week / shot_file(what, names[0])
            if not shot.is_file():
                continue
            used = [t["test_id"] for t in plans.get(week, [])
                    if shows(t) == what and (week, t["test_id"]) in made]
            items.append({
                "id": f"{week}/{shot.stem}", "kind": "source",
                "title": "Product photo" if len(names) == 1 else f"Product photo: {what}",
                "hook": hook(what),
                "test_id": None, "week": period(f"{week}/x"), "status": "src",
                "status_label": "Source", "image": _media_url(str(shot), media_dir),
                "images": [], "models": [], "after": "Used by " + ", ".join(used) if used
                else "Not used yet", "bad": False, "steps": [],
                "facts": [["Week", period(f"{week}/x")], ["Used by", ", ".join(used) or "None yet"]],
                "used_in": None, "deliverable": None, "source": None, "version": 1,
                "updated": None,
            })
    for e in ws.events.list("asset.uploaded"):
        p = e.payload
        items.append({
            "id": f"upload/{p['file']}", "kind": "upload", "title": p["name"],
            "hook": f"Uploaded by {OWNER}. The team asks before using it in an ad.",
            "test_id": None, "week": None, "status": "src", "status_label": "Uploaded",
            "image": _media_url(p["path"], media_dir), "images": [], "models": [],
            "after": f"{p['width']} x {p['height']}", "bad": False, "steps": [],
            "facts": [["File", p["name"]], ["Size", f"{p['width']} x {p['height']}"],
                      ["Used in", "Not used yet"]],
            "used_in": None, "deliverable": None, "source": None, "version": 1,
            "updated": e.ts,
        })
    return {"items": items, **_which(ws, account)}
