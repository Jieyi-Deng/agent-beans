"""P0 proof: cached tokens on every call, a byte-stable prefix, stored requests, failure types."""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from pgt.cli import main
from pgt.harness import memory
from pgt.harness.context import assemble
from pgt.harness.policy import CostMeter
from pgt.harness.requests import INSTRUCTIONS_REF, PROMPT_REF, inspect_call, render
from pgt.harness.workspace import Workspace
from pgt.llm import runtime
from pgt.llm.fake import FunctionCall, MetaFake
from pgt.llm.meta import responses_model
from pgt.llm.runtime import ModelCallTimeout, Scope, run_role
from pgt.sandbox import fitness_app_facts
from pgt.schemas import Finding, Findings

LONG = "You are a test role. " + "Read the inputs with care and cite every number. " * 120


def pack(data=None, role="analyst", topic="day1/findings"):
    return assemble(role, LONG, fitness_app_facts(), 1, task="Write the findings.",
                    done_when="Done.", data=data or {"day": 1, "signals": []},
                    inputs=["a@v1"], topic=topic)


def finding(subject="ad_1"):
    return Finding(subject_id=subject, claim="CTR fell.", metric="ctr", value=1.0,
                   evidence_ids=["ev-1"], severity="act")


def run(ws, fake, p, day=1, tools=()):
    scope = Scope(ws, CostMeter(ws.events), f"daily-d{day}", day)
    return asyncio.run(run_role("analyst", Findings, p, scope, responses_model(fake.client()),
                                list(tools)))


def test_every_call_records_cached_tokens_and_the_second_call_hits_the_cache():
    fake = MetaFake(lambda body: Findings(findings=[]))
    ws = Workspace()
    run(ws, fake, pack({"day": 1, "signals": [{"subject_id": "ad_1"}]}))
    run(ws, fake, pack({"day": 2, "signals": [{"subject_id": "ad_2"}]}), day=2)
    first, second = (e.payload for e in ws.events.list("model.call"))
    assert "cached_tokens" in first and first["cached_tokens"] == 0
    assert second["cached_tokens"] >= 1024 and second["cached_tokens"] <= second["input_tokens"]
    assert second["failure"] is None and second["ok"]


def test_prefix_is_byte_stable_up_to_the_task_with_the_same_memory_versions():
    ws = Workspace()
    memory.write_version(ws, memory.ACCOUNT, None, [memory.Line(
        id="a1", text="Square video hooks beat stills here.", sources=["wk-1"],
        author="growth_lead")], "growth_lead", 1, "test")
    memory.write_version(ws, memory.LESSONS, "analyst", [memory.Line(
        id="l1", text="Check the learning phase before calling an ad tired.", sources=["j-1"],
        author="analyst")], "analyst", 1, "test")
    fake = MetaFake(lambda body: Findings(findings=[]))
    run(ws, fake, pack({"day": 3, "signals": [{"subject_id": "ad_1", "severity": "act"}]}), 3)
    run(ws, fake, pack({"day": 4, "signals": [{"subject_id": "ad_9", "severity": "watch"}]}), 4)
    a, b = fake.requests
    # Everything before the task is identical: tools, output schema and instructions.
    assert a["instructions"] == b["instructions"]
    assert a.get("tools") == b.get("tools") and a["text"] == b["text"]
    assert "Account memory (version 1)" in a["instructions"]
    assert "Check the learning phase" in a["instructions"]
    prompt = a["input"][0]["content"] if isinstance(a["input"], list) else a["input"]
    text = prompt if isinstance(prompt, str) else prompt[0]["text"]
    assert text.startswith("## Task")
    assert a["input"] != b["input"]


def test_another_roles_lessons_never_reach_this_role():
    ws = Workspace()
    memory.write_version(ws, memory.LESSONS, "media_buyer", [memory.Line(
        id="l1", text="SECRET-MB-LESSON", sources=["j-1"], author="media_buyer")],
        "media_buyer", 1, "test")
    fake = MetaFake(lambda body: Findings(findings=[]))
    run(ws, fake, pack())
    assert "SECRET-MB-LESSON" not in json.dumps(fake.requests)
    with pytest.raises(memory.MemoryAccessError):
        memory.view(ws, memory.LESSONS, "media_buyer", reader="analyst")
    with pytest.raises(memory.MemoryAccessError):
        memory.journal(ws, "media_buyer", reader="analyst")


def test_stored_request_rebuilds_exactly_what_the_model_saw(tmp_path):
    db = tmp_path / "run.db"
    ws = Workspace(db)

    def get_evidence(evidence_id: str) -> str:
        """Read one evidence row."""
        return "row"

    def responder(body):
        if not any(i.get("type") == "function_call_output" for i in body["input"]):
            return FunctionCall("get_evidence", {"evidence_id": "ev-1"})
        return Findings(findings=[finding()])

    fake = MetaFake(responder)
    p = pack()
    run(ws, fake, p, tools=[get_evidence])
    calls = ws.events.list("model.call")
    assert len(calls) == 2 and all(c.payload["request_hash"] for c in calls)
    assert calls[0].payload["request_hash"] != calls[1].payload["request_hash"]
    assert ws.requests.count() == (1, 2)
    seen = inspect_call(ws, calls[1].id)
    sent = fake.requests[1]
    assert seen["instructions"] == sent["instructions"]
    first = sent["input"][0]
    assert seen["prompt"] == (first["content"] if isinstance(first["content"], str)
                              else first["content"][0]["text"])
    assert [t["name"] for t in seen["tools"]] == ["get_evidence", "recall_week", "recall_journal",
                                                 "propose_fact"]
    assert seen["output"]["object"]["name"] == "Findings"
    assert seen["settings"]["tool_choice"] == "auto"
    assert any(part.get("tool_name") == "get_evidence"
               for turn in seen["turns"] for part in turn["parts"])
    body = ws.requests.request(calls[0].payload["request_hash"])
    assert PROMPT_REF in [part.get("content") for m in body["messages"] for part in m["parts"]]
    assert body["messages"][0].get("instructions") in (INSTRUCTIONS_REF, None)
    assert "## Instructions" in render(seen) and "get_evidence" in render(seen)
    ws.close()


