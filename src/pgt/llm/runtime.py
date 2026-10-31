"""Runs one role as a Pydantic AI agent inside the harness: logged calls, cost caps, tool policy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ValidationError
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

from pgt.harness.context import ContextPack
from pgt.harness.policy import CostMeter, check_tool
from pgt.harness.workspace import Workspace
from pgt.llm.meta import estimate_cost
from pgt.llm.routing import RoleRouter, resolve


@dataclass
class Scope:
    """Where a role runs: the workspace it logs to, its spend meter and the loop it belongs to."""

    ws: Workspace
    meter: CostMeter
    loop_id: str
    sim_day: int | None = None


@dataclass
class RoleRun[T: BaseModel]:
    """A role's typed output plus what the hosted tools did along the way."""

    output: T
    searches: int = 0
    citations: list[tuple[str, str]] = field(default_factory=list)  # (title, url)


def _output_spec[T: BaseModel](model: Model, output_type: type[T], tools: list):
    """Strict JSON schema output, except on Chat Completions (Baseten) when the role has tools.
    There the server enforces neither response_format nor tool_choice="required" once tools are
    in the request, so the deliverable comes back as a call to a strict final_result tool, and a
    plain-text answer is still accepted when it is the same JSON (code fences allowed)."""
    if tools and isinstance(model, OpenAIChatModel):
        return [ToolOutput(output_type, strict=True), TextOutput(_json_text(output_type))]
    return NativeOutput(output_type, strict=True)


def _json_text[T: BaseModel](output_type: type[T]):
    def parse(text: str) -> T:
        body = text.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            return output_type.model_validate_json(body)
        except ValidationError as e:
            raise ModelRetry(
                f"Call final_result with the {output_type.__name__} object. {e}"
            ) from e

    parse.__annotations__["return"] = output_type
    return parse


class _Harness(AbstractCapability):
    def __init__(self, role: str, scope: Scope, pack: ContextPack) -> None:
        self.role, self.scope, self.pack = role, scope, pack
        self.searches = 0
        self.citations: list[tuple[str, str]] = []
        self.blocked: set[str] = set()  # tool call IDs already refused and logged
        self.repeats: dict[str, int] = {}  # tool + args -> times called this run

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

    def _block_disallowed(self, response: ModelResponse, output_tools: set[str]) -> None:
        """Log every call the model makes to a tool outside the role's allowlist, including
        tools the role was never given (a prompt injection asking for a write tool, say).
        Output tools (final_result, how a role hands in its deliverable on Baseten) are not
        role tools and are not checked."""
        for part in response.parts:
            if (
                isinstance(part, ToolCallPart)
                and part.tool_name not in output_tools
                and part.tool_call_id not in self.blocked
                and check_tool(self.role, part.tool_name, self.scope.ws.events, self.scope.loop_id)
            ):
                self.blocked.add(part.tool_call_id)

    async def wrap_model_request(
        self,
        ctx: RunContext,
        *,
        request_context: ModelRequestContext,
        handler: WrapModelRequestHandler,
    ) -> ModelResponse:
        self.scope.meter.check(self.role, self.scope.loop_id)  # refuse once the total cap is used
        response = None
        started = perf_counter()
        try:
            response = await handler(request_context)
        finally:
            usage = response.usage if response is not None else None
            model_name = request_context.model.model_name
            input_tokens = usage.input_tokens if usage else 0
            output_tokens = usage.output_tokens if usage else 0
            searches = self._record_hosted(response) if response is not None else 0
            if response is not None:
                params = request_context.model_request_parameters
                self._block_disallowed(response, {t.name for t in params.output_tools})
            cost = estimate_cost(model_name, input_tokens, output_tokens, searches)
            self.scope.ws.events.append(
                "model.call",
                self.role,
                {
                    "role": self.role,
                    "model": model_name,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "searches": searches,
                    "cost": round(cost, 6),
                    "wall_time_s": round(perf_counter() - started, 3),
                    "context_hash": self.pack.context_hash,
                    "fact_version": self.pack.fact_version,
                    "ok": response is not None,
                },
                refs=self.pack.inputs,
                sim_day=self.scope.sim_day,
                loop_id=self.scope.loop_id,
            )
            # Raises SpendCapExceeded, which stops the run before the next model call.
            if response is not None:
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
        return await handler(args)


async def run_role[T: BaseModel](
    role: str,
    output_type: type[T],
    pack: ContextPack,
    scope: Scope,
    model: Model | RoleRouter,
    tools: list = (),
    attachments: list[BinaryContent] = (),
    capabilities: list[AbstractCapability] = (),
) -> T:
    """Run one role and return its typed deliverable (strict JSON schema output)."""
    run = await run_role_full(
        role, output_type, pack, scope, model, tools, attachments, capabilities
    )
    return run.output


async def run_role_full[T: BaseModel](
    role: str,
    output_type: type[T],
    pack: ContextPack,
    scope: Scope,
    model: Model | RoleRouter,
    tools: list = (),
    attachments: list[BinaryContent] = (),
    capabilities: list[AbstractCapability] = (),
) -> RoleRun[T]:
    """Like run_role, plus hosted search counts and citations. `attachments` are images the
    model sees with the prompt (the Reviewer's vision check). `capabilities` add hosted tools;
    callers check the allowlist before adding one."""
    model = resolve(model, role)
    harness = _Harness(role, scope, pack)
    agent = Agent(
        model,
        name=role,
        instructions=pack.instructions,
        output_type=_output_spec(model, output_type, tools),
        tools=list(tools),
        capabilities=[harness, *capabilities],
        # Open-weight models sometimes miss the schema once; one extra try keeps a day from failing.
        retries={"tools": 1, "output": 2},
        model_settings={"tool_choice": "auto", "openai_store": False},
    )
    agent.instrument = False
    scope.ws.events.append(
        "context.assembled",
        role,
        {
            "role": role,
            "context_hash": pack.context_hash,
            "fact_version": pack.fact_version,
            "attachments": len(attachments),
        },
        refs=pack.inputs,
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )
    prompt = [pack.prompt, *attachments] if attachments else pack.prompt
    result = await agent.run(prompt)
    return RoleRun(result.output, harness.searches, list(dict.fromkeys(harness.citations)))
