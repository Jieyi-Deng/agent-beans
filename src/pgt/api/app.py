"""FastAPI app over the workspace: read models, server-sent events, and the owner's actions.

Everything runs on one event loop, so the workspace connection is only used from one thread at a
time. Approvals go through `Approvals.grant`; the next stage then runs in the background.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from pgt.ads import meta_login
from pgt.api import board, views
from pgt.api.memory_routes import add_memory_routes
from pgt.harness import access
from pgt.harness.workspace import Workspace
from pgt.loops.notes import route_message, route_send_back, triage_notes
from pgt.schemas import ChangeSet
from pgt.schemas.settings import PRESETS, HouseRules


@dataclass
class Team:
    ws: Workspace
    model: object
    media: object
    media_dir: str
    account: object = None
    fake: object = None
    meter: object = None  # CostMeter shared by every loop; live runs carry a total cap
    mode: str = "offline"  # "offline", "live" (Meta API) or "mixed" (Baseten + Meta API)
    busy: str | None = None  # what the team is doing right now, if anything
    error: str | None = None
    tasks: set = field(default_factory=set)
    public: bool = False  # the hosted demo: sandbox only, no real Meta login
    meta: dict = field(default_factory=dict)  # login state, token and granted accounts; memory only
    db: str = ":memory:"  # the sandbox workspace; a real account gets its own file next to it
    model_name: str = ""
    parked: tuple | None = None  # the sandbox (ws, meter, account) while a real account is in use
    template: str = "app"  # which synthetic business the sandbox runs: see pgt.sandbox.TEMPLATES

    @property
    def real(self) -> bool:
        return self.parked is not None

    def run(self, label: str, coro) -> None:
        if self.busy:
            coro.close()
            raise HTTPException(409, f"The team is busy: {self.busy}")
        self.busy, self.error = label, None

        async def wrapper():
            try:
                await coro
            except Exception as exc:  # noqa: BLE001 - shown in the UI; the event log has the rest
                self.error = f"{label} failed: {exc}"
            finally:
                self.busy = None

        task = asyncio.create_task(wrapper())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


async def seed(team: Team, days: int | None = None, seed: int = 7) -> None:
    """Run the template's history, then plan this week's review up to the plan approval.

    Each earlier week is a full review the simulated owner approved: plan, new ads, then the
    paused ads created. So a store two weeks in already has a creative library and a test live.
    """
    from pgt.loops.close import close_week
    from pgt.loops.simulate import simulate
    from pgt.loops.weekly import apply_week, plan_week, produce_week
    from pgt.sandbox import TEMPLATES

    template = TEMPLATES[team.template]
    days = days or 7 * template.history_weeks
    if access.connection(team.ws) is None:  # the history starts with the sandbox connected at L2
        access.connect(team.ws, dict(access.SANDBOX, name=template.sandbox_name), access.APPLY,
                       sim_day=0)
    account = None
    while True:
        week_days = min(7, days - (account.day if account else 0))
        sim = await simulate(week_days, team.model, team.ws, seed=seed, meter=team.meter,
                             template=team.template, account=account)
        account = team.account = sim.account
        summary = await plan_week(team.ws, account, team.model, meter=team.meter)
        if account.day >= days:
            return
        if summary.stage != "awaiting_plan_approval":
            raise RuntimeError(f"Seeding stopped at week {summary.week}: {summary.stage}")
        _simulated_grant(team.ws, summary.plan_id, account.day)
        summary = await produce_week(team.ws, account, team.model, team.media, team.media_dir,
                                     meter=team.meter)
        if summary.stage != "awaiting_change_approval":
            raise RuntimeError(f"Seeding stopped at week {summary.week}: {summary.stage}")
        key = f"week{summary.week}/change_set"
        _simulated_grant(team.ws, key, account.day)
        _, receipt = apply_week(team.ws, account)
        # The Operator only creates ads paused; the owner turns them on in Ads Manager.
        writer = account.writer()
        for ad_id in receipt.created_ad_ids:
            writer.set_ad_status(ad_id, "ACTIVE")
        names = {a: account.state().ad(a).name for a in receipt.created_ad_ids}
        team.ws.events.append("ads.turned_on", "system",
                              {"ad_ids": receipt.created_ad_ids, "names": names},
                              sim_day=account.day)
        await close_week(team.ws, team.model, summary.week, meter=team.meter)


def _simulated_grant(ws: Workspace, key: str, day: int) -> None:
    """The seeded history stands in for the owner; the room says so."""
    record = ws.artifacts.latest(key)
    ws.events.append("approval.simulated", "system", {"key": key, "note": "Seeded history."},
                     refs=[record.artifact_id], sim_day=day)
    ws.approvals.grant(key, record, actor="user")


def offline_team(db: str, media_dir: str) -> Team:
    from pgt.harness.policy import CostMeter
    from pgt.llm.media import MediaClient
    from pgt.llm.offline import offline_model

    model, fake = offline_model("muse-spark-1.3")
    ws = Workspace(db)
    return Team(ws, model, MediaClient(fake.client()), media_dir, fake=fake,
                meter=CostMeter(ws.events), db=db)


def live_team(
    db: str, media_dir: str, model_name: str = "muse-spark-1.3", provider: str = "mixed",
    effort: str = "medium", cap: float = 5.0,
) -> Team:
    """A team on the real models. Every paid call is checked against `cap` (US dollars, at list
    price, for the whole workspace) before it is sent. Keys come from the environment or .env."""
    from dotenv import load_dotenv

    from pgt.harness.policy import CostMeter
    from pgt.llm.media import MediaClient
    from pgt.llm.meta import meta_client, responses_model

    load_dotenv()
    client = meta_client()
    model = responses_model(client, model_name)
    if provider == "mixed":
        from pgt.llm.baseten import baseten_client, chat_model
        from pgt.llm.routing import mixed

        model = mixed(model, chat_model(baseten_client(), effort=effort))
    ws = Workspace(db)
    return Team(ws, model, MediaClient(client), media_dir, meter=CostMeter(ws.events, total=cap),
                mode="mixed" if provider == "mixed" else "live", db=db, model_name=model_name)


def _real_db(team: Team, account_id: str) -> str:
    if team.db == ":memory:":
        return ":memory:"
    return str(Path(team.db).with_name(f"meta-{account_id}.db"))


def use_real_account(team: Team, account: dict, transport=None, mcp_transport=None,
                     rules: HouseRules | None = None) -> dict:
    """Move the team onto a real ad account, in a workspace of its own.

    Reads go through Meta's Ads MCP (META_ADS_MCP=0 turns it off), with the Graph API as the
    fallback; writes go through the Graph API. The sandbox's history stays in its own file and
    comes back on disconnect. The token stays in memory; the workspace keeps only the account id,
    the origin date and Agent Beans' own writes. A first connect starts the team cold: the sandbox's
    facts with the house rules chosen at setup, and no history."""
    from pgt.ads.ads_mcp import AdsMcp
    from pgt.ads.graph import Graph
    from pgt.ads.meta_account import MetaAccount
    from pgt.harness.policy import CostMeter

    if team.model_name.endswith("-contributor"):
        raise HTTPException(409, "Real ad data never goes to a -contributor model")
    token = team.meta.get("token")
    if not token:
        raise HTTPException(409, "Sign in with Facebook and grant this ad account first")
    if team.real:
        leave_real_account(team)
    ws = Workspace(_real_db(team, account["account_id"]))
    if ws.facts.version == 0:
        # Start from the sandbox's facts; the owner corrects them in Settings.
        facts = team.ws.facts.current()
        if rules is not None:
            facts = facts.model_copy(update={"house_rules": rules})
        ws.facts.set(facts, "user")
    earlier = [e for e in ws.events.list("account.connected")
               if e.payload.get("account_id") == account["account_id"] and e.payload.get("origin")]
    lookback = int(os.environ.get("META_LOOKBACK_DAYS", "7"))
    origin = (date.fromisoformat(earlier[-1].payload["origin"]) if earlier
              else date.today() - timedelta(days=lookback))
    meter = team.meter
    total = None if meter is None or meter.total is None else meter.remaining()
    team.parked = (team.ws, team.meter, team.account)
    team.ws, team.meter = ws, CostMeter(ws.events, total=total)
    mcp = None if os.environ.get("META_ADS_MCP") == "0" else AdsMcp(token, mcp_transport)
    team.account = MetaAccount.from_env(
        Graph(token, transport), account["account_id"], origin, mcp=mcp,
        currency=account.get("currency") or None,
        changes=[e.payload for e in ws.events.list("meta.write")],
        on_write=lambda record: ws.events.append("meta.write", "operator", record,
                                                 sim_day=record["day"]),
    )
    return {**account, "origin": origin.isoformat()}


def leave_real_account(team: Team) -> None:
    """Back to the sandbox. Spend on the real account still counts against the sandbox's cap."""
    if not team.real:
        return
    spent = team.meter.spent() if team.meter is not None else 0.0
    team.ws, team.meter, team.account = team.parked
    team.parked = None
    if team.meter is not None and team.meter.total is not None:
        team.meter.total = max(team.meter.spent(), team.meter.total - spent)


