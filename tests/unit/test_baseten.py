"""Baseten provider, offline: the Chat Completions requests it sends and the per-role router."""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from pgt.harness.context import assemble
from pgt.harness.policy import CostMeter
from pgt.harness.workspace import Workspace
from pgt.llm.baseten import BASETEN_MODEL, MAX_OUTPUT, baseten_client, chat_model
from pgt.llm.fake import MetaFake
from pgt.llm.meta import estimate_cost, responses_model
from pgt.llm.repair import close_cut_json, tool_calls_in_text
from pgt.llm.routing import RoleRouter, mixed, resolve
from pgt.llm.runtime import Scope, run_role
from pgt.roles.tools import evidence_tool
from pgt.sandbox import fitness_app_facts
from pgt.schemas import Evidence, Findings


def pack(role="analyst"):
    return assemble(
        role,
        "You are a test role.",
        fitness_app_facts(),
        1,
        task="Do the task.",
        done_when="Done.",
        data={"x": 1},
        inputs=["a@v1"],
    )


def completion(message: dict, finish: str, out_tokens: int = 40) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": BASETEN_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": out_tokens,
            "total_tokens": 100 + out_tokens,
            "completion_tokens_details": {"reasoning_tokens": out_tokens - 10},
        },
    }


class BasetenFake:
    """Answers Chat Completions like Baseten: one tool call first if tools are offered, then JSON."""

    def __init__(self, text_answer: str | None = None, lookups: list[dict] | None = None,
                 json_answer: str | None = None, finish: str = "stop") -> None:
        self.bodies: list[dict] = []
        self.text_answer = text_answer  # answer in plain text instead of final_result
        self.json_answer, self.finish = json_answer, finish  # the answer when no tools are sent
        self.lookups = lookups or [{}]  # arguments for the first tool, one call each, in parallel

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        assert request.url.path.endswith("/chat/completions")
        answer = Findings(findings=[]).model_dump_json()
        tools = [t["function"]["name"] for t in body.get("tools", [])]
        called = any(m["role"] == "tool" for m in body["messages"])
        if tools and called and self.text_answer is not None:
            msg = {"role": "assistant", "content": self.text_answer}
            return httpx.Response(200, json=completion(msg, "stop"))
        if tools:
            work = [t for t in tools if t != "final_result"]
            if work and not called:
                calls = [(work[0], json.dumps(a)) for a in self.lookups]
            else:
                calls = [("final_result", answer)]
            msg = {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Look it up.",
                "tool_calls": [
                    {
                        "id": f"call_{len(self.bodies)}_{i}",
                        "type": "function",
                        "function": {"name": n, "arguments": a},
                    }
                    for i, (n, a) in enumerate(calls)
                ],
            }
            return httpx.Response(200, json=completion(msg, "tool_calls"))
        msg = {"role": "assistant", "content": self.json_answer or answer,
               "reasoning_content": "Nothing stands out."}
        return httpx.Response(200, json=completion(msg, self.finish))

    def client(self) -> AsyncOpenAI:
        transport = httpx.MockTransport(self.handler)
        return AsyncOpenAI(
            api_key="test",
            base_url="https://baseten.test/v1",
            http_client=httpx.AsyncClient(transport=transport),
        )


def test_tools_use_final_result_tool():
    """With tools in the request Baseten ignores response_format, so the answer is a tool call."""
    fake = BasetenFake()
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)

    def lookup() -> str:
        """Look up the evidence."""
        return "ev_031: 96 trials"

    out = asyncio.run(
        run_role(
            "analyst", Findings, pack(), scope, chat_model(fake.client(), effort="low"), [lookup]
        )
    )
    assert out == Findings(findings=[])
    first, second = fake.bodies
    assert first["model"] == BASETEN_MODEL
    assert first["reasoning_effort"] == "low"
    assert first["max_completion_tokens"] == MAX_OUTPUT  # a runaway answer stops early
    assert "response_format" not in first
    assert first["tool_choice"] == "auto"
    final = next(t for t in first["tools"] if t["function"]["name"] == "final_result")
    assert final["function"]["strict"] is True
    assert any(m["role"] == "tool" for m in second["messages"])
    blocked = {e.payload["tool"] for e in ws.events.list("tool.blocked")}
    assert "final_result" not in blocked  # an output tool, not a role tool
    calls = ws.events.list("model.call")
    assert [c.payload["model"] for c in calls] == [BASETEN_MODEL, BASETEN_MODEL]
    assert calls[0].payload["cost"] == pytest.approx(estimate_cost(BASETEN_MODEL, 100, 40))
    assert estimate_cost(BASETEN_MODEL, 1_000_000, 1_000_000) == pytest.approx(5.50)


@pytest.mark.parametrize("fence", ["", "```json\n"])
def test_json_text_answer_is_accepted(fence):
    """Baseten does not force the tool call, so the same JSON as text (fenced or not) counts."""
    answer = Findings(findings=[]).model_dump_json()
    fake = BasetenFake(text_answer=f"{fence}{answer}\n```" if fence else answer)
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)

    def lookup() -> str:
        """Look up the evidence."""
        return "ev_031: 96 trials"

    out = asyncio.run(
        run_role("analyst", Findings, pack(), scope, chat_model(fake.client()), [lookup])
    )
    assert out == Findings(findings=[])
    assert len(fake.bodies) == 2  # no retry


