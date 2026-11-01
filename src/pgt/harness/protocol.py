"""How roles talk to each other: handoffs, ask/answer, escalation, fact proposals.

Every message is an event, so the Team Room can show it and a run can be replayed from the log.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic_ai import Tool

from pgt.harness.creative_checks import check_answer
from pgt.llm.runtime import Scope
from pgt.schemas import Answer

MAX_QUESTIONS_PER_TASK = 3
DISPATCHERS = {"growth_lead", "system"}


def handoff(scope: Scope, sender: str, to: str, task: str, refs: list[str] = ()) -> None:
    """Only the Growth Lead (or the loop itself) assigns work to another role."""
    if sender not in DISPATCHERS:
        raise PermissionError(f"Role {sender} cannot assign work; only the Growth Lead can.")
    scope.ws.events.append(
        "task.handoff",
        sender,
        {"from": sender, "to": to, "task": task},
        refs=list(refs),
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )


def escalate(scope: Scope, role: str, reason: str, refs: list[str] = ()) -> str:
    """Put a problem in the user's inbox. Returns the reason for the caller's summary."""
    scope.ws.events.append(
        "escalation.raised",
        role,
        {"role": role, "reason": reason},
        refs=list(refs),
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )
    return reason


def propose_fact(scope: Scope, role: str, key: str, value, reason: str,
                 evidence_ids: list[str] = (), was=None) -> int:
    """Roles may suggest a shared-fact change; only the user can apply it. Returns the event ID."""
    event = scope.ws.events.append(
        "fact.proposed",
        role,
        {"role": role, "key": key, "value": value, "reason": reason,
         "evidence_ids": list(evidence_ids), "was": was},
        refs=[i for i in evidence_ids if scope.ws.ledger.has(i)],
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )
    return event.id


Answerer = Callable[[str, list[str]], Awaitable[Answer]]


def ask_tool(scope: Scope, asker: str, to: str, task_id: str, answer: Answerer) -> Tool:
    """A tool that lets `asker` put one question to `to`. One hop only: the answering role gets
    no ask tool of its own. At most MAX_QUESTIONS_PER_TASK per task. Answers are checked in code
    (cited evidence exists and every number matches it), with one retry."""
    events = scope.ws.events

    async def ask(question: str) -> dict:
        asked = [
            e
            for e in events.list("question.asked", loop_id=scope.loop_id)
            if e.payload["task_id"] == task_id
        ]
        if len(asked) >= MAX_QUESTIONS_PER_TASK:
            events.append(
                "question.refused",
                "system",
                {
                    "task_id": task_id,
                    "asker": asker,
                    "question": question,
                    "reason": f"Limit of {MAX_QUESTIONS_PER_TASK} questions per task reached.",
                },
                sim_day=scope.sim_day,
                loop_id=scope.loop_id,
            )
            return {
                "error": f"Question limit reached ({MAX_QUESTIONS_PER_TASK} per task). "
                "Work with what you have."
            }
        event = events.append(
            "question.asked",
            asker,
            {"task_id": task_id, "asker": asker, "to": to, "question": question},
            sim_day=scope.sim_day,
            loop_id=scope.loop_id,
        )
        question_id = f"q-{event.id}"
        issues: list[str] = []
        reply = None
        for _ in range(2):
            reply = await answer(question, issues)
            issues = check_answer(reply, scope.ws.ledger)
            if not issues:
                break
        events.append(
            "question.answered",
            to,
            {
                "task_id": task_id,
                "question_id": question_id,
                "answer": reply.answer,
                "evidence_ids": reply.evidence_ids,
                "issues": issues,
            },
            refs=[i for i in reply.evidence_ids if scope.ws.ledger.has(i)],
            sim_day=scope.sim_day,
            loop_id=scope.loop_id,
        )
        if issues:
            return {
                "question_id": question_id,
                "error": "The answer failed its checks; do not rely on it.",
                "issues": issues,
            }
        return {
            "question_id": question_id,
            "answer": reply.answer,
            "evidence_ids": reply.evidence_ids,
        }

    return Tool(
        ask,
        name=f"ask_{to}",
        takes_ctx=False,
        description=f"Ask the {to.replace('_', ' ')} one question about the account "
        "data. The answer cites evidence IDs you may use in claims.",
    )
