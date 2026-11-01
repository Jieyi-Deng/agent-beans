"""Read-only tools that roles may call. The harness checks the allowlist before each call."""

from __future__ import annotations

from pydantic_ai import Tool

from pgt.llm.runtime import Scope


def evidence_tool(scope: Scope) -> Tool:
    # Async so parallel calls run one at a time on the event loop: a sync tool would run in a
    # worker thread, and two at once would share the workspace's SQLite connection.
    async def get_evidence(evidence_id: str) -> dict:
        """Look up one evidence entry by ID: its numbers, inputs and note."""
        evidence = scope.ws.ledger.get(evidence_id)
        if evidence is None:
            return {"error": f"Unknown evidence ID {evidence_id}."}
        return evidence.model_dump(mode="json")

    return Tool(get_evidence, takes_ctx=False)


def recall_tools(scope: Scope, role: str) -> list[Tool]:
    """Bring back what the context left out: a frozen week record, or the caller's own journal.
    The journal tool is bound to the calling role in code; it cannot read another role's."""
    from pgt.harness import memory

    async def recall_week(week: int) -> dict:
        """The full frozen record of one past week: readouts, tests, changes, owner verdicts."""
        return memory.recall_week(scope.ws, week)

    async def recall_journal(subject: str = "", day: int = 0) -> list[dict]:
        """Your own earlier decisions and how they turned out, filtered by a subject ID (a
        campaign, ad or test) or a day. Leave both empty for the latest entries."""
        return memory.recall_journal(scope.ws, role, subject or None, day or None)

    return [Tool(recall_week, takes_ctx=False), Tool(recall_journal, takes_ctx=False)]


PROPOSABLE = {
    "target_cost_per_result": float, "weekly_budget": float, "test_budget_share": float,
    "brand_voice": str, "audience": str, "long_term_goal": str, "constraints": list,
}


def propose_fact_tool(scope: Scope, role: str) -> Tool:
    """Suggest a change to the owner's charter. Nothing changes until the owner accepts it."""
    from pgt.harness import safeguards
    from pgt.harness.protocol import propose_fact as log_proposal
    from pgt.schemas import SharedFacts

    async def propose_fact(key: str, value: str, reason: str, evidence_ids: list[str]) -> dict:
        """Propose a change to one shared fact. Only the owner can apply it. key is one of
        target_cost_per_result, weekly_budget, test_budget_share, brand_voice, audience,
        long_term_goal or constraints. value is the new value as text: a number for numbers,
        one new constraint for constraints. Cite the evidence IDs behind it."""
        ws = scope.ws
        if key not in PROPOSABLE:
            return {"error": f"{key} cannot be proposed. Use one of {', '.join(PROPOSABLE)}."}
        if not reason.strip() or not evidence_ids:
            return {"error": "Give a reason and the evidence IDs behind it."}
        unknown = [i for i in evidence_ids if safeguards.source_problem(ws, i, role)]
        if unknown:
            return {"error": f"Unknown evidence: {', '.join(unknown)}."}
        current = ws.facts.current()
        kind = PROPOSABLE[key]
        try:
            parsed = float(str(value).strip().lstrip("$").rstrip("%")) if kind is float \
                else [*current.constraints, value.strip()] if kind is list else value.strip()
            if kind is float and str(value).strip().endswith("%"):
                parsed /= 100
            SharedFacts.model_validate({**current.model_dump(), key: parsed})
        except (ValueError, TypeError) as exc:
            return {"error": f"{value!r} is not a valid {key}: {exc}"}
        resolved = {e.payload["proposal_id"] for e in ws.events.list("fact.proposal_resolved")}
        if any(e.payload.get("key") == key and e.id not in resolved
               for e in ws.events.list("fact.proposed")):
            return {"error": f"A change to {key} is already waiting on the owner."}
        event_id = log_proposal(scope, role, key, parsed, reason.strip(), evidence_ids,
                                was=getattr(current, key))
        return {"ok": True, "proposal": f"e-{event_id}",
                "note": "The owner decides. Carry on with the current facts."}

    return Tool(propose_fact, takes_ctx=False)
