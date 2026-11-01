"""Two ways an open-weight model on Chat Completions hands in a good answer in the wrong shape,
and the narrow repairs code makes before the answer is checked. A repair never adds content:
the repaired answer still has to pass the strict schema, and the `model.call` event says which
repair ran.

- A tool call written as text (`<x:invoke name="final_result"><x:parameter name="k">v</...>`)
  becomes a real call, when that tool was offered in the request.
- JSON cut off at the output limit while the model was only emitting whitespace loses the
  whitespace and gets its open brackets closed. A cut inside a value is left alone.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

WHITESPACE_RUN = 200  # trailing whitespace this long means the model was stuck, not done

_NS = r"(?:[\w.-]+:)?"
_INVOKE = re.compile(rf'<{_NS}invoke\s+name="([^"]+)"\s*>(.*?)</{_NS}invoke>', re.DOTALL)
_PARAM = re.compile(rf'<{_NS}parameter\s+name="([^"]+)"\s*>(.*?)</{_NS}parameter>', re.DOTALL)
_WRAPPER = re.compile(rf"</?{_NS}function_calls>")


def _value(raw: str, schema: dict):
    """A parameter's text as the tool's schema expects it: strings stay text, the rest is JSON."""
    raw = raw.strip()
    if schema.get("type") == "string":
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw  # the schema check rejects it


def tool_calls_in_text(text: str, tools: dict[str, dict]) -> tuple[list[ToolCallPart], str]:
    """Tool calls the model wrote as text, for tools in `tools` (name -> parameters schema),
    and the text left once they are taken out. Calls to tools it was not offered stay text."""
    calls: list[ToolCallPart] = []

    def take(match: re.Match) -> str:
        name, body = match.group(1), match.group(2)
        if name not in tools:
            return match.group(0)
        props = tools[name].get("properties", {})
        args = {k: _value(v, props.get(k, {})) for k, v in _PARAM.findall(body)}
        calls.append(ToolCallPart(name, args, tool_call_id=f"text_{uuid.uuid4().hex[:12]}"))
        return ""

    rest = _INVOKE.sub(take, text)
    if not calls:
        return [], text
    return calls, _WRAPPER.sub("", rest).strip()


def close_cut_json(text: str) -> str | None:
    """JSON followed by a long run of whitespace, usually cut off at the output limit: the JSON
    with the whitespace dropped and its open brackets closed. None when the text does not end
    that way, or the cut fell inside a string or after a comma."""
    body = text.rstrip()
    if len(text) - len(body) < WHITESPACE_RUN or body[:1] not in "{[":
        return None
    closers: list[str] = []
    in_string = escaped = False
    for ch in body:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            closers.append("]" if ch == "[" else "}")
        elif ch in "]}" and (not closers or closers.pop() != ch):
            return None
    if in_string:
        return None
    candidate = body + "".join(reversed(closers))
    try:
        json.loads(candidate)
    except ValueError:
        return None
    return candidate


def repair(response: ModelResponse, tools: dict[str, dict]) -> tuple[ModelResponse, list[str]]:
    """The response with both repairs applied to its text parts, and which ones ran."""
    parts, ran = [], []
    for part in response.parts:
        if isinstance(part, TextPart):
            calls, rest = tool_calls_in_text(part.content, tools)
            if calls:
                ran.append("tool_call_in_text")
                if rest:
                    parts.append(replace(part, content=rest))
                parts.extend(calls)
                continue
            closed = close_cut_json(part.content)
            if closed is not None:
                ran.append("cut_json")
                parts.append(replace(part, content=closed))
                continue
        parts.append(part)
    if not ran:
        return response, []
    return replace(response, parts=parts), ran
