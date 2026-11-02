"""Runs one role as a Pydantic AI agent inside the harness: logged calls, cost caps, tool policy,
deadlines, stored requests, and the role's memory in its context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Agent, ModelRetry, NativeOutput, TextOutput, ToolOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import WrapModelRequestHandler, WrapToolExecuteHandler
from pydantic_ai.messages import (
    BinaryContent,
    ModelResponse,
    NativeToolCallPart,
    TextPart,
    ToolCallPart,
    WebCitationSource,
)
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.tools import RunContext, ToolDefinition

from pgt.harness import memory as team_memory
from pgt.harness.budgets import budget_for
from pgt.harness.context import ContextPack, MemorySlice, reassemble
from pgt.harness.policy import CostMeter, SpendCapExceeded, check_tool
from pgt.harness.requests import capture
from pgt.harness.workspace import Workspace
from pgt.llm.meta import estimate_cost
from pgt.llm.repair import repair
from pgt.llm.routing import RoleRouter, resolve

# Deadlines in seconds. A model call that runs past its deadline is logged and tried once more.
TEXT_DEADLINE_S = 120.0
SEARCH_DEADLINE_S = 180.0  # the Strategist's hosted web search
TOOL_DEADLINE_S = 30.0
IMAGE_DEADLINE_S = 90.0  # Muse Image and SAM 3.1
ASK_DEADLINE_S = 300.0  # an ask_* tool runs another role's call
NOTE_LINES, NOTE_CHARS = 2, 240
NOTE_MAX = NOTE_LINES * NOTE_CHARS  # the whole note, in characters

FAILURES = ("timeout", "schema", "provider", "spend_cap", "blocked_tool")


class ModelCallTimeout(RuntimeError):
    """A model call ran past its deadline twice."""


@dataclass
class Scope:
    """Where a role runs: the workspace it logs to, its spend meter and the loop it belongs to.
    `memory` puts the role's readable memory into its context; `budget_scale` scales its
    may-trim budgets (the sweep runs 0.5, 1 and 2)."""

    ws: Workspace
    meter: CostMeter
    loop_id: str
    sim_day: int | None = None
    memory: bool = True
    budget_scale: float | None = None


@dataclass
class RoleRun[T: BaseModel]:
    """A role's typed output plus what the hosted tools did along the way."""

    output: T
    searches: int = 0
    citations: list[tuple[str, str]] = field(default_factory=list)  # (title, url)
    note_to_self: str = ""
    entry_id: int | None = None


def _output_spec[T: BaseModel](model: Model, output_type: type[T], tools: list):
    """Strict JSON schema output, except on Chat Completions (Baseten) when the role has tools.
    There the server enforces neither response_format nor tool_choice="required" once tools are
    in the request, so the deliverable comes back as a call to a strict final_result tool, and a
    plain-text answer is still accepted when it is the same JSON (code fences allowed)."""
    if tools and isinstance(model, OpenAIChatModel):
        return [ToolOutput(output_type, strict=True), TextOutput(_json_text(output_type))]
    return NativeOutput(output_type, strict=True)


