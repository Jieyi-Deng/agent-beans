"""Command-line entry points:

pgt simulate --days 7 [--template app|ecommerce] [--live] [--provider meta|mixed] [--db path]
pgt weekly [--template app|ecommerce] [--yes] [--live] [--provider meta|mixed] [--db path]
           [--out runs/media]
pgt serve [--template app|ecommerce] [--port 8000] [--db path] [--live] [--provider meta|mixed]
          [--cap 5] [--public] [--cold] [--ssl-certfile cert.pem --ssl-keyfile key.pem]
pgt replay --source http://127.0.0.1:8000 [--out replay]
pgt inspect-call <event id> --db path      print exactly what one model call saw
pgt usage --db path [--loop daily-d3]      calls, cached share, cost and trims per role

replay records a running team as the static, read-only site that GitHub Pages serves.

--template picks the synthetic business in the sandbox: "app" is Beans' Active, a fitness app;
"ecommerce" is Northpaw Supply, an online pet store with two weeks of history.

Live runs default to --provider mixed: every role runs on Muse Spark Open (open weights) on
Baseten, except the Creative Strategist's hosted web search and the image work (Muse Image,
SAM 3.1), which stay on the Meta API. --provider meta runs every role on the Meta API.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from pgt.harness.workspace import Workspace


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    parser = argparse.ArgumentParser(prog="pgt", description="Agent Beans")
    sub = parser.add_subparsers(dest="command", required=True)
    sim = sub.add_parser("simulate", help="Run the daily check over simulated days")
    sim.add_argument("--days", type=int, default=7)
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--db", default=":memory:", help="SQLite file for the workspace")
    sim.add_argument("--live", action="store_true", help="Call Muse (costs money; needs a key)")
    sim.add_argument("--model", default="muse-spark-1.3")
    sim.add_argument("--provider", choices=["meta", "mixed"], default="mixed")
    sim.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh"],
        default="medium",
        help="Reasoning effort on Baseten",
    )
    sim.add_argument("--plant-injection", action="store_true")
    week = sub.add_parser("weekly", help="Simulate a week, then run the weekly review")
    week.add_argument("--seed", type=int, default=7)
    week.add_argument("--db", default=":memory:", help="SQLite file for the workspace")
    week.add_argument("--out", default="runs/media", help="Folder for creative images")
    week.add_argument("--live", action="store_true", help="Call Muse (costs money; needs a key)")
    week.add_argument("--model", default="muse-spark-1.3")
    week.add_argument("--provider", choices=["meta", "mixed"], default="mixed")
    week.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh"],
        default="medium",
        help="Reasoning effort on Baseten",
    )
    week.add_argument("--yes", action="store_true", help="Approve both steps without asking")
    serve = sub.add_parser("serve", help="Run the web app (offline unless --live)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default=None, help="SQLite file (default: a new one in runs/web)")
    serve.add_argument("--web", default="web/dist", help="Built frontend to serve, if present")
    serve.add_argument("--live", action="store_true", help="Call Muse (costs money; needs a key)")
    serve.add_argument("--model", default="muse-spark-1.3")
    serve.add_argument("--provider", choices=["meta", "mixed"], default="mixed")
    serve.add_argument("--effort", choices=["low", "medium", "high", "xhigh"], default="medium")
    serve.add_argument(
        "--public", action="store_true", default=bool(os.environ.get("PGT_PUBLIC")),
        help="Hosted demo: sandbox only, no real Meta login (also PGT_PUBLIC=1)",
    )
    serve.add_argument("--cold", action="store_true",
                       help="Start the team cold: no demo week, the sandbox appears on connect")
    serve.add_argument("--ssl-certfile", default=None,
                       help="Serve over HTTPS, as Meta login needs (also PGT_TLS_CERT)")
    serve.add_argument("--ssl-keyfile", default=None, help="Key for --ssl-certfile "
                       "(also PGT_TLS_KEY)")
    serve.add_argument(
        "--cap", type=float, default=5.0,
        help="Live runs stop before any call once this many dollars are spent (list price)",
    )
    serve.add_argument("--owner", default=None,
                       help="The owner's name, as the app shows it (also set on first run)")
    serve.add_argument("--owner-avatar", default=None,
                       help="PNG or JPEG for the owner's picture (default: their initials)")
    rep = sub.add_parser("replay", help="Record a running team as a static, read-only site")
    rep.add_argument("--source", default="http://127.0.0.1:8000", help="The running pgt serve")
    rep.add_argument("--out", default="replay", help="Folder for data/ and media/")
    ins = sub.add_parser("inspect-call", help="Print exactly what one model call saw")
    ins.add_argument("event_id", type=int, help="The model.call event ID")
    ins.add_argument("--db", required=True, help="SQLite file of the run")
    use = sub.add_parser("usage", help="Calls, cached share, cost and trims per role")
    use.add_argument("--db", required=True, help="SQLite file of the run")
    use.add_argument("--loop", default=None, help="Only this loop, e.g. daily-d3 or weekly-w1")
    for p in (sim, week, serve):
        p.add_argument("--template", choices=["app", "ecommerce"], default="app",
                       help="Synthetic business in the sandbox")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "inspect-call":
        from pgt.harness.requests import inspect_call, render

        ws = Workspace(args.db)
        try:
            print(render(inspect_call(ws, args.event_id)))
        except LookupError as e:
            raise SystemExit(str(e)) from None
        finally:
            ws.close()
        return None
    if args.command == "usage":
        from pgt.harness.report import harness_report, render

        ws = Workspace(args.db)
        try:
            print(render(harness_report(ws, args.loop)))
        finally:
            ws.close()
        return None
    if args.command == "replay":
        from pgt.replay import export, http_get

        manifest = export(http_get(args.source), Path(args.out))
        print(f"Replay in {args.out}: {manifest['files']} views, {manifest['images']} images, "
              f"{manifest['model_calls']} model calls for ${manifest['cost']:.2f} ({manifest['mode']}).")
        return None
    asyncio.run(_simulate(args) if args.command == "simulate" else _weekly(args))


def _models(args):
    """Returns (text model or per-role router, AsyncOpenAI client for images, fake or None)."""
    from pgt.llm.meta import meta_client, responses_model
    from pgt.llm.offline import offline_model

    if args.live:
        from dotenv import load_dotenv

        load_dotenv()
        client = meta_client()
        model = responses_model(client, args.model)
        if args.provider == "mixed":
            from pgt.llm.baseten import BASETEN_MODEL, baseten_client, chat_model
            from pgt.llm.routing import mixed

            model = mixed(model, chat_model(baseten_client(), effort=args.effort))
            print(f"Live run: every role on {BASETEN_MODEL} (Baseten), web search on {args.model}.")
        else:
            print(f"Live run on {args.model}.")
        return model, client, None
    model, fake = offline_model(args.model)
    print("Offline run: a rule-based stand-in answers instead of Muse (no cost).")
    return model, fake.client(), fake


def _serve(args) -> None:
    from datetime import datetime

    import uvicorn
    from dotenv import load_dotenv

    from pgt.api.app import create_app, live_team, offline_team

    load_dotenv()  # Meta login settings; offline runs still never call a model

    if args.db:  # the media lives next to the workspace it belongs to
        run_dir = Path(args.db).with_suffix("")
        run_dir.mkdir(parents=True, exist_ok=True)
    else:  # a new folder; two servers started in the same second must not share one
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for n in range(1, 100):
            run_dir = Path("runs/web") / (stamp if n == 1 else f"{stamp}-{n}")
            try:
                run_dir.mkdir(parents=True)
                break
            except FileExistsError:
                continue
    db = args.db or str(run_dir / "workspace.db")
    if args.live:
        team = live_team(db, str(run_dir / "media"), args.model, args.provider, args.effort,
                         args.cap)
        where = "Muse Spark Open on Baseten; web search and images on the Meta API" \
            if args.provider == "mixed" else f"{args.model} on the Meta API"
        print(f"Live run: {where}. Spend cap ${args.cap:g} at list price.")
    else:
        team = offline_team(db, str(run_dir / "media"))
        print("Offline demo: a rule-based stand-in answers instead of Muse (no cost).")
    team.public = args.public
    team.template = args.template
    if args.owner or args.owner_avatar:
        _set_owner(team, args.owner, args.owner_avatar)
    cert = args.ssl_certfile or os.environ.get("PGT_TLS_CERT") or None
    key = args.ssl_keyfile or os.environ.get("PGT_TLS_KEY") or None
    if bool(cert) != bool(key):
        raise SystemExit("HTTPS needs both a certificate and its key")
    # Meta matches the login callback exactly, so open the address the callback was saved with.
    host = "localhost" if args.host in {"127.0.0.1", "localhost"} else args.host
    print(f"Workspace {db}. Open {'https' if cert else 'http'}://{host}:{args.port}")
    from pgt.sandbox import TEMPLATES

    days = 0 if args.cold else 7 * TEMPLATES[args.template].history_weeks
    uvicorn.run(create_app(team, args.web, seed_days=days), host=args.host, port=args.port,
                log_level="warning", ssl_certfile=cert, ssl_keyfile=key)


def _set_owner(team, name: str | None, picture: str | None) -> None:
    from PIL import Image

    from pgt.harness import owner

    day = team.account.day if team.account else None
    try:
        if name:
            owner.set_name(team.ws, name, day)
        if picture:
            with Image.open(picture) as img:
                img.load()
                owner.set_avatar(team.ws, team.media_dir, img, day)
    except (owner.ProfileError, OSError) as exc:
        raise SystemExit(f"--owner: {exc}") from exc
    print(f"Owner: {owner.name(team.ws)}"
          + (", with their picture" if owner.profile(team.ws)["avatar"] else ""))


def _ask(question: str, yes: bool) -> bool:
    if yes:
        print(f"{question} yes (--yes)")
        return True
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


async def _weekly(args) -> None:
    from pgt.llm.media import MediaClient
    from pgt.loops.close import close_week
    from pgt.loops.simulate import simulate
    from pgt.loops.weekly import apply_week, plan_week, produce_week
    from pgt.schemas import ChangeSet, TestPlan

    model, image_client, fake = _models(args)
    ws = Workspace(args.db)
    try:
        sim = await simulate(7, model, ws, seed=args.seed, template=args.template)
        account = sim.account
        print(f"Simulated days 1-{account.day}: {len(sim.briefs)} daily checks.")

        summary = await plan_week(ws, account, model)
        if summary.stage != "awaiting_plan_approval":
            return _stop(summary)
        plan = TestPlan.model_validate(ws.artifacts.latest(summary.plan_id).content)
        print(f"\nWeek {plan.week} test plan: {plan.summary}")
        print(f"Test budget: ${plan.available_test_budget:g} for the week.")
        for t in plan.tests:
            print(f"  {t.test_id} {t.name}: ${t.weekly_budget:g} in {t.campaign_id}")
            print(f"    Hypothesis: {t.hypothesis}")
            print(f"    Hook: {t.brief.hook}  (evidence {', '.join(t.evidence_ids)})")
        if not _ask("Approve the test plan?", args.yes):
            print("Stopped before production. Nothing was changed.")
            return
        ws.approvals.grant(summary.plan_id, ws.artifacts.latest(summary.plan_id))

        summary = await produce_week(ws, account, model, MediaClient(image_client), args.out)
        for e in ws.events.list("question.answered", loop_id=f"weekly-w{plan.week}"):
            print(f"  Analyst answered {e.payload['task_id']}: {e.payload['answer']}")
        for e in ws.events.list("review.redo", loop_id=f"weekly-w{plan.week}"):
            p = e.payload
            print(f"  Redo {p['subject']} (by {p.get('by', 'reviewer')}): {'; '.join(p['issues'])}")
        if summary.stage != "awaiting_change_approval":
            return _stop(summary)
        key = f"week{plan.week}/change_set"
        change_set = ChangeSet.model_validate(ws.artifacts.latest(key).content)
        print(f"\n{change_set.summary}")
        for d in change_set.ad_drafts:
            creative = ws.artifacts.latest(f"week{plan.week}/creative/{d.test_id}").content
            copy = creative["ad_copy"]
            print(f'  {d.name} in {d.campaign_id}: "{copy["headline"]}"')
            print(f"    {copy['primary_text']}")
            folder = Path(creative["images"][0]["path"]).parent
            scenes = len(copy.get("scenes", [])) or 1
            print(f"    {scenes} scenes x 3 sizes (1:1, 4:5, 9:16): {folder}")
            if creative.get("video"):
                print(f"    10-second video: {creative['video']['path']}")
        if not _ask("Approve creating these ads, paused?", args.yes):
            print("Stopped before the sandbox. Nothing was changed.")
            return
        ws.approvals.grant(key, ws.artifacts.latest(key))
        summary, receipt = apply_week(ws, account)
        print(f"\nCreated {', '.join(receipt.created_ad_ids)} in the sandbox, all PAUSED.")
        closed = await close_week(ws, model, plan.week)
        if closed:
            print(f"Closed week {plan.week}: memory versions {closed['versions']}, "
                  f"{closed['rejected']} line(s) rejected.")
        _totals(ws, fake, f"weekly-w{plan.week}")
    finally:
        ws.close()


def _stop(summary) -> None:
    print(f"\nWeekly review stopped: {summary.stage}.")
    for reason in summary.escalations:
        print(f"  Needs you: {reason}")


def _totals(ws, fake, loop_id: str) -> None:
    calls = ws.events.list("model.call", loop_id=loop_id)
    tokens = sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in calls)
    cost = sum(e.payload["cost"] for e in calls)
    label = "estimated at list price" if fake else "at list price"
    print(f"Weekly review: {len(calls)} model calls, {tokens:,} tokens, ${cost:.4f} {label}.")
    if fake is not None:
        print(f"Meta request rule violations: {len(fake.violations)}")


async def _simulate(args) -> None:
    from pgt.loops.simulate import simulate

    model, _, fake = _models(args)

    ws = Workspace(args.db)
    try:
        sim = await simulate(
            args.days, model, ws, seed=args.seed, plant_injection=args.plant_injection,
            template=args.template,
        )
        receipts = {r.day: r for r in sim.receipts}
        for brief in sim.briefs:
            print(f"\n{brief.headline}")
            for f in brief.findings:
                print(f"  [{f.severity}] {f.subject_id}: {f.claim}  ({', '.join(f.evidence_ids)})")
            for reason in brief.blocked:
                print(f"  {reason}")
            if brief.review and brief.review.issues:
                print(f"  Review {brief.review.verdict}: {'; '.join(brief.review.issues)}")
            if brief.day in receipts:
                print(f"  Applied changes {receipts[brief.day].applied} (simulated approval).")
        calls = ws.events.list("model.call")
        tokens = sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in calls)
        cost = sum(e.payload["cost"] for e in calls)
        label = "estimated at list price" if fake else "at list price"
        print(f"\n{len(calls)} model calls, {tokens:,} tokens, ${cost:.4f} {label}.")
        if fake is not None:
            print(f"Meta request rule violations: {len(fake.violations)}")
    finally:
        ws.close()
