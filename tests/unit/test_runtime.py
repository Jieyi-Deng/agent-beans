"""The role runtime: tool policy inside the agent loop, review redo, spend caps, context hashes."""

import asyncio

import pytest

from pgt.harness.context import assemble
from pgt.harness.policy import CostMeter, SpendCapExceeded
from pgt.harness.workspace import Workspace
from pgt.llm.fake import FunctionCall, MetaFake, check_meta_request, prompt_inputs
from pgt.llm.meta import estimate_cost, responses_model
from pgt.llm.offline import offline_responder
from pgt.llm.runtime import Scope, run_role
from pgt.loops.daily import run_daily_check
from pgt.sandbox import SandboxAccount, fitness_app_facts, fitness_app_scenario
from pgt.schemas import Finding, Findings


def pack(data=None, role="analyst"):
    return assemble(
        role,
        "You are a test role.",
        fitness_app_facts(),
        1,
        task="Do the task.",
        done_when="Done.",
        data=data or {"x": 1},
        inputs=["a@v1"],
    )


def test_context_hash_is_stable_and_sensitive():
    assert pack().context_hash == pack().context_hash
    assert pack({"x": 2}).context_hash != pack().context_hash
    p = pack()
    assert p.instructions.index("Team rules") < p.instructions.index("Shared facts")
    assert prompt_inputs({"input": p.prompt}) == {"x": 1}


def test_blocked_tool_never_runs():
    ran = []

    def update_budget(campaign_id: str, daily_budget: float) -> str:
        """Change a campaign budget."""
        ran.append(campaign_id)
        return "ok"

    def responder(body):
        if not any(i.get("type") == "function_call_output" for i in body["input"]):
            return FunctionCall("update_budget", {"campaign_id": "cmp_1", "daily_budget": 999})
        return Findings(findings=[])

    fake = MetaFake(responder)
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    out = asyncio.run(
        run_role(
            "analyst", Findings, pack(), scope, responses_model(fake.client()), [update_budget]
        )
    )
    assert out == Findings(findings=[])
    assert ran == []
    (blocked,) = ws.events.list("tool.blocked")
    assert blocked.payload == {"role": "analyst", "tool": "update_budget"}
    outputs = [i for i in fake.requests[1]["input"] if i.get("type") == "function_call_output"]
    assert "not allowed" in outputs[0]["output"]
    assert len(ws.events.list("model.call")) == 2 and fake.violations == []


def test_repeated_identical_tool_call_is_cut_short():
    ran = []

    def get_evidence(evidence_id: str) -> str:
        """Read one evidence row."""
        ran.append(evidence_id)
        return "row"

    def responder(body):
        outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
        if any("Hand in your final answer" in o["output"] for o in outputs):
            return Findings(findings=[])
        return FunctionCall("get_evidence", {"evidence_id": "ev-1"})

    fake = MetaFake(responder)
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    out = asyncio.run(
        run_role("analyst", Findings, pack(), scope, responses_model(fake.client()), [get_evidence])
    )
    assert out == Findings(findings=[])
    assert ran == ["ev-1", "ev-1"]
    assert len(ws.events.list("model.call")) == 4


def test_a_model_that_ignores_the_repeat_warning_loses_its_tools():
    ran = []

    def get_evidence(evidence_id: str) -> str:
        """Read one evidence row."""
        ran.append(evidence_id)
        return "row"

    def responder(body):
        if not body.get("tools"):
            return Findings(findings=[])
        return FunctionCall("get_evidence", {"evidence_id": "ev-1"})  # never takes the hint

    fake = MetaFake(responder)
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    out = asyncio.run(
        run_role("analyst", Findings, pack(), scope, responses_model(fake.client()), [get_evidence])
    )
    assert out == Findings(findings=[])
    assert ran == ["ev-1", "ev-1"]
    assert len(ws.events.list("model.call")) == 4
    assert fake.violations == []


def test_call_to_a_tool_the_role_was_never_given_is_logged():
    # A prompt injection asks for a write tool the role does not have: the attempt is logged
    # once, nothing runs, and the role still finishes.
    def responder(body):
        if not any(i.get("type") == "function_call_output" for i in body["input"]):
            return FunctionCall("update_budget", {"campaign_id": "cmp_3", "daily_budget": 10000})
        return Findings(findings=[])

    fake = MetaFake(responder)
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    out = asyncio.run(run_role("analyst", Findings, pack(), scope, responses_model(fake.client())))
    assert out == Findings(findings=[])
    (blocked,) = ws.events.list("tool.blocked")
    assert blocked.payload == {"role": "analyst", "tool": "update_budget"}
    assert blocked.loop_id == "loop-1"
    assert ws.events.list("tool.call") == [] and fake.violations == []


