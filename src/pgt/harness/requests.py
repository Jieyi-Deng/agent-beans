"""Stored requests: exactly what each model call saw, so any call can be inspected later.

A role's context (instructions, prompt, sections, attachment digests) is stored once under its
context hash. Each model request made from it (the first one, and each turn after a tool call or
a retry) is stored under its own request hash, with the model, settings, tool schemas, output
schema and the turns after the prompt. `model.call` events carry both hashes, so
`pgt inspect-call <event id>` can print the request as the model received it.

Both tables are append-only, like the event log.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelMessagesTypeAdapter

if TYPE_CHECKING:
    from pgt.harness.context import ContextPack
    from pgt.harness.events import EventLog

PROMPT_REF = {"$context": "prompt"}
INSTRUCTIONS_REF = {"$context": "instructions"}


def digest(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _plain(value: Any) -> Any:
    """JSON-safe copy of settings and tool configs; anything else becomes its repr."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__name__, **_plain(asdict(value))}
    return repr(value)


def _strip_binary(node: Any) -> Any:
    """Attachments are stored as digests, not bytes."""
    if isinstance(node, dict):
        if node.get("kind") == "binary" and isinstance(node.get("data"), str):
            try:
                raw = base64.b64decode(node["data"])
            except ValueError:
                raw = node["data"].encode()
            return {"kind": "binary", "media_type": node.get("media_type"),
                    "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        return {k: _strip_binary(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_binary(v) for v in node]
    return node


def _refs(node: Any, pack: ContextPack) -> Any:
    """Replace the context's own prompt and instructions with references to it."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "content" and v == pack.prompt:
                out[k] = PROMPT_REF
            elif k == "instructions" and v == pack.instructions:
                out[k] = INSTRUCTIONS_REF
            else:
                out[k] = _refs(v, pack)
        return out
    if isinstance(node, list):
        if node and node[0] == pack.prompt:  # a prompt sent with attachments
            return [PROMPT_REF, *(_refs(v, pack) for v in node[1:])]
        return [_refs(v, pack) for v in node]
    return node


def _tool(t) -> dict:
    return {"name": t.name, "description": t.description,
            "parameters": t.parameters_json_schema, "strict": t.strict}


def capture(request_context, pack: ContextPack) -> dict:
    """The request as Pydantic AI sends it: model, settings, tools, output schema and turns."""
    params = request_context.model_request_parameters
    obj = params.output_object
    messages = ModelMessagesTypeAdapter.dump_python(request_context.messages, mode="json")
    return {
        "context_hash": pack.context_hash,
        "model": request_context.model.model_name,
        "provider": request_context.model.system,
        "settings": _plain(dict(request_context.model_settings or {})),
        "tools": [_tool(t) for t in params.function_tools],
        "native_tools": [_plain(t) for t in params.native_tools],
        "output": {
            "mode": params.output_mode,
            "tools": [_tool(t) for t in params.output_tools],
            "object": None if obj is None else {
                "name": obj.name, "schema": obj.json_schema, "strict": obj.strict},
        },
        "messages": _refs(_strip_binary(messages), pack),
    }


class RequestStore:
    def __init__(self, events: EventLog) -> None:
        self._events = events
        self._conn = events._conn
        with events._transaction():
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS contexts (
                    context_hash TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    request_hash TEXT PRIMARY KEY, context_hash TEXT NOT NULL,
                    body TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS contexts_no_update BEFORE UPDATE ON contexts
                BEGIN SELECT RAISE(ABORT, 'stored contexts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS contexts_no_delete BEFORE DELETE ON contexts
                BEGIN SELECT RAISE(ABORT, 'stored contexts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS requests_no_update BEFORE UPDATE ON requests
                BEGIN SELECT RAISE(ABORT, 'stored requests are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS requests_no_delete BEFORE DELETE ON requests
                BEGIN SELECT RAISE(ABORT, 'stored requests are append-only'); END;
            """)

    def store_context(self, pack: ContextPack, attachments: list[dict] = ()) -> str:
        body = {
            "role": pack.role, "topic": pack.topic, "instructions": pack.instructions,
            "prompt": pack.prompt, "fact_version": pack.fact_version, "inputs": pack.inputs,
            "sections": [s.model_dump(mode="json", exclude={"text"}) for s in pack.sections],
            "attachments": list(attachments),
        }
        with self._events._transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO contexts (context_hash, body) VALUES (?, ?)",
                (pack.context_hash, json.dumps(body)),
            )
        return pack.context_hash

    def store_request(self, body: dict) -> str:
        request_hash = digest(body)
        with self._events._transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO requests (request_hash, context_hash, body) "
                "VALUES (?, ?, ?)",
                (request_hash, body["context_hash"], json.dumps(body)),
            )
        return request_hash

    def context(self, context_hash: str) -> dict | None:
        row = self._conn.execute(
            "SELECT body FROM contexts WHERE context_hash = ?", (context_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def request(self, request_hash: str) -> dict | None:
        row = self._conn.execute(
            "SELECT body FROM requests WHERE request_hash = ?", (request_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def count(self) -> tuple[int, int]:
        contexts = self._conn.execute("SELECT COUNT(*) FROM contexts").fetchone()[0]
        requests = self._conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        return contexts, requests


def _expand(node: Any, context: dict) -> Any:
    if node == PROMPT_REF:
        return context["prompt"]
    if node == INSTRUCTIONS_REF:
        return context["instructions"]
    if isinstance(node, dict):
        return {k: _expand(v, context) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v, context) for v in node]
    return node


def inspect_call(ws, event_id: int) -> dict:
    """Everything one model call saw, rebuilt from the stored request and context."""
    found = [e for e in ws.events.list(since_id=event_id - 1) if e.id == event_id]
    if not found or found[0].type != "model.call":
        raise LookupError(f"Event {event_id} is not a model call")
    call = found[0]
    p = call.payload
    context = ws.requests.context(p["context_hash"]) if p.get("context_hash") else None
    request = ws.requests.request(p["request_hash"]) if p.get("request_hash") else None
    if context is None:
        raise LookupError(f"Model call {event_id} has no stored context (an image call, or a "
                          "run recorded before requests were stored)")
    turns = []
    if request is not None:
        # The first request turn is the context itself; later turns are tool calls and retries.
        for message in request["messages"]:
            if message.get("kind") == "request" and any(
                    part.get("content") == PROMPT_REF
                    or (isinstance(part.get("content"), list)
                        and part["content"][:1] == [PROMPT_REF])
                    for part in message.get("parts", [])):
                continue
            turns.append(_expand(message, context))
    return {
        "event_id": call.id,
        "ts": call.ts,
        "loop_id": call.loop_id,
        "sim_day": call.sim_day,
        "call": p,
        "role": context["role"],
        "topic": context.get("topic"),
        "model": (request or {}).get("model", p.get("model")),
        "provider": (request or {}).get("provider"),
        "settings": (request or {}).get("settings", {}),
        "instructions": context["instructions"],
        "prompt": context["prompt"],
        "sections": context.get("sections", []),
        "attachments": context.get("attachments", []),
        "tools": (request or {}).get("tools", []),
        "native_tools": (request or {}).get("native_tools", []),
        "output": (request or {}).get("output"),
        "turns": turns,
        "request_hash": p.get("request_hash"),
        "context_hash": p.get("context_hash"),
    }


def render(seen: dict) -> str:
    """`pgt inspect-call` output: the request in reading order."""
    call = seen["call"]
    lines = [
        f"Model call {seen['event_id']} · {seen['role']} · {seen['model']}"
        + (f" ({seen['provider']})" if seen.get("provider") else ""),
        f"Loop {seen['loop_id']} · day {seen['sim_day']} · topic {seen.get('topic') or '-'}",
        f"Input {call.get('input_tokens', 0):,} tokens ({call.get('cached_tokens', 0):,} cached) "
        f"· output {call.get('output_tokens', 0):,} · ${call.get('cost', 0):.4f}"
        + (f" · failure: {call['failure']}" if call.get("failure") else ""),
        f"Context hash {seen['context_hash']}",
        f"Request hash {seen.get('request_hash') or '-'}",
        "",
        "## Sections",
    ]
    for s in seen["sections"]:
        trimmed = f", trimmed {s['dropped']} item(s)" if s.get("dropped") else ""
        lines.append(f"- {s['name']} ({s['place']}, {'must keep' if s['must_keep'] else 'may trim'}"
                     f"): {s['tokens']:,} tokens"
                     + (f" of {s['budget']:,}" if s.get("budget") else "") + trimmed)
    lines += ["", "## Instructions", seen["instructions"], "", "## Prompt", seen["prompt"]]
    if seen["attachments"]:
        lines += ["", "## Attachments"]
        lines += [f"- {a.get('media_type')} {a.get('bytes', 0):,} bytes sha256 {a.get('sha256')}"
                  for a in seen["attachments"]]
    if seen["tools"] or seen["native_tools"]:
        lines += ["", "## Tools"]
        lines += [f"- {t['name']}: {t.get('description') or ''}" for t in seen["tools"]]
        lines += [f"- hosted {t.get('type', t)}" for t in seen["native_tools"]]
    output = seen.get("output") or {}
    if output:
        lines += ["", f"## Output ({output.get('mode')})"]
        schema = (output.get("object") or {}).get("schema")
        if schema is None and output.get("tools"):
            schema = output["tools"][0]["parameters"]
        if schema is not None:
            lines.append(json.dumps(schema, indent=1)[:4000])
    if seen["settings"]:
        lines += ["", "## Settings", json.dumps(seen["settings"], sort_keys=True)]
    if seen["turns"]:
        lines += ["", "## Turns after the prompt"]
        for turn in seen["turns"]:
            for part in turn.get("parts", []):
                kind = part.get("part_kind", "")
                body = part.get("content", part.get("args", ""))
                text = body if isinstance(body, str) else json.dumps(body)
                name = part.get("tool_name")
                lines.append(f"- {turn.get('kind')} {kind}{f' {name}' if name else ''}: "
                             f"{text[:600]}")
    return "\n".join(lines)