def test_inspect_call_command_prints_the_request(tmp_path, capsys):
    db = tmp_path / "run.db"
    ws = Workspace(db)
    run(ws, MetaFake(lambda body: Findings(findings=[])), pack())
    event_id = ws.events.list("model.call")[0].id
    ws.close()
    main(["inspect-call", str(event_id), "--db", str(db)])
    out = capsys.readouterr().out
    assert f"Model call {event_id} · analyst" in out and "## Task" in out
    main(["usage", "--db", str(db)])
    assert "analyst" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["inspect-call", "1", "--db", str(db)])


def test_schema_failure_is_marked_on_the_call_that_missed():
    answers = iter([{"findings": "not a list"}, Findings(findings=[])])
    fake = MetaFake(lambda body: next(answers))
    ws = Workspace()
    run(ws, fake, pack())
    assert [c.payload["failure"] for c in ws.events.list("model.call")] == ["schema", None]


def test_blocked_tool_is_marked_on_the_call():
    def responder(body):
        if not any(i.get("type") == "function_call_output" for i in body["input"]):
            return FunctionCall("update_budget", {"campaign_id": "cmp_1", "daily_budget": 9})
        return Findings(findings=[])

    ws = Workspace()
    run(ws, MetaFake(responder), pack())
    assert [c.payload["failure"] for c in ws.events.list("model.call")] == ["blocked_tool", None]


class SlowFake:
    """Answers after `delays[n]` seconds on the n-th request, or with an HTTP error."""

    def __init__(self, delays, status=200):
        self.delays, self.status, self.n = list(delays), status, 0
        self.fake = MetaFake(lambda body: Findings(findings=[]))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        delay = self.delays[min(self.n, len(self.delays) - 1)]
        self.n += 1
        await asyncio.sleep(delay)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "down"}})
        return self.fake._respond(request)

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(base_url="https://api.meta.ai/v1", api_key="offline", max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)))


def test_a_timeout_is_logged_and_retried_once(monkeypatch):
    monkeypatch.setattr(runtime, "TEXT_DEADLINE_S", 0.05)
    slow = SlowFake([0.3, 0])
    ws = Workspace()
    run(ws, slow, pack())
    calls = [c.payload for c in ws.events.list("model.call")]
    assert [(c["failure"], c["attempt"]) for c in calls] == [("timeout", 1), (None, 2)]
    assert calls[0]["deadline_s"] == 0.05


def test_two_timeouts_stop_the_role(monkeypatch):
    monkeypatch.setattr(runtime, "TEXT_DEADLINE_S", 0.05)
    ws = Workspace()
    with pytest.raises(ModelCallTimeout):
        run(ws, SlowFake([0.3]), pack())
    assert [c.payload["failure"] for c in ws.events.list("model.call")] == ["timeout", "timeout"]


def test_provider_error_is_its_own_failure_type():
    ws = Workspace()
    with pytest.raises(Exception):  # noqa: B017 - the provider's own error type
        run(ws, SlowFake([0], status=500), pack())
    assert [c.payload["failure"] for c in ws.events.list("model.call")] == ["provider"]


def test_note_to_self_is_kept_in_the_journal_and_cut_to_two_lines():
    def responder(body):
        assert "note_to_self" in body["text"]["format"]["schema"]["required"]
        return {"findings": [], "note_to_self": "one\ntwo\nthree"}

    ws = Workspace()
    out = run(ws, MetaFake(responder), pack())
    assert out == Findings(findings=[]) and type(out) is Findings
    (entry,) = ws.events.list("journal.entry")
    assert entry.payload["note_to_self"] == "one\ntwo"
    assert entry.payload["topic"] == "day1/findings" and entry.payload["kind"] == "Findings"


def test_an_image_call_has_its_own_deadline_and_one_retry(monkeypatch):
    from pgt.llm.media import MediaClient
    from pgt.llm.offline import offline_image

    calls = {"n": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.3)
        return MetaFake(lambda body: Findings(findings=[]), offline_image)._respond(request)

    monkeypatch.setattr(runtime, "IMAGE_DEADLINE_S", 0.05)
    client = AsyncOpenAI(base_url="https://api.meta.ai/v1", api_key="offline", max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    ws = Workspace()
    scope = Scope(ws, CostMeter(ws.events), "weekly-w1", 7)
    image = asyncio.run(MediaClient(client).background(scope, "producer", "An empty room."))
    assert image.size[0] > 0
    calls_logged = [c.payload for c in ws.events.list("model.call")]
    assert [(c["failure"], c["attempt"], c["deadline_s"]) for c in calls_logged] == [
        ("timeout", 1, 0.05), (None, 2, 0.05)]
    assert calls_logged[0]["cost"] == 0 and calls_logged[1]["cost"] > 0