def test_spend_cap_stops_the_role():
    fake = MetaFake(lambda body: Findings(findings=[]))
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events, per_call=1e-9), "loop-1", 1)
    with pytest.raises(SpendCapExceeded):
        asyncio.run(run_role("analyst", Findings, pack(), scope, responses_model(fake.client())))
    assert ws.events.list("budget.exceeded")


def test_reviewer_sends_bad_numbers_back_once():
    attempts = []

    def responder(body):
        out = offline_responder(body)
        if isinstance(out, Findings) and out.findings and not attempts:
            attempts.append(1)
            bad = out.findings[0].model_copy(update={"value": 123.0})
            return Findings(findings=[bad, *out.findings[1:]])
        return out

    ws = Workspace()
    ws.facts.set(fitness_app_facts(), "user")
    account = SandboxAccount(fitness_app_scenario())
    for _ in range(3):
        account.advance_day()
    fake = MetaFake(responder)
    brief = asyncio.run(run_daily_check(ws, account, responses_model(fake.client())))
    assert attempts == [1]
    (redo,) = ws.events.list("review.redo")
    assert "matches no number" in redo.payload["issues"][0]
    assert brief.review.verdict == "pass"
    assert ws.artifacts.latest("day3/findings").version == 1


def test_reviewer_blocks_when_the_redo_still_fails():
    def responder(body):
        out = offline_responder(body)
        if isinstance(out, Findings):
            return Findings(
                findings=[
                    Finding(
                        subject_id="ad_1",
                        claim="Made up.",
                        metric="ctr",
                        value=0.5,
                        evidence_ids=["ev-nope"],
                        severity="act",
                    )
                ]
            )
        return out

    ws = Workspace()
    ws.facts.set(fitness_app_facts(), "user")
    account = SandboxAccount(fitness_app_scenario())
    account.advance_day()
    brief = asyncio.run(run_daily_check(ws, account, responses_model(MetaFake(responder).client())))
    assert brief.review.verdict == "block" and brief.change_set_id is None
    assert ws.artifacts.latest("day1/change_set") is None
    assert any("unknown evidence ev-nope" in i for i in brief.review.issues)


def test_meta_request_checker_catches_bad_requests():
    body = {
        "model": "gpt-4o",
        "tool_choice": "required",
        "text": {
            "format": {
                "type": "json_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": [],
                },
            }
        },
    }
    problems = check_meta_request(body)
    assert "tool_choice must be auto" in problems
    assert "Unsupported Meta model" in problems
    assert "Every object needs additionalProperties: false" in problems
    assert "Every object property must be required" in problems


def test_estimate_cost():
    assert estimate_cost("muse-spark-1.3", 1_000_000, 0) == pytest.approx(1.25)
    assert estimate_cost("muse-spark-1.3-contributor", 0, 1_000_000, 2) == pytest.approx(4.255)


def test_used_up_cap_stops_a_loop_before_any_model_call():
    from pgt.llm.offline import offline_model
    from pgt.loops.simulate import simulate

    ws = Workspace()
    model, _ = offline_model("muse-spark-1.3")
    with pytest.raises(SpendCapExceeded):
        asyncio.run(simulate(1, model, ws, meter=CostMeter(ws.events, total=0.0)))
    # The refused call is logged as a spend_cap failure; nothing reached the model.
    calls = ws.events.list("model.call")
    assert [c.payload["failure"] for c in calls] == ["spend_cap"]
    assert calls[0].payload["input_tokens"] == 0 and ws.events.list("model.cost") == []
    assert ws.events.list("budget.exceeded")[0].payload["cap"] == "total"


def test_text_before_a_web_search_is_not_judged_as_the_answer():
    """Seen live: the Strategist wrote a line, searched, then answered. The answer was fine."""
    from pydantic_ai.messages import ModelResponse, NativeToolCallPart, TextPart

    from pgt.llm.runtime import schema_failure

    answer = Findings(findings=[]).model_dump_json()
    search = NativeToolCallPart("web_search", {"query": "dog harness"})
    searched = ModelResponse(parts=[TextPart("Looking at competitors."), search, TextPart(answer)])
    assert not schema_failure(searched, Findings, set())
    assert schema_failure(ModelResponse(parts=[TextPart("Looking at competitors.")]),
                          Findings, set())