class Message(BaseModel):
    text: str
    to: str | None = None
    thread: int | None = None


class Note(BaseModel):
    note: str = ""


class Rules(BaseModel):
    """A change to the house rules. Missing fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    preset: Literal["conservative", "balanced", "aggressive"] | None = None
    daily_budget_cap: float | None = Field(None, gt=0, le=100_000)
    max_budget_change_pct: float | None = Field(None, gt=0, le=1)
    min_days_between_budget_changes: int | None = Field(None, ge=0, le=30)
    min_spend_multiple_before_pause: float | None = Field(None, ge=0, le=10)
    protect_learning_phase: bool | None = None


def _rules(current: HouseRules, body: Rules) -> HouseRules:
    values = current.model_dump()
    if body.preset:
        values.update(PRESETS[body.preset], preset=body.preset)
    fields = body.model_dump(exclude_none=True, exclude={"preset"})
    if fields:
        values.update(fields, preset="custom")
    return HouseRules(**values)


class Facts(BaseModel):
    """A change to the numbers the team plans with. Missing fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    weekly_budget: float | None = Field(None, gt=0, le=1_000_000)
    target_cost_per_result: float | None = Field(None, gt=0, le=100_000)
    test_budget_share: float | None = Field(None, gt=0, le=1)
    long_term_goal: str | None = Field(None, max_length=300)


