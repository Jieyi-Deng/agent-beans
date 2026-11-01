"""Budget sweep: the same sandbox week, live, with every role's may-trim budgets at 0.5x, 1x and 2x.

    uv run python -m bench.sweep --scale 0.5 --out runs/sweep        # one live run (costs money)
    uv run python -m bench.sweep --report runs/sweep                 # table from the runs

Each run is week 1 of the Northpaw Supply sandbox (seed 7): seven daily checks, the weekly review
with research, test plan and creative, the simulated owner's approvals, and the week close. Only
the budget scale differs (PGT_BUDGET_SCALE). Quality is scored on what the harness already
measures: first-try pass rate, redos, evidence-check redos, gate blocks, Reviewer issues on
creative and escalations. Keys come from .env and are never printed. Each run has its own spend
cap, checked before every paid call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

SCALES = (0.5, 1.0, 2.0)
TEMPLATE = "ecommerce"
SEED = 7
EVIDENCE_SUBJECTS = {"findings", "readout", "weekly_findings"}


def db_path(out: Path, scale: float) -> Path:
    return out / f"scale-{scale:g}.db"


async def one_week(team, seed: int = SEED) -> str:
    """Week 1 as the demo history runs it, then the week close. Returns the stage reached."""
    from pgt.api.app import _simulated_grant
    from pgt.harness import access
    from pgt.loops.close import close_week
    from pgt.loops.simulate import simulate
    from pgt.loops.weekly import apply_week, plan_week, produce_week
    from pgt.sandbox import TEMPLATES

    template = TEMPLATES[team.template]
    access.connect(team.ws, dict(access.SANDBOX, name=template.sandbox_name), access.APPLY,
                   sim_day=0)
    sim = await simulate(7, team.model, team.ws, seed=seed, meter=team.meter,
                         template=team.template)
    account = team.account = sim.account
    summary = await plan_week(team.ws, account, team.model, meter=team.meter)
    if summary.stage != "awaiting_plan_approval":
        return summary.stage
    _simulated_grant(team.ws, summary.plan_id, account.day)
    summary = await produce_week(team.ws, account, team.model, team.media, team.media_dir,
                                 meter=team.meter)
    if summary.stage == "awaiting_change_approval":
        _simulated_grant(team.ws, f"week{summary.week}/change_set", account.day)
        apply_week(team.ws, account)
    await close_week(team.ws, team.model, summary.week, meter=team.meter)
    if summary.stage == "awaiting_change_approval":
        return "week closed"
    return f"week closed after {summary.stage}"


def score(ws, scale: float, stage: str, wall_s: float) -> dict:
    """What the harness measured in one run."""
    from pgt.harness import memory as mem
    from pgt.harness import safeguards as sg
    from pgt.harness.report import harness_report

    mem.record_outcomes(ws)
    report = harness_report(ws, scale=scale)
    redos = [e.payload for e in ws.events.list("review.redo")]
    creative = [r for r in redos if "-c" in str(r.get("subject", ""))]
    trimmed = Counter(s["name"] for e in ws.events.list("context.trimmed")
                      for s in e.payload["sections"])
    quality = {role: sg.quality(mem.journal(ws, role, reader="system")) for role in mem.ROLES}
    firsts = [q["parts"]["first_try"] for q in quality.values() if "first_try" in q["parts"]]
    scores = [q["score"] for q in quality.values() if q["score"] is not None]
    t = report["totals"]
    return {
        "scale": scale, "stage": stage, "wall_s": round(wall_s),
        "calls": t["calls"], "input_tokens": t["input_tokens"],
        "cached_share": t["cached_share"], "output_tokens": t["output_tokens"],
        "cost": round(t["cost"], 4), "failed": t["failed"],
        "trimmed_contexts": t["trimmed"], "trimmed_sections": dict(trimmed),
        "first_try": round(sum(firsts) / len(firsts), 3) if firsts else None,
        "quality": round(sum(scores) / len(scores), 3) if scores else None,
        "redos": len(redos),
        "evidence_redos": sum(r.get("subject") in EVIDENCE_SUBJECTS for r in redos),
        "gate_blocks": len(ws.events.list("policy.blocked")),
        "creative_issues": sum(len(r.get("issues", [])) for r in creative),
        "escalations": len(ws.events.list("escalation.raised")),
        "roles": report["roles"],
    }


def run(scale: float, out: Path, cap: float) -> dict:
    from pgt.api.app import live_team

    os.environ["PGT_BUDGET_SCALE"] = str(scale)
    out.mkdir(parents=True, exist_ok=True)
    db = db_path(out, scale)
    if db.exists():
        raise SystemExit(f"{db} exists; pick a new --out")
    team = live_team(str(db), str(out / f"media-{scale:g}"), cap=cap)
    team.template = TEMPLATE
    start = time.monotonic()
    try:
        stage = asyncio.run(one_week(team))
    except Exception as exc:  # noqa: BLE001 - the run is still scored from what it logged
        stage = f"stopped: {type(exc).__name__}: {str(exc)[:160]}"
    result = score(team.ws, scale, stage, time.monotonic() - start)
    (out / f"scale-{scale:g}.json").write_text(json.dumps(result, indent=1))
    team.ws.close()
    return result


def markdown(results: list[dict]) -> list[str]:
    rows = sorted(results, key=lambda r: r["scale"])
    head = ["| | " + " | ".join(f"{r['scale']:g}x" for r in rows) + " |",
            "| --- |" + " --- |" * len(rows)]

    def line(label: str, fmt) -> str:
        return f"| {label} | " + " | ".join(fmt(r) for r in rows) + " |"

    def pct(v) -> str:
        return "n/a" if v is None else f"{v:.0%}"

    return head + [
        line("Stage reached", lambda r: r["stage"].replace("_", " ")),
        line("Contexts trimmed", lambda r: str(r["trimmed_contexts"])),
        line("Sections trimmed", lambda r: ", ".join(f"{k} {v}" for k, v in
                                                     sorted(r["trimmed_sections"].items()))
             or "none"),
        line("First-try pass rate", lambda r: pct(r["first_try"])),
        line("Quality score (harness)", lambda r: "n/a" if r["quality"] is None
             else f"{r['quality']:.2f}"),
        line("Redos", lambda r: str(r["redos"])),
        line("Evidence-check redos", lambda r: str(r["evidence_redos"])),
        line("Gate blocks", lambda r: str(r["gate_blocks"])),
        line("Reviewer issues on creative", lambda r: str(r["creative_issues"])),
        line("Escalations", lambda r: str(r["escalations"])),
        line("Model calls", lambda r: str(r["calls"])),
        line("Input tokens", lambda r: f"{r['input_tokens']:,}"),
        line("Cached share", lambda r: pct(r["cached_share"])),
        line("Cost", lambda r: f"${r['cost']:.2f}"),
        line("Wall time", lambda r: f"{r['wall_s'] // 60} min"),
    ]


def report(out: Path) -> list[dict]:
    results = [json.loads(p.read_text()) for p in sorted(out.glob("scale-*.json"))]
    print("\n".join(markdown(results)))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", type=float, choices=SCALES)
    parser.add_argument("--out", default="runs/sweep")
    parser.add_argument("--cap", type=float, default=5.0, help="US dollars for this run")
    parser.add_argument("--report", metavar="DIR", help="Print the table from finished runs")
    args = parser.parse_args()
    if args.report:
        report(Path(args.report))
        return
    if args.scale is None:
        parser.error("give --scale or --report")
    result = run(args.scale, Path(args.out), args.cap)
    print(json.dumps({k: v for k, v in result.items() if k != "roles"}, indent=1))


if __name__ == "__main__":
    main()