def test_wrong_shape_text_is_retried():
    fake = BasetenFake(text_answer="[]")

    def lookup() -> str:
        """Look up the evidence."""
        return "ev_031: 96 trials"

    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    with pytest.raises(Exception, match="output retries"):
        asyncio.run(
            run_role("analyst", Findings, pack(), scope, chat_model(fake.client()), [lookup])
        )
    retry = fake.bodies[-1]["messages"][-1]
    assert "final_result" in json.dumps(retry)


def test_parallel_evidence_lookups(tmp_path):
    """Baseten often asks for two evidence entries at once; both read the same SQLite file."""
    ws = Workspace(tmp_path / "ws.db")
    ws.ledger.add(
        Evidence(evidence_id="ev-a", kind="state", values={"x": 1.0}, inputs=[], note="A")
    )
    ws.ledger.add(
        Evidence(evidence_id="ev-b", kind="state", values={"x": 2.0}, inputs=[], note="B")
    )
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    fake = BasetenFake(lookups=[{"evidence_id": f"ev-{k}"} for k in "ab" * 4])
    out = asyncio.run(
        run_role(
            "analyst", Findings, pack(), scope, chat_model(fake.client()), [evidence_tool(scope)]
        )
    )
    assert out == Findings(findings=[])
    results = [m for m in fake.bodies[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 8 and all("error" not in m["content"] for m in results)
    ws.close()


def test_no_tools_use_json_schema():
    fake = BasetenFake()
    ws = Workspace()
    # The Reviewer has no tools of its own and no memory tools.
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    out = asyncio.run(run_role("reviewer", Findings, pack(), scope, chat_model(fake.client())))
    assert out == Findings(findings=[])
    (body,) = fake.bodies
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning_effort"] == "medium"
    assert "tools" not in body


def test_router_sends_strategist_to_meta():
    meta = responses_model(MetaFake(lambda body: Findings(findings=[])).client())
    text = chat_model(BasetenFake().client())
    router = mixed(meta, text)
    assert isinstance(router, RoleRouter)
    assert resolve(router, "strategist") is meta
    for role in ("analyst", "growth_lead", "producer", "reviewer", "media_buyer"):
        assert resolve(router, role) is text
    assert resolve(meta, "analyst") is meta


def test_mixed_run_logs_each_provider():
    meta_fake = MetaFake(lambda body: Findings(findings=[]))
    router = mixed(responses_model(meta_fake.client()), chat_model(BasetenFake().client()))
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    asyncio.run(run_role("analyst", Findings, pack(), scope, router))
    asyncio.run(run_role("strategist", Findings, pack("strategist"), scope, router))
    models = {c.payload["role"]: c.payload["model"] for c in ws.events.list("model.call")}
    assert models == {"analyst": BASETEN_MODEL, "strategist": "muse-spark-1.3"}


def test_key_comes_from_either_name(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("Baseten_API", raising=False)
    with pytest.raises(RuntimeError, match="BASETEN_API_KEY"):
        baseten_client()
    monkeypatch.setenv("Baseten_API", "k")
    assert baseten_client().api_key == "k"


def test_effort_is_checked():
    with pytest.raises(ValueError):
        chat_model(BasetenFake().client(), effort="max")


# Seen live on Baseten: the deliverable written as tool-call markup instead of a tool call.
LEAKED = ('<atem:function_calls>\n<atem:invoke name="final_result">\n'
          '<atem:parameter name="findings">[]</atem:parameter>\n'
          '<atem:parameter name="note_to_self"></atem:parameter>\n'
          '</atem:invoke>\n</atem:function_calls>')


def test_a_tool_call_written_as_text_becomes_the_call():
    fake = BasetenFake(text_answer=LEAKED)
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)

    def lookup() -> str:
        """Look up the evidence."""
        return "ev_031: 96 trials"

    out = asyncio.run(
        run_role("analyst", Findings, pack(), scope, chat_model(fake.client()), [lookup])
    )
    assert out == Findings(findings=[])
    assert len(fake.bodies) == 2  # no retry
    last = ws.events.list("model.call")[-1].payload
    assert last["ok"] and last["repaired"] == ["tool_call_in_text"]


def test_json_stuck_in_trailing_whitespace_is_closed_without_a_retry():
    """Seen live: a complete answer, then whitespace until the output limit, no closing brace."""
    fake = BasetenFake(json_answer='{"findings":[] ' + " " * 3_000, finish="length")
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "loop-1", 1)
    out = asyncio.run(run_role("reviewer", Findings, pack(), scope, chat_model(fake.client())))
    assert out == Findings(findings=[])
    assert len(fake.bodies) == 1
    assert ws.events.list("model.call")[-1].payload["repaired"] == ["cut_json"]


def test_repairs_stay_narrow():
    stuck = " " * 300
    assert close_cut_json('{"a": [1]' + stuck) == '{"a": [1]}'
    assert close_cut_json('{"a": "half a sent' + stuck) is None  # cut inside a value
    assert close_cut_json('{"a": [1,' + stuck) is None
    assert close_cut_json('{"a": [1]' + " " * 20) is None  # short whitespace: not stuck
    # A call to a tool the request did not offer stays text, so the schema check rejects it.
    text = LEAKED.replace("final_result", "apply_change")
    assert tool_calls_in_text(text, {"final_result": {}}) == ([], text)
    (call,), rest = tool_calls_in_text(LEAKED, {"final_result": {
        "properties": {"findings": {"type": "array"}, "note_to_self": {"type": "string"}}}})
    assert call.args == {"findings": [], "note_to_self": ""} and rest == ""
