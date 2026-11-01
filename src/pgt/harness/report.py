"""What the harness measured, per role: calls, input, cached share, output, cost, failures, and
how often its context was trimmed against its budget. Read by `pgt usage`, the Harness view and
the budget sweep."""

from __future__ import annotations

from collections import Counter, defaultdict

from pgt.harness.budgets import TRIM_FLAG_SHARE, budget_for, table

ROLE_ORDER = ("analyst", "media_buyer", "growth_lead", "producer", "reviewer", "strategist")


def _share(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


def role_rows(ws, loop_id: str | None = None, scale: float | None = None) -> list[dict]:
    calls: dict[str, list] = defaultdict(list)
    for e in ws.events.list("model.call", loop_id=loop_id):
        calls[e.payload["role"]].append(e.payload)
    contexts: dict[str, list] = defaultdict(list)
    for e in ws.events.list("context.assembled", loop_id=loop_id):
        contexts[e.payload["role"]].append(e.payload)
    rows = []
    roles = sorted(set(calls) | set(contexts),
                   key=lambda r: ROLE_ORDER.index(r) if r in ROLE_ORDER else 99)
    for role in roles:
        made = calls[role]
        text = [c for c in made if c.get("context_hash")]  # image and SAM calls have none
        input_tokens = sum(c["input_tokens"] for c in made)
        cached = sum(c.get("cached_tokens", 0) for c in made)
        failures = Counter(c.get("failure") or ("provider" if not c.get("ok", True) else None)
                           for c in made)
        failures.pop(None, None)
        assembled = contexts[role]
        trimmed = [c for c in assembled if c.get("trimmed")]
        inputs = [c.get("sections", {}).get("inputs", 0) for c in assembled]
        budget = budget_for(role, scale)
        largest = max(inputs, default=0)
        share = _share(len(trimmed), len(assembled))
        rows.append({
            "role": role,
            "calls": len(made),
            "text_calls": len(text),
            "input_tokens": input_tokens,
            "cached_tokens": cached,
            "cached_share": _share(cached, input_tokens),
            "output_tokens": sum(c["output_tokens"] for c in made),
            "searches": sum(c.get("searches", 0) for c in made),
            "cost": round(sum(c["cost"] for c in made), 4),
            "failures": dict(failures),
            "failed": sum(failures.values()),
            "contexts": len(assembled),
            "trimmed": len(trimmed),
            "trimmed_share": share,
            "trimmed_sections": dict(Counter(n for c in trimmed for n in c["trimmed"])),
            "flag_larger_budget": share > TRIM_FLAG_SHARE,
            "largest_inputs": largest,
            "inputs_budget": budget.inputs,
            "largest_context": max((sum(c.get("sections", {}).values()) for c in assembled),
                                   default=0),
            "budget_total": budget.total,
        })
    return rows


def totals(rows: list[dict]) -> dict:
    input_tokens = sum(r["input_tokens"] for r in rows)
    cached = sum(r["cached_tokens"] for r in rows)
    return {
        "calls": sum(r["calls"] for r in rows),
        "input_tokens": input_tokens,
        "cached_tokens": cached,
        "cached_share": _share(cached, input_tokens),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cost": round(sum(r["cost"] for r in rows), 4),
        "failed": sum(r["failed"] for r in rows),
        "trimmed": sum(r["trimmed"] for r in rows),
    }


def harness_report(ws, loop_id: str | None = None, scale: float | None = None) -> dict:
    rows = role_rows(ws, loop_id, scale)
    contexts, requests = ws.requests.count()
    return {
        "roles": rows,
        "totals": totals(rows),
        "budgets": table(scale),
        "stored": {"contexts": contexts, "requests": requests},
        "trim_flag_share": TRIM_FLAG_SHARE,
    }


def render(report: dict) -> str:
    header = (f"{'Role':<12} {'Calls':>5} {'Input':>9} {'Cached':>7} {'Output':>8} {'Cost':>8} "
              f"{'Failed':>6} {'Trimmed':>8} {'Largest/budget':>15}")
    lines = [header]
    for r in report["roles"]:
        lines.append(
            f"{r['role']:<12} {r['calls']:>5} {r['input_tokens']:>9,} {r['cached_share']:>7.0%} "
            f"{r['output_tokens']:>8,} ${r['cost']:>7.4f} {r['failed']:>6} "
            f"{r['trimmed_share']:>8.0%} {r['largest_inputs']:>7,}/{r['inputs_budget']:<7,}"
            + ("  needs a larger budget" if r["flag_larger_budget"] else ""))
    t = report["totals"]
    lines.append(f"{'Total':<12} {t['calls']:>5} {t['input_tokens']:>9,} {t['cached_share']:>7.0%} "
                 f"{t['output_tokens']:>8,} ${t['cost']:>7.4f} {t['failed']:>6}")
    s = report["stored"]
    lines.append(f"Stored: {s['contexts']} contexts, {s['requests']} requests.")
    return "\n".join(lines)