def _strip_fence(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
    return body


def _json_text[T: BaseModel](output_type: type[T]):
    def parse(text: str) -> T:
        try:
            return output_type.model_validate_json(_strip_fence(text))
        except ValidationError as e:
            raise ModelRetry(
                f"Call final_result with the {output_type.__name__} object. {e}"
            ) from e

    parse.__annotations__["return"] = output_type
    return parse


def _require_note(schema: dict) -> None:
    """Strict schemas list every field as required; the default only helps offline stand-ins
    that leave the note out."""
    props = schema.get("properties", {})
    if "note_to_self" in props:
        props["note_to_self"].pop("default", None)
        schema["required"] = list(dict.fromkeys([*schema.get("required", []), "note_to_self"]))


def with_note[T: BaseModel](output_type: type[T]) -> type[T]:
    """The output type plus a `note_to_self` field: up to two lines the role wants to remember.
    Same name, so schemas and offline responders see the deliverable they expect."""
    if "note_to_self" in output_type.model_fields:
        return output_type
    namespace = {
        "__annotations__": {"note_to_self": str},
        "__module__": output_type.__module__,
        "__qualname__": output_type.__qualname__,
        "model_config": ConfigDict(**{**output_type.model_config,
                                      "json_schema_extra": _require_note}),
        # maxLength reaches the model as a hint (strict schemas move it into the description);
        # code trims a longer note rather than failing the deliverable over it.
        "note_to_self": Field(
            "", description=f"Optional: at most {NOTE_LINES} short lines (under {NOTE_MAX} "
            "characters in all) you want to remember next time about this decision. Not your "
            "working and never part of the deliverable. Empty string if nothing.",
            json_schema_extra={"maxLength": NOTE_MAX}),
    }
    return type(output_type.__name__, (output_type,), namespace)


def split_note[T: BaseModel](output: BaseModel, output_type: type[T]) -> tuple[T, str]:
    """The deliverable without the note, and the note cut to two short lines in code."""
    if output_type is type(output) or "note_to_self" not in type(output).model_fields:
        return output, ""
    raw = getattr(output, "note_to_self", "") or ""
    lines = [line.strip()[:NOTE_CHARS] for line in raw.strip().splitlines() if line.strip()]
    data = output.model_dump(exclude={"note_to_self"})
    return output_type.model_validate(data), "\n".join(lines[:NOTE_LINES])


def schema_failure(response: ModelResponse, output_cls: type[BaseModel],
                   output_tools: set[str]) -> bool:
    """True when this response was meant as the deliverable but does not match its schema.
    A response that only calls role tools is not judged."""
    calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
    finals = [p for p in calls if p.tool_name in output_tools]
    for part in finals:
        try:
            output_cls.model_validate(part.args_as_dict())
        except Exception:  # noqa: BLE001 - malformed JSON or a schema mismatch both count
            return True
    if finals or calls:
        return False
    # As Pydantic AI reads it: text written before a hosted tool call (a web search) is the
    # model thinking aloud, so the answer is the text after the last one.
    text = before = ""
    for part in response.parts:
        if isinstance(part, TextPart):
            text += part.content
        elif isinstance(part, NativeToolCallPart):
            before, text = text or before, ""
    text = (text or before).strip()
    if not text:
        return True
    try:
        output_cls.model_validate_json(_strip_fence(text))
    except ValidationError:
        return True
    return False


class _Harness(AbstractCapability):
    def __init__(self, role: str, scope: Scope, pack: ContextPack,
                 output_cls: type[BaseModel]) -> None:
        self.role, self.scope, self.pack, self.output_cls = role, scope, pack, output_cls
        self.searches = 0
        self.citations: list[tuple[str, str]] = []
        self.blocked: set[str] = set()  # tool call IDs already refused and logged
        self.repeats: dict[str, int] = {}  # tool + args -> times called this run
        self.refused = 0  # repeated calls answered with "hand in your final answer"

    def _record_hosted(self, response: ModelResponse) -> int:
        """Log hosted (server-side) tool calls such as web search, and collect citations."""
        searches = 0
        for part in response.parts:
            if isinstance(part, NativeToolCallPart):
                if part.tool_name == "web_search":
                    searches += 1
                self.scope.ws.events.append(
                    "tool.call",
                    self.role,
                    {
                        "role": self.role,
                        "tool": part.tool_name,
                        "args": part.args_as_dict(),
                        "hosted": True,
                    },
                    sim_day=self.scope.sim_day,
                    loop_id=self.scope.loop_id,
                )
            elif isinstance(part, TextPart):
                for citation in part.citations or []:
                    for source in citation.sources:
                        if isinstance(source, WebCitationSource):
                            self.citations.append((source.title or source.url, source.url))
        self.searches += searches
        return searches

    def _block_disallowed(self, response: ModelResponse, output_tools: set[str]) -> int:
        """Log every call the model makes to a tool outside the role's allowlist, including
        tools the role was never given (a prompt injection asking for a write tool, say).
        Output tools (final_result, how a role hands in its deliverable on Baseten) are not
        role tools and are not checked. Returns how many were refused."""
        refused = 0
        for part in response.parts:
            if (
                isinstance(part, ToolCallPart)
                and part.tool_name not in output_tools
                and part.tool_call_id not in self.blocked
                and check_tool(self.role, part.tool_name, self.scope.ws.events, self.scope.loop_id)
            ):
                self.blocked.add(part.tool_call_id)
                refused += 1
        return refused

    def _store_request(self, request_context: ModelRequestContext) -> str | None:
        try:
            return self.scope.ws.requests.store_request(capture(request_context, self.pack))
        except Exception as e:  # noqa: BLE001 - storing must never stop a live run
            self.scope.ws.events.append(
                "harness.error", "system",
                {"role": self.role, "where": "store_request", "error": str(e)[:300]},
                loop_id=self.scope.loop_id,
            )
            return None

    def _log_call(self, model_name: str, response: ModelResponse | None, *, failure: str | None,
                  started: float, request_hash: str | None, attempt: int,
                  deadline: float | None, searches: int = 0,
                  repaired: list[str] | None = None) -> float:
        usage = response.usage if response is not None else None
        input_tokens = usage.input_tokens if usage else 0
        output_tokens = usage.output_tokens if usage else 0
        cached = usage.cache_read_tokens if usage else 0
        cost = estimate_cost(model_name, input_tokens, output_tokens, searches)
        self.scope.ws.events.append(
            "model.call",
            self.role,
            {
                "role": self.role,
                "model": model_name,
                "input_tokens": input_tokens,
                "cached_tokens": cached,
                "output_tokens": output_tokens,
                "searches": searches,
                "cost": round(cost, 6),
                "wall_time_s": round(perf_counter() - started, 3),
                "context_hash": self.pack.context_hash,
                "request_hash": request_hash,
                "fact_version": self.pack.fact_version,
                "topic": self.pack.topic,
                "attempt": attempt,
                "deadline_s": deadline,
                "ok": failure is None,
                "failure": failure,
                **({"repaired": repaired} if repaired else {}),
            },
            refs=self.pack.inputs,
            sim_day=self.scope.sim_day,
            loop_id=self.scope.loop_id,
        )
        return cost

    async def wrap_model_request(
        self,
        ctx: RunContext,
        *,
        request_context: ModelRequestContext,
        handler: WrapModelRequestHandler,
    ) -> ModelResponse:
        model_name = request_context.model.model_name
        try:
            self.scope.meter.check(self.role, self.scope.loop_id)  # refuse once the cap is used
        except SpendCapExceeded:
            self._log_call(model_name, None, failure="spend_cap", started=perf_counter(),
                           request_hash=None, attempt=1, deadline=None)
            raise
        if self.refused:
            # The model was told to stop fetching and answer, and some open-weight models keep
            # calling anyway. From here the request carries no role tools, only the way to
            # hand in the deliverable.
            params = request_context.model_request_parameters
            request_context = replace(
                request_context, model_request_parameters=replace(params, function_tools=[])
            )
        params = request_context.model_request_parameters
        deadline = SEARCH_DEADLINE_S if params.native_tools else TEXT_DEADLINE_S
        request_hash = self._store_request(request_context)
        response = None
        for attempt in (1, 2):
            started = perf_counter()
            try:
                # Pydantic AI lets the handler run once; the retry after a timeout sends the
                # same prepared request to the model directly.
                send = handler(request_context) if attempt == 1 else request_context.model.request(
                    request_context.messages, request_context.model_settings,
                    request_context.model_request_parameters)
                response = await asyncio.wait_for(send, deadline)
                break
            except TimeoutError:
                self._log_call(model_name, None, failure="timeout", started=started,
                               request_hash=request_hash, attempt=attempt, deadline=deadline)
                if attempt == 2:
                    raise ModelCallTimeout(
                        f"{self.role}: the model did not answer within {deadline:g} s, twice"
                    ) from None
            except Exception:
                self._log_call(model_name, None, failure="provider", started=started,
                               request_hash=request_hash, attempt=attempt, deadline=deadline)
                raise
        # A tool call written as text, or JSON stuck in trailing whitespace (see repair.py).
        offered = {t.name: t.parameters_json_schema
                   for t in [*params.output_tools, *params.function_tools]}
        response, repaired = repair(response, offered)
        searches = self._record_hosted(response)
        output_tools = {t.name for t in params.output_tools}
        failure = None
        if self._block_disallowed(response, output_tools):
            failure = "blocked_tool"
        elif schema_failure(response, self.output_cls, output_tools):
            failure = "schema"
        cost = self._log_call(model_name, response, failure=failure, started=started,
                              request_hash=request_hash, attempt=attempt, deadline=deadline,
                              searches=searches, repaired=repaired)
        # Raises SpendCapExceeded, which stops the run before the next model call.
        self.scope.meter.record(self.role, self.scope.loop_id, cost, model=model_name)
        return response

    async def wrap_tool_execute(
        self,
        ctx: RunContext,
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        events = self.scope.ws.events
        if call.tool_call_id in self.blocked:
            return f"Role {self.role} is not allowed to call {call.tool_name}."
        if error := check_tool(self.role, call.tool_name, events, self.scope.loop_id):
            return error
        # Open-weight models sometimes fetch the same thing over and over. The answer is
        # already in the conversation, so after two identical calls the tool says so.
        same = f"{call.tool_name}:{json.dumps(call.args_as_dict(), sort_keys=True)}"
        self.repeats[same] = self.repeats.get(same, 0) + 1
        if self.repeats[same] > 2:
            self.refused += 1
            return (
                f"You already called {call.tool_name} with these arguments and have the result "
                "above. Do not call it again. Hand in your final answer now."
            )
        events.append(
            "tool.call",
            self.role,
            {"role": self.role, "tool": call.tool_name, "args": call.args_as_dict()},
            sim_day=self.scope.sim_day,
            loop_id=self.scope.loop_id,
        )
        deadline = ASK_DEADLINE_S if call.tool_name.startswith("ask_") else TOOL_DEADLINE_S
        try:
            return await asyncio.wait_for(handler(args), deadline)
        except TimeoutError:
            events.append(
                "tool.failed",
                self.role,
                {"role": self.role, "tool": call.tool_name, "failure": "timeout",
                 "deadline_s": deadline},
                sim_day=self.scope.sim_day,
                loop_id=self.scope.loop_id,
            )
            return (f"{call.tool_name} did not answer within {deadline:g} s. Work from what "
                    "you already have.")


async def run_role[T: BaseModel](
    role: str,
    output_type: type[T],
    pack: ContextPack,
    scope: Scope,
    model: Model | RoleRouter,
    tools: list = (),
    attachments: list[BinaryContent] = (),
    capabilities: list[AbstractCapability] = (),
    settings: dict | None = None,
    check: Callable[[T], str | None] | None = None,
) -> T:
    """Run one role and return its typed deliverable (strict JSON schema output)."""
    run = await run_role_full(
        role, output_type, pack, scope, model, tools, attachments, capabilities, settings, check
    )
    return run.output


def with_memory(pack: ContextPack, scope: Scope) -> ContextPack:
    """Re-assemble the context with what this role may read from memory, inside its budget."""
    source = pack.source()
    if source is None:
        return pack
    budget = budget_for(pack.role, scope.budget_scale)
    if not scope.memory:
        return reassemble(pack, MemorySlice(), budget)
    memory = team_memory.slice_for(
        scope.ws, pack.role, day=scope.sim_day,
        subjects=team_memory.subjects_of(source.get("data", {})), topic=pack.topic,
    )
    return reassemble(pack, memory, budget)


PROPOSE_ROLES = frozenset({"growth_lead", "analyst"})


def memory_tools(scope: Scope, role: str) -> list:
    """Recall tools for roles whose context can leave memory out, and propose_fact for the roles
    that may suggest a charter change. The same tools on every call of a role, so the cached
    prefix stays the same. Memory passes (memory=False) get none."""
    if not scope.memory:
        return []
    from pgt.harness.context import RECALL_ROLES
    from pgt.roles.tools import propose_fact_tool, recall_tools

    out = recall_tools(scope, role) if role in RECALL_ROLES else []
    if role in PROPOSE_ROLES:
        out.append(propose_fact_tool(scope, role))
    return out


def _digest(image: BinaryContent) -> dict:
    return {"media_type": image.media_type, "bytes": len(image.data),
            "sha256": hashlib.sha256(image.data).hexdigest()}


def _log_context(scope: Scope, role: str, pack: ContextPack, attachments: list) -> None:
    events = scope.ws.events
    events.append(
        "context.assembled",
        role,
        {
            "role": role,
            "topic": pack.topic,
            "context_hash": pack.context_hash,
            "fact_version": pack.fact_version,
            "attachments": len(attachments),
            "memory_versions": pack.memory_versions,
            "sections": {s.name: s.tokens for s in pack.sections},
            "trimmed": [s.name for s in pack.trimmed],
        },
        refs=pack.inputs,
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )
    if pack.trimmed:
        events.append(
            "context.trimmed",
            role,
            {
                "role": role,
                "topic": pack.topic,
                "context_hash": pack.context_hash,
                "sections": [
                    {"name": s.name, "dropped": s.dropped, "left_out": s.left_out[:50],
                     "tokens": s.tokens, "before_tokens": s.before_tokens, "budget": s.budget}
                    for s in pack.trimmed
                ],
            },
            sim_day=scope.sim_day,
            loop_id=scope.loop_id,
        )
    scope.ws.requests.store_context(pack, [_digest(a) for a in attachments])


async def run_role_full[T: BaseModel](
    role: str,
    output_type: type[T],
    pack: ContextPack,
    scope: Scope,
    model: Model | RoleRouter,
    tools: list = (),
    attachments: list[BinaryContent] = (),
    capabilities: list[AbstractCapability] = (),
    settings: dict | None = None,
    check: Callable[[T], str | None] | None = None,
) -> RoleRun[T]:
    """Like run_role, plus hosted search counts and citations. `attachments` are images the
    model sees with the prompt (the Reviewer's vision check). `capabilities` add hosted tools;
    callers check the allowlist before adding one. `settings` add model settings. `check`
    returns a problem with a schema-valid deliverable, or None; a problem sends the answer back
    once with it as feedback, and the second answer is returned whatever it says."""
    model = resolve(model, role)
    pack = with_memory(pack, scope)
    tools = [*tools, *memory_tools(scope, role)]
    output_cls = with_note(output_type)
    harness = _Harness(role, scope, pack, output_cls)
    agent = Agent(
        model,
        name=role,
        instructions=pack.instructions,
        output_type=_output_spec(model, output_cls, tools),
        tools=list(tools),
        capabilities=[harness, *capabilities],
        # Open-weight models sometimes miss the schema once; one extra try keeps a day from failing.
        retries={"tools": 1, "output": 2},
        model_settings={"tool_choice": "auto", "openai_store": False, **(settings or {})},
    )
    agent.instrument = False
    if check is not None:
        agent.output_validator(_send_back_once(check, output_type, role, scope, pack))
    _log_context(scope, role, pack, list(attachments))
    prompt = [pack.prompt, *attachments] if attachments else pack.prompt
    result = await agent.run(prompt)
    output, note = split_note(result.output, output_type)
    notes_seen = _note_ids(pack)
    entry_id = team_memory.record_entry(
        scope.ws, role, pack, output, note, sim_day=scope.sim_day, loop_id=scope.loop_id,
        notes_seen=notes_seen,
    )
    return RoleRun(output, harness.searches, list(dict.fromkeys(harness.citations)), note,
                   entry_id)


def _send_back_once[T: BaseModel](check: Callable[[T], str | None], output_type: type[T],
                                  role: str, scope: Scope, pack: ContextPack):
    sent = False

    def validate(ctx: RunContext, output):
        nonlocal sent
        if sent or ctx.retry >= ctx.max_retries:
            return output
        problem = check(split_note(output, output_type)[0])
        if not problem:
            return output
        sent = True
        scope.ws.events.append("output.sent_back", role,
                               {"role": role, "topic": pack.topic, "reason": problem[:300]},
                               sim_day=scope.sim_day, loop_id=scope.loop_id)
        raise ModelRetry(problem)

    return validate


def _note_ids(pack: ContextPack) -> list[str]:
    """Owner notes this call's context showed (left-out ones excluded)."""
    section = next((s for s in pack.sections if s.name == "owner_notes"), None)
    if section is None:
        return []
    shown = re.findall(r"^- \[(n-\d+),", section.text, flags=re.MULTILINE)
    return [n for n in shown if n not in section.left_out]
