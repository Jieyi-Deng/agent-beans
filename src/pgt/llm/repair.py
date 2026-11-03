"""Ways an open-weight model on Chat Completions hands in a good answer in the wrong shape, and
the narrow repairs code makes before the answer is checked. A repair never adds content: the
repaired answer still has to pass the strict schema, and the `model.call` event says which
repair ran.

- A tool call written as text (`<x:invoke name="final_result"><x:parameter name="k">v</...>`)
  becomes a real call, when that tool was offered in the request.
- JSON cut off at the output limit while the model was only emitting whitespace loses the
  whitespace and gets its open brackets closed. A cut inside a value is left alone.
- JSON cut off inside `note_to_self`, the last field and never part of the deliverable: the
  note is trimmed and the JSON closed. A cut anywhere else is left alone.
- JSON with raw control characters (tabs, newlines) inside its strings, which strict JSON
  forbids (seen live: a lessons pass padded its strings with tabs and failed three times): the
  characters are escaped, so each string keeps what it held. Runs before the two repairs above,
  which a cut-off answer may still need.
- A teammate's answer written as plain sentences, not as the Answer object (seen live: the
  Analyst used its recall tools, then wrote the answer as text). The text becomes `answer` and
  the evidence IDs it cites become `evidence_ids`; code then checks those IDs and numbers as
  it checks any answer. Only for a final_result shaped exactly like Answer.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from pgt.harness.memory import EVIDENCE_ID

WHITESPACE_RUN = 200  # trailing whitespace this long means the model was stuck, not done
NOTE_KEEP = 480  # characters of a cut-off note kept; code trims it to two lines later

_NS = r"(?:[\w.-]+:)?"
_INVOKE = re.compile(rf'<{_NS}invoke\s+name="([^"]+)"\s*>(.*?)</{_NS}invoke>', re.DOTALL)
_PARAM = re.compile(rf'<{_NS}parameter\s+name="([^"]+)"\s*>(.*?)</{_NS}parameter>', re.DOTALL)
_WRAPPER = re.compile(rf"</?{_NS}function_calls>")
_NOTE = re.compile(r'"note_to_self"\s*:\s*"')
_TAIL_ESCAPE = re.compile(r"(\\+)(u[0-9a-fA-F]{0,3})?$")
_JSON_START = re.compile(r"\s*(?:```(?:json)?[ \t]*\n\s*)?(?=[\[{])")  # optionally fenced
_ESCAPES = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


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


def _scan(body: str) -> tuple[list[str], int | None] | None:
    """The brackets still open at the end of `body`, and where the string it ends inside
    starts (None when it ends outside a string). None when the brackets do not match."""
    closers: list[str] = []
    opened: int | None = None
    escaped = False
    for i, ch in enumerate(body):
        if opened is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                opened = None
        elif ch == '"':
            opened = i
        elif ch in "[{":
            closers.append("]" if ch == "[" else "}")
        elif ch in "]}" and (not closers or closers.pop() != ch):
            return None
    return closers, opened


def _drop_half_escape(text: str) -> str:
    """Text cut in the middle of an escape (a lone backslash, or \\u and fewer than 4 digits)
    loses the half escape."""
    m = _TAIL_ESCAPE.search(text)
    if m and len(m.group(1)) % 2:
        return text[:m.end(1) - 1]
    return text


def _parses(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def close_cut_json(text: str) -> str | None:
    """JSON followed by a long run of whitespace, usually cut off at the output limit: the JSON
    with the whitespace dropped and its open brackets closed. None when the text does not end
    that way, or the cut fell inside a string or after a comma."""
    body = text.rstrip()
    if len(text) - len(body) < WHITESPACE_RUN or body[:1] not in "{[":
        return None
    state = _scan(body)
    if state is None or state[1] is not None:
        return None
    candidate = body + "".join(reversed(state[0]))
    return candidate if _parses(candidate) else None


def close_cut_note(text: str) -> str | None:
    """JSON that stops inside the `note_to_self` string: the note cut to NOTE_KEEP characters
    and the JSON closed. None when the text parses already, or stops anywhere else."""
    body = text.rstrip()
    if body[:1] not in "{[" or _parses(body):
        return None
    found = list(_NOTE.finditer(body))
    state = _scan(body)
    if not found or state is None or state[1] != found[-1].end() - 1:
        return None
    start = found[-1].end()
    note = _drop_half_escape(body[start:start + NOTE_KEEP])
    head = body[:start] + note
    state = _scan(head)
    if state is None or state[1] != start - 1:  # trimming ended the string early
        return None
    candidate = head + '"' + "".join(reversed(state[0]))
    return candidate if _parses(candidate) else None


def escape_control_chars(text: str) -> str | None:
    """JSON (fenced or not) with raw control characters inside its strings: the same text with
    them escaped (a tab becomes \\t). Whitespace between values, and anything after the JSON,
    stays as it is. None when the text does not start like JSON or has no such character."""
    start = _JSON_START.match(text)
    if start is None:
        return None
    out = [text[:start.end()]]
    depth, in_string, escaped, changed = 0, False, False, False
    for i in range(start.end(), len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ch < " ":
                ch = _ESCAPES.get(ch) or f"\\u{ord(ch):04x}"
                changed = True
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                out.append(text[i:])
                break
        out.append(ch)
    return "".join(out) if changed else None


ANSWER_FIELDS = {"answer", "evidence_ids", "note_to_self"}


def answer_in_text(response: ModelResponse, tools: dict[str, dict]) -> ToolCallPart | None:
    """A plain-text answer as a final_result call, when final_result is shaped like Answer and
    the response is only prose: no tool call, nothing that starts like JSON."""
    schema = tools.get("final_result") or {}
    if set(schema.get("properties", {})) != ANSWER_FIELDS:
        return None
    if any(isinstance(p, ToolCallPart) for p in response.parts):
        return None
    text = "".join(p.content for p in response.parts if isinstance(p, TextPart)).strip()
    if not text or text[:1] in "{[`<":
        return None
    return ToolCallPart("final_result", {
        "answer": text, "evidence_ids": list(dict.fromkeys(EVIDENCE_ID.findall(text))),
        "note_to_self": ""}, tool_call_id=f"text_{uuid.uuid4().hex[:12]}")


CUTS = (("cut_json", close_cut_json), ("cut_note", close_cut_note))


def _fix_json(text: str, ran: list[str], cuts=CUTS) -> str:
    """`text` with control characters escaped, then the first of `cuts` that applies. The
    names of the repairs that ran are added to `ran`."""
    if (escaped := escape_control_chars(text)) is not None:
        ran.append("control_chars")
        text = escaped
    for name, fix in cuts:
        if (fixed := fix(text)) is not None:
            ran.append(name)
            return fixed
    return text


def repair(response: ModelResponse, tools: dict[str, dict]) -> tuple[ModelResponse, list[str]]:
    """The response with the repairs applied, and which ones ran."""
    if (call := answer_in_text(response, tools)) is not None:
        rest = [p for p in response.parts if not isinstance(p, TextPart)]
        return replace(response, parts=[*rest, call]), ["answer_in_text"]
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
            content = _fix_json(part.content, ran)
            if content != part.content:
                part = replace(part, content=content)
        elif isinstance(part, ToolCallPart) and isinstance(part.args, str):
            args = _fix_json(part.args, ran, CUTS[1:])
            if args != part.args:
                part = replace(part, args=args)
        parts.append(part)
    if not ran:
        return response, []
    return replace(response, parts=parts), ran
