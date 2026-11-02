"""Stage 2: run arms on the live Meta API, with a hard spend cap. Needs approval per run.

    uv run python -m bench.live --env-file PATH            # pgt, single-agent, agents-sdk on s1, s5, s7
    uv run python -m bench.live --env-file PATH --arms pgt --scenarios s7 --seeds 7

The world is still built offline (7 simulated days with the plain offline model); only the
arm's own calls go to the API. Every request passes the same Meta rule check as offline, and
once the estimated spend reaches the cap, further requests are refused and the run stops.
Writes bench/out/live.json and docs/LIVE_RESULTS.md. The key is read from the env file into
the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from openai import AsyncOpenAI

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from bench.grader import Grade, call_costs, grade, verdict
from bench.run import ARMS, ROOT, SEEDS
from bench.scenarios import BUDGET_CAMPAIGN, BY_KEY, Scenario
from bench.world import ScriptedUser, WeekResult, build_world
from pgt.llm.fake import check_meta_request, form_fields
from pgt.llm.meta import DEFAULT_MODEL, META_BASE_URL, responses_model

LIVE_ARMS = ["pgt", "single-agent", "agents-sdk"]
# s2, s3 and s6 plant their mistake through the offline model or the harness, so live they
# would repeat s1. s5 and s7 plant theirs in the world, which the live model really sees.
LIVE_SCENARIOS = ["s1", "s5", "s7"]
DEFAULT_CAP = 5.0


class SpendCapReached(RuntimeError):
    pass


@dataclass
class Spend:
    cap: float
    spent: float = 0.0


@dataclass
class LiveMeta:
    """The same face as MetaFake (client(), calls, violations), backed by the live API."""

    api_key: str
    spend: Spend
    calls: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    inner: httpx.AsyncBaseTransport | None = None  # tests pass a mock; live runs use the network

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url=os.environ.get("META_MODEL_BASE_URL", META_BASE_URL),
            api_key=self.api_key,
            max_retries=2,
            timeout=180.0,
            http_client=httpx.AsyncClient(transport=_Recorder(self, self.inner), timeout=180.0),
        )


class _Recorder(httpx.AsyncBaseTransport):
    """Checks the cap and the Meta rules before each request, and records usage after it."""

    def __init__(self, live: LiveMeta, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.live = live
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        live = self.live
        if live.spend.spent >= live.spend.cap:
            raise SpendCapReached(f"Spend cap ${live.spend.cap:g} reached.")
        path = request.url.path
        if path.endswith("/images/edits"):
            body = {k: v for k, v in form_fields(request).items() if k != "image"}
        else:
            body = json.loads(request.content or b"{}")
        if path.endswith("/responses"):
            live.violations.extend(check_meta_request(body))
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        await response.aclose()
        if response.status_code == 200:
            call = _usage(body, path, json.loads(content))
            if call:
                live.calls.append(call)
                live.spend.spent += call_costs([call])[2]
        headers = [
            (k, v)
            for k, v in response.headers.items()
            if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            response.status_code, headers=headers, content=content, request=request
        )


def _usage(body: dict, path: str, data: dict) -> dict | None:
    if path.endswith(("/images/generations", "/images/edits")):
        return {"model": body.get("model"), "input_tokens": 0, "output_tokens": 0, "searches": 0}
    if not path.endswith("/responses"):
        return None
    usage = data.get("usage") or {}
    output = data.get("output") or []
    return {
        "model": body.get("model"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "searches": sum(1 for o in output if o.get("type") == "web_search_call"),
    }


async def run_live_one(
    arm: str, scenario: Scenario, seed: int, media_root: Path, api_key: str, spend: Spend
) -> Grade:
    world = await build_world(
        seed,
        scenario.plant_injection,
        media_root / arm / scenario.key,
        raise_yesterday=BUDGET_CAMPAIGN if scenario.budget_jump else None,
    )
    live = LiveMeta(api_key, spend)
    model = responses_model(live.client(), DEFAULT_MODEL)
    user = ScriptedUser()
    started = time.perf_counter()
    try:
        result = await ARMS[arm](arm, world, model, live, user, scenario)
        error = None
    except SpendCapReached:
        raise
    # A live run can stop where an offline one cannot (an API error, a product spend cap).
    except Exception as exc:  # noqa: BLE001
        result, error = WeekResult(stage="error"), f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    per_call = ", ".join(f"{call_costs([c])[2]:.3f}" for c in live.calls)
    print(f"  {arm} {scenario.key} seed {seed} call costs: {per_call}", flush=True)
    graded = grade(arm, scenario, seed, world, result, user, live.calls, elapsed, planted=False)
    if not graded.passed and result.note:
        graded.why += f" ({result.note})"
    if error:
        graded.passed, graded.completed, graded.why = False, False, "stopped: " + error
    if live.violations:
        graded.passed, graded.why = False, "broke Meta API rules: " + "; ".join(live.violations)
    graded.proposed = [_change_text(c) for _, a in user.approvals for c in a.get("changes", [])]
    graded.proposed += [b for _, a in user.approvals for b in a.get("blocked", [])]
    # Saved so a result can be checked later: the live sandbox is a temporary folder.
    graded.writes = world.writes()
    return graded


def _change_text(change: dict) -> str:
    """One proposed change, in a few words, for the results table."""
    text = f"{change.get('kind')} {change.get('target_id')}"
    if change.get("new_daily_budget") is not None:
        text += f" to ${change['new_daily_budget']:g}"
    return text


async def run_live(
    arms: list[str],
    scenarios: list[Scenario],
    seeds: list[int],
    api_key: str,
    spend: Spend,
    done: set[str] = frozenset(),
) -> tuple[list[Grade], list[str]]:
    """Run each arm, scenario and seed once, skipping the runs named in `done`."""
    grades, skipped = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for scenario in scenarios:
            for seed in seeds:
                for arm in arms:
                    name = f"{arm} {scenario.key} seed {seed}"
                    if name in done:
                        continue
                    if spend.spent >= spend.cap:
                        skipped.append(name)
                        continue
                    try:
                        g = await run_live_one(
                            arm, scenario, seed, Path(tmp) / str(seed), api_key, spend
                        )
                    except SpendCapReached:
                        skipped.append(f"{name} (stopped mid-run)")
                        continue
                    if _cap_stopped(g):  # an arm that catches its own errors caught the cap
                        skipped.append(f"{name} (stopped mid-run)")
                        continue
                    grades.append(g)
                    print(
                        f"{name}: {'pass' if g.passed else 'FAIL'} {g.stage} "
                        f"${g.cost:.4f} {g.wall_time_s:.0f}s (total ${spend.spent:.3f}) {g.why}",
                        flush=True,
                    )
    return grades, skipped


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def markdown(
    grades: list[Grade], skipped: list[str], spend: Spend, notes: list[str] | None = None
) -> str:
    arms = list(dict.fromkeys(g.arm for g in grades))
    keys = list(dict.fromkeys(g.scenario for g in grades))
    lines = [
        "# Bench results (live)",
        "",
        "Generated by `uv run python -m bench.live`. Do not edit by hand.",
        "",
        (
            f"Model: `{DEFAULT_MODEL}` on the Meta API. Seeds: "
            f"{', '.join(map(str, sorted({g.seed for g in grades})))}. The model makes its own "
            "choices here: nothing is planted in its replies, so a pass means either the model "
            "did not make the mistake or the harness stopped it. Costs are list-price estimates "
            f"from the API's token counts. Total spend: ${spend.spent:.2f} of a ${spend.cap:g} cap. "
            "The cap is checked before each request, so the request in flight when it is reached "
            "can take the total slightly past it."
        ),
        "",
        "## Pass rate",
        "",
        "| Arm | " + " | ".join(keys) + " | All |",
        "|---|" + "---|" * (len(keys) + 1),
    ]
    for arm in arms:
        row, total, ok = [], 0, 0
        for key in keys:
            cell = [g for g in grades if g.arm == arm and g.scenario == key]
            passed = sum(g.passed for g in cell)
            total, ok = total + len(cell), ok + passed
            row.append(f"{passed}/{len(cell)}")
        lines.append(f"| {arm} | " + " | ".join(row) + f" | {ok}/{total} |")
    lines += [
        "",
        "## Cost and behavior per run",
        "",
        "Means per arm and scenario.",
        "",
        (
            "| Arm | Scenario | Model calls | Input tokens | Output tokens | Est. cost (USD) | "
            "Wall time (s) | Redos | Blocked tool calls | Blocked changes shown | "
            "House-rule breaks |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key in keys:
        for arm in arms:
            cell = [g for g in grades if g.arm == arm and g.scenario == key]
            if not cell:
                continue
            lines.append(
                f"| {arm} | {key} | {_mean([g.calls for g in cell]):.1f} "
                f"| {_mean([g.input_tokens for g in cell]):,.0f} "
                f"| {_mean([g.output_tokens for g in cell]):,.0f} "
                f"| {_mean([g.cost for g in cell]):.4f} "
                f"| {_mean([g.wall_time_s for g in cell]):.1f} "
                f"| {_mean([g.redos for g in cell]):.1f} "
                f"| {_mean([g.blocked_tool_calls for g in cell]):.1f} "
                f"| {_mean([g.blocked_shown for g in cell]):.1f} "
                f"| {_mean([len(g.house_rule_breaks) for g in cell]):.1f} |"
            )
    daily = [g for g in grades if BY_KEY[g.scenario].daily_check]
    if daily:
        lines += [
            "",
            "## What each daily check proposed",
            "",
            "The changes the user was shown, and for Agent Beans what the gate blocked and why.",
            "",
            "| Arm | Scenario | Seed | Proposed |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {g.arm} | {g.scenario} | {g.seed} | "
            + ("; ".join(g.proposed) or "No changes").replace("|", "/")
            + " |"
            for g in daily
        ]
    failures = [g for g in grades if not g.passed]
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("None.")
    else:
        lines += ["| Arm | Scenario | Seed | Why |", "|---|---|---|---|"]
        lines += [
            f"| {g.arm} | {g.scenario} | {g.seed} | {g.why.replace('|', '/')} |" for g in failures
        ]
    if skipped:
        lines += ["", "## Not run (spend cap reached)", "", *[f"- {name}" for name in skipped]]
    if notes:
        lines += ["", "## Notes", "", *[f"- {note}" for note in notes]]
    return "\n".join(lines) + "\n"


def _cap_stopped(g: Grade) -> bool:
    return "SpendCapReached" in g.stage or "SpendCapReached" in g.why


def _run_name(g: Grade) -> str:
    return f"{g.arm} {g.scenario} seed {g.seed}"


def append(saved: dict, grades: list[Grade], skipped: list[str], spend: Spend) -> dict:
    """Add a new live run to saved results: its grades join the old ones, the runs it made
    leave the old skipped list, and its cap and spend add to the totals."""
    ran = {_run_name(g) for g in grades}
    old_skipped = [n for n in saved.get("skipped", []) if n.split(" (")[0] not in ran]
    return {
        **saved,
        "cap": round(saved["cap"] + spend.cap, 6),
        "spent": round(saved["spent"] + spend.spent, 6),
        "skipped": [n for n in old_skipped if n not in skipped] + skipped,
        "grades": saved["grades"] + [g.as_dict() for g in grades],
    }


def regrade(saved: dict) -> tuple[list[Grade], list[str]]:
    """Apply the current pass rules to saved live runs, without calling the API again.
    Returns the grades and the runs the spend cap stopped."""
    grades, skipped = [], list(saved.get("skipped", []))
    for d in saved["grades"]:
        g = Grade(**d)
        if _cap_stopped(g):
            skipped.insert(0, f"{g.arm} {g.scenario} seed {g.seed} (stopped mid-run)")
            continue
        if not g.why.startswith(("stopped:", "broke Meta API rules")):
            passed, why = verdict(g.scenario, g, planted=False)
            g.passed, g.why = passed, ("" if passed else (g.why or why))
        grades.append(g)
    return grades, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--arms", nargs="+", default=LIVE_ARMS, choices=list(ARMS))
    parser.add_argument("--scenarios", nargs="+", default=LIVE_SCENARIOS, choices=list(BY_KEY))
    parser.add_argument("--seeds", nargs="+", type=int, default=SEEDS)
    parser.add_argument("--cap", type=float, default=DEFAULT_CAP, help="USD, for the whole run")
    parser.add_argument("--out", type=Path, default=ROOT / "bench" / "out" / "live.json")
    parser.add_argument("--doc", type=Path, default=ROOT / "docs" / "LIVE_RESULTS.md")
    parser.add_argument(
        "--regrade", action="store_true", help="re-grade --out and rewrite --doc; no API calls"
    )
    parser.add_argument(
        "--append", action="store_true", help="add to --out, skipping runs it already graded"
    )
    args = parser.parse_args()
    if args.regrade:
        saved = json.loads(args.out.read_text())
        (grades, skipped), spend = regrade(saved), Spend(saved["cap"], saved["spent"])
        args.out.write_text(
            json.dumps(
                {**saved, "skipped": skipped, "grades": [g.as_dict() for g in grades]}, indent=2
            )
            + "\n"
        )
        args.doc.write_text(markdown(grades, skipped, spend, saved.get("notes")))
        print(args.doc.read_text())
        return

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        parser.error("MODEL_API_KEY is required for an approved live run")
    spend = Spend(cap=args.cap)
    scenarios = [BY_KEY[k] for k in args.scenarios]
    old = json.loads(args.out.read_text()) if args.append else None
    done = {_run_name(Grade(**d)) for d in old["grades"]} if old else set()
    grades, skipped = asyncio.run(run_live(args.arms, scenarios, args.seeds, api_key, spend, done))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    saved = {
        "cap": spend.cap,
        "spent": spend.spent,
        "skipped": skipped,
        "grades": [g.as_dict() for g in grades],
    }
    if old:
        saved = append(old, grades, skipped, spend)
        grades, spend = [Grade(**d) for d in saved["grades"]], Spend(saved["cap"], saved["spent"])
        skipped = saved["skipped"]
    args.out.write_text(json.dumps(saved, indent=2) + "\n")
    report = markdown(grades, skipped, spend, saved.get("notes"))
    args.doc.write_text(report)
    print(report)


if __name__ == "__main__":
    main()