class Connect(BaseModel):
    kind: Literal["sandbox", "meta"] = "sandbox"
    account_id: str | None = None
    level: int = Field(access.DEFAULT_LEVEL, ge=0, le=2)
    rules: Rules | None = None


class Level(BaseModel):
    level: int = Field(ge=0, le=2)


MAX_UPLOAD = 5 * 1024 * 1024


def create_app(team: Team, web_dist: str | Path | None = None, seed_days: int | None = 7) -> FastAPI:
    """With `seed_days` the sandbox starts with that many simulated days already run. Without, the
    team starts cold: no history, and the sandbox account appears when the owner connects it."""
    from pgt.sandbox import TEMPLATES, SandboxAccount

    template = TEMPLATES[team.template]
    sandbox = dict(access.SANDBOX, name=template.sandbox_name, blurb=template.blurb)

    @asynccontextmanager
    async def lifespan(_app):
        if seed_days and not team.ws.events.list("loop.started"):
            team.run("Setting up the demo week", seed(team, seed_days))
        elif not seed_days and team.ws.facts.version == 0:
            team.ws.facts.set(template.facts(), "user")
        yield
        for task in list(team.tasks):
            task.cancel()

    app = FastAPI(title="Agent Beans", lifespan=lifespan)
    app.state.team = team
    app.state.graph_transport = None  # tests put a fake Graph here
    app.state.mcp_transport = None  # and a fake Ads MCP server here

    def _model_ready():
        if team.real and team.fake is not None:
            raise HTTPException(409, "A real ad account needs the live team (pgt serve --live). "
                                     "The offline stand-in only knows the sandbox.")

    def _record(key: str):
        record = team.ws.artifacts.latest(key)
        if record is None:
            raise HTTPException(404, f"No deliverable {key}")
        return record

    @app.get("/api/status")
    async def status():
        last = team.ws.events.list(since_id=0)[-1:] or []
        return {"busy": team.busy, "error": team.error, "where": views.where(team.ws),
                "day": team.account.day if team.account else None,
                "day_one": (start := views.ws_day_one(team.ws)) and start.isoformat(),
                "last_event_id": last[0].id if last else 0, "owner": views.OWNER,
                "mode": team.mode}

    @app.get("/api/stream")
    async def stream():
        async def gen():
            last, busy = -1, object()
            while True:
                tail = team.ws.events.list(since_id=max(last, 0))
                newest = tail[-1].id if tail else max(last, 0)
                if newest != last or team.busy != busy:
                    last, busy = newest, team.busy
                    yield f"data: {json.dumps({'last_event_id': last, 'busy': busy})}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/api/room")
    async def room():
        idx = views.Index.build(team.ws)
        return {"where": views.where(team.ws), "items": views.room(team.ws, team.account, idx)}

    @app.get("/api/deliverables")
    async def deliverables():
        return {"items": views.deliverables(team.ws, team.account),
                "account_name": board._which(team.ws, team.account)["account_name"]}

    @app.get("/api/deliverables/{key:path}")
    async def deliverable(key: str):
        d = views.detail(team.ws, key, team.account, team.media_dir)
        if d is None:
            raise HTTPException(404, f"No deliverable {key}")
        return d

    @app.post("/api/approve/{key:path}")
    async def approve(key: str):
        from pgt.loops.weekly import apply_week, produce_week
        from pgt.roles.operator import apply_change_set

        record = _record(key)
        idx = views.Index.build(team.ws)
        if idx.waiting(record) is None:
            raise HTTPException(409, "This version is not waiting on you")
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}")
        if not access.can_propose(team.ws):
            raise HTTPException(409, "The team is set to L0 Observe and acts on nothing. Raise "
                                     "the level in Settings to approve proposals.")
        approved = None
        if record.kind == "ChangeSet" and record.content.get("changes"):
            approved = [g.change_index for g in ChangeSet.model_validate(record.content).gate
                        if g.allowed]
        try:
            team.ws.approvals.grant(key, record, approved=approved)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        period = key.split("/")[0]
        if key.endswith("/change_set") and not access.can_apply(team.ws):
            content = record.content
            team.ws.events.append(
                "changeset.handed_off", "operator",
                {"key": key, "changes": len(approved or []),
                 "ads": len(content.get("ad_drafts") or [])},
                refs=[record.artifact_id], sim_day=team.account.day if team.account else None)
            return {"ok": True, "handed_off": True}
        if key.endswith("/test_plan") and period.startswith("week"):
            week = int(period[4:])
            team.run("Producing creative", produce_week(
                team.ws, team.account, team.model, team.media, team.media_dir, week=week,
                meter=team.meter))
        elif key.endswith("/change_set") and period.startswith("week"):
            week = int(period[4:])

            async def apply():
                from pgt.loops.close import close_week

                apply_week(team.ws, team.account, week=week)
                await close_week(team.ws, team.model, week, meter=team.meter)

            team.run("Applying the change set", apply())
        elif key.endswith("/change_set"):
            async def apply_day():
                apply_change_set(team.ws, team.account, key)

            team.run("Applying the change set", apply_day())
        return {"ok": True}

    @app.post("/api/send-back/{key:path}")
    async def send_back(key: str, body: Note):
        """Record the owner's note, then have the role that made this version redo it with the
        note as the first issue to fix. The new version comes back for approval as usual."""
        from pgt.loops.daily import revise_day
        from pgt.loops.weekly import produce_week, revise_plan

        record = _record(key)
        note = body.note.strip()
        if views.Index.build(team.ws).waiting(record) is None:
            raise HTTPException(409, "This version is not waiting on you")
        if not note:
            raise HTTPException(400, "Say what should change, so the team knows what to redo")
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}")
        period, _, kind = key.partition("/")
        redo, label = None, None
        if period.startswith("week") and kind == "test_plan":
            label = "Redoing the test plan"
            redo = revise_plan(team.ws, team.account, team.model, note, week=int(period[4:]),
                               meter=team.meter)
        elif period.startswith("week") and kind == "change_set":
            label = "Redoing the creative"
            redo = produce_week(team.ws, team.account, team.model, team.media, team.media_dir,
                                week=int(period[4:]), meter=team.meter, note=note)
        elif (period.startswith("day") and kind == "change_set" and team.account
              and int(period[3:]) == team.account.day):
            label = "Redoing today's change set"
            redo = revise_day(team.ws, team.account, team.model, note, meter=team.meter)
        returned = team.ws.events.append("approval.returned", "user",
                                         {"key": key, "note": note, "redo": redo is not None},
                                         refs=[record.artifact_id], sim_day=record.sim_day)
        route_send_back(team.ws, returned, key, record.author)
        if redo is not None:
            team.run(label, redo)
        return {"ok": True, "redo": redo is not None}

    @app.post("/api/messages")
    async def post_message(body: Message):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Empty message")
        day = team.account.day if team.account else None
        e = team.ws.events.append("message.posted", "user",
                                  {"text": text, "to": body.to, "thread": body.thread},
                                  sim_day=day)
        to = route_message(team.ws, e, body.to, body.thread)
        # A note for the Growth Lead is read now when the team is free; otherwise at the start
        # of the next daily check or weekly review.
        if to == "growth_lead" and not team.busy and not (team.real and team.fake is not None):
            team.run("Reading your note",
                     triage_notes(team.ws, team.model, team.meter, day))
        return {"id": e.id, "to": to}

    @app.post("/api/run/day")
    async def run_day():
        """Advance the sandbox one day and run the daily check. Changes wait on the owner."""
        from pgt.loops.daily import run_daily_check

        if team.account is None:
            raise HTTPException(409, "The demo week is still being set up" if seed_days
                                else "Connect an ad account first")
        _model_ready()

        async def day():
            team.account.advance_day()
            brief = await run_daily_check(team.ws, team.account, team.model, team.meter)
            key = f"day{team.account.day}/change_set"
            record = team.ws.artifacts.latest(key)
            if (record and access.can_propose(team.ws)
                    and brief.review and brief.review.verdict == "pass"
                    and any(g.allowed for g in ChangeSet.model_validate(record.content).gate)):
                team.ws.events.append("approval.requested", "media_buyer",
                                      {"key": key, "what": "change set"},
                                      refs=[record.artifact_id], sim_day=team.account.day,
                                      loop_id=f"daily-d{team.account.day}")

        team.run(f"Daily check, day {team.account.day + 1}", day())
        return {"ok": True}

    @app.post("/api/run/week")
    async def run_week():
        from pgt.loops.weekly import plan_week

        if team.account is None:
            raise HTTPException(409, "The demo week is still being set up" if seed_days
                                else "Connect an ad account first")
        _model_ready()
        if not access.can_propose(team.ws):
            raise HTTPException(409, "The team is set to L0 Observe, so it does not plan tests. "
                                     "Raise the level in Settings first.")
        team.run("Planning the week",
                 plan_week(team.ws, team.account, team.model, meter=team.meter))
        return {"ok": True}

    @app.get("/api/settings")
    async def settings():
        calls = team.ws.events.list("model.call")
        cap = getattr(team.meter, "total", None)
        return {
            "facts": team.ws.facts.current().model_dump(mode="json"),
            "fact_version": team.ws.facts.version,
            "model_calls": len(calls),
            "tokens": sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in calls),
            "cost": round(sum(e.payload["cost"] for e in calls), 4),
            "offline": team.fake is not None,
            "mode": team.mode,
            "usage": views.usage(team.ws),
            "cap": cap,
            "remaining": None if cap is None else round(team.meter.remaining(), 4),
            "day": team.account.day if team.account else None,
            "now": datetime.now(UTC).isoformat(),
        }

    def _day():
        return team.account.day if team.account else None

    @app.get("/api/access")
    async def get_access():
        facts = team.ws.facts.current()
        return {
            "connection": access.connection(team.ws),
            "level": access.level(team.ws),
            "levels": access.LEVELS,
            "sandbox": sandbox,
            "public": team.public,
            "meta_login": meta_login.unavailable(team.public),
            "meta_accounts": team.meta.get("accounts", []),
            "rules": facts.house_rules.model_dump(mode="json"),
            "presets": PRESETS,
            "weekly_budget": facts.weekly_budget,
            "weekly_test_budget": round(facts.weekly_budget * facts.test_budget_share, 2),
            "constraints": facts.constraints,
            "fact_version": team.ws.facts.version,
            "reads": team.account.read_sources() if team.real else None,
        }

    def _set_rules(body: Rules, setup: bool = False) -> int:
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}. Change the rules after.")
        facts = team.ws.facts.current()
        rules = _rules(facts.house_rules, body)
        if rules == facts.house_rules:
            return team.ws.facts.version
        return team.ws.facts.set(facts.model_copy(update={"house_rules": rules}), "user",
                                 setup=setup)

    def _refresh(before: int) -> bool:
        """After the owner changed a fact, redo the stale work that needs no model."""
        from pgt.loops.refresh import refresh

        if team.ws.facts.version == before or team.account is None:
            return False
        changed = team.ws.events.list("fact.changed")[-1].payload["changed"]
        team.run("Re-running stale work", refresh(team.ws, team.account, changed))
        return True

    @app.post("/api/house-rules")
    async def house_rules(body: Rules):
        before = team.ws.facts.version
        version = _set_rules(body)
        return {"ok": True, "fact_version": version, "refresh": _refresh(before)}

    @app.post("/api/facts")
    async def set_facts(body: Facts):
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}. Change the facts after.")
        before = team.ws.facts.version
        update = body.model_dump(exclude_none=True)
        if update:
            team.ws.facts.set(team.ws.facts.current().model_copy(update=update), "user")
        return {"ok": True, "fact_version": team.ws.facts.version, "refresh": _refresh(before)}

    @app.post("/api/access/connect")
    async def connect(body: Connect):
        if body.kind == "sandbox":
            if team.busy:
                raise HTTPException(409, f"The team is busy: {team.busy}. Connect after.")
            leave_real_account(team)
            account = {k: v for k, v in sandbox.items() if k != "blurb"}
            if team.account is None and not seed_days:
                team.account = SandboxAccount(template.scenario())
        else:
            reason = meta_login.unavailable(team.public)
            if reason:
                raise HTTPException(409, reason)
            granted = {a["account_id"]: a for a in team.meta.get("accounts", [])}
            if body.account_id not in granted:
                raise HTTPException(409, "Sign in with Facebook and grant this ad account first")
            if team.busy:
                raise HTTPException(409, f"The team is busy: {team.busy}. Connect after.")
            rules = _rules(team.ws.facts.current().house_rules, body.rules) if body.rules \
                else None
            account = use_real_account(team, granted[body.account_id], app.state.graph_transport,
                                       app.state.mcp_transport, rules)
        cold = team.real and not team.ws.events.list("account.connected")
        try:
            access.connect(team.ws, account, body.level, sim_day=_day())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        # A real account is read on every connect; the sandbox only before the team has run.
        kick = team.real or (team.account is not None
                             and not team.ws.events.list("loop.started"))
        stale = None
        if body.rules and kick and not team.real:
            _set_rules(body.rules, setup=True)  # the sandbox before its first run: nothing stale
        elif body.rules and not cold:  # a first connect already started with these rules
            before = team.ws.facts.version
            _set_rules(body.rules)
            if not kick:
                _refresh(before)
            elif team.ws.facts.version != before:  # one task: refresh, then read the account
                from pgt.loops.refresh import refresh

                changed = team.ws.events.list("fact.changed")[-1].payload["changed"]
                stale = refresh(team.ws, team.account, changed)
        if kick:
            from pgt.loops.kickoff import kickoff

            name = account.get("name") or account["account_id"]

            async def read():
                if stale is not None:
                    await stale
                await kickoff(team.ws, team.account, name)

            team.run("Reading the account", read())
        return {"ok": True}

    @app.post("/api/access/level")
    async def set_level(body: Level):
        access.set_level(team.ws, body.level, sim_day=_day())
        return {"ok": True, "level": body.level}

    @app.post("/api/access/disconnect")
    async def disconnect():
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}. Disconnect after.")
        access.disconnect(team.ws, sim_day=_day())
        leave_real_account(team)
        team.meta.clear()
        return {"ok": True}

    @app.get("/api/meta/login")
    async def meta_start(request: Request):
        """Send the owner to Facebook Login for Business on facebook.com. Local installs only."""
        reason = meta_login.unavailable(team.public)
        if reason:
            raise HTTPException(409, reason)
        state = meta_login.new_state()
        redirect = str(request.url_for("meta_callback"))
        team.meta = {"state": state, "redirect": redirect}
        return RedirectResponse(meta_login.login_url(redirect, state), status_code=302)

    @app.get("/api/meta/callback", name="meta_callback")
    async def meta_callback(code: str = "", state: str = "", error: str = ""):
        expected = team.meta.get("state")
        if error or not code:
            team.meta.clear()
            return RedirectResponse("/#/settings?meta=cancelled", status_code=302)
        if not expected or not secrets.compare_digest(state, expected):
            team.meta.clear()
            raise HTTPException(400, "Login state does not match. Start again from Settings.")
        try:
            token = await asyncio.to_thread(meta_login.exchange, code, team.meta["redirect"])
            accounts = await asyncio.to_thread(meta_login.ad_accounts, token)
        except Exception:  # noqa: BLE001 - the token and Meta's reply are not shown or logged
            team.meta.clear()
            return RedirectResponse("/#/settings?meta=failed", status_code=302)
        team.meta = {"token": token, "accounts": accounts}
        return RedirectResponse("/#/settings?meta=pick", status_code=302)

    @app.get("/api/meta/mcp")
    async def meta_mcp():
        """What the Ads MCP offers this account, and which source answered each read."""
        if not team.real:
            raise HTTPException(409, "Connect a Meta ad account first")
        from pgt.ads.ads_mcp import McpError

        try:
            tools = await asyncio.to_thread(team.account.mcp_tools)
        except McpError as exc:
            tools = {"error": str(exc)}
        return {**tools, **team.account.read_sources()}

    @app.get("/api/drafts")
    async def drafts():
        """Ads Manager drafts on the connected account, read only: a real account's through the
        Ads MCP, or the sandbox's synthetic ones."""
        from pgt.ads.ads_mcp import McpError, draft_counts

        if team.account is None:
            return {"available": False, "reason": "Connect an ad account first", "drafts": []}
        try:
            found = await asyncio.to_thread(team.account.drafts)
        except McpError as exc:
            return {"available": False, "reason": str(exc), "drafts": []}
        return {"available": True, "reason": None, "drafts": found,
                "counts": draft_counts(found)}

    @app.get("/api/dashboard")
    async def dashboard(days: int = 7):
        return board.dashboard(team.ws, team.account, days)

    @app.get("/api/assets")
    async def assets():
        return board.assets(team.ws, team.account, team.media_dir)

    @app.post("/api/assets/upload")
    async def upload(request: Request, name: str = "upload.png"):
        """A product photo from the owner. Checked as an image, re-saved as PNG, never trusted."""
        import hashlib
        import io
        import re

        from PIL import Image, UnidentifiedImageError

        body = await request.body()
        if not body:
            raise HTTPException(400, "Empty upload")
        if len(body) > MAX_UPLOAD:
            raise HTTPException(413, "Images up to 5 MB")
        try:
            img = Image.open(io.BytesIO(body))
            if img.format not in ("PNG", "JPEG"):
                raise HTTPException(415, "PNG or JPEG only")
            if img.width * img.height > 40_000_000:
                raise HTTPException(413, "Image too large")
            img.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise HTTPException(415, "Not an image") from exc
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
        out = io.BytesIO()
        img.save(out, "PNG")
        data = out.getvalue()
        digest = hashlib.sha256(data).hexdigest()[:12]
        folder = Path(team.media_dir) / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{digest}.png"
        path.write_bytes(data)
        clean = re.sub(r"[^A-Za-z0-9 ._-]", "", Path(name).name)[:80] or "upload.png"
        sim_day = team.account.day if team.account else None
        team.ws.events.append("asset.uploaded", "user", {
            "file": path.name, "path": str(path), "name": clean,
            "width": img.width, "height": img.height, "sha256": digest,
        }, sim_day=sim_day)
        return {"ok": True, "id": f"upload/{path.name}"}

    @app.get("/api/media/{rel:path}")
    async def media(rel: str):
        root = Path(team.media_dir).resolve()
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    add_memory_routes(app, team, _refresh)

    if web_dist and Path(web_dist).is_dir():
        dist = Path(web_dist)
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str):
            file = (dist / path).resolve()
            if path and file.is_relative_to(dist.resolve()) and file.is_file():
                return FileResponse(file)
            # Never cache the page itself, so a rebuild shows up on the next load.
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app
