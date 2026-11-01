"""An offline stand-in for the Meta Model API, used by tests and offline demo runs.

It serves the Responses API over an in-memory transport, so requests go through the same
client code as live runs, and it checks every request against Meta's rules.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel


@dataclass
class FunctionCall:
    """Return this from a responder to make the fake model call a tool."""

    name: str
    arguments: dict


@dataclass
class RawText:
    """Return this for a plain-text answer, such as SAM 3.1's segmentation output."""

    text: str


@dataclass
class Searched:
    """Return this to make the fake model run hosted web searches before answering.
    Each source becomes a url_citation annotation on the answer."""

    output: BaseModel | dict
    queries: list[str]
    sources: list[tuple[str, str]]  # (title, url)


Responder = Callable[[dict], "BaseModel | dict | FunctionCall | RawText | Searched"]
ImageResponder = Callable[[dict], bytes]  # request body -> PNG bytes

META_MODELS = {"muse-spark-1.3", "muse-wonder-1.0", "sam-3.1"}
IMAGE_MODELS = {"muse-image-1.0"}


def check_meta_request(body: dict) -> list[str]:
    """Return the Meta Responses API rules this request breaks (the subset PGT relies on)."""
    violations: list[str] = []
    if body.get("tool_choice", "auto") != "auto":
        violations.append("tool_choice must be auto")
    for tool in body.get("tools", []):
        if tool.get("type") not in {"function", "web_search"}:
            violations.append(f"Unsupported hosted tool: {tool.get('type')}")
    if body.get("model", "").removesuffix("-contributor") not in META_MODELS:
        violations.append("Unsupported Meta model")
    fmt = body.get("text", {}).get("format", {})
    if fmt.get("type") == "json_schema" and fmt.get("strict"):
        schema = fmt.get("schema", {})
        if schema.get("type") != "object":
            violations.append("Strict schema root must be an object")
        _check_schema(schema, 1, violations)
        for definition in schema.get("$defs", {}).values():
            _check_schema(definition, 1, violations)
    return list(dict.fromkeys(violations))


def _check_schema(node: Any, depth: int, violations: list[str]) -> None:
    if not isinstance(node, dict):
        return
    if depth > 10:
        violations.append("Strict schema nesting exceeds 10")
        return
    for keyword in ("allOf", "oneOf"):
        if keyword in node:
            violations.append(f"Strict schema cannot use {keyword}")
    if node.get("type") == "object" or "properties" in node:
        if node.get("additionalProperties") is not False:
            violations.append("Every object needs additionalProperties: false")
        if not set(node.get("properties", {})) <= set(node.get("required", [])):
            violations.append("Every object property must be required")
        for child in node.get("properties", {}).values():
            _check_schema(child, depth + 1, violations)
    for child in node.get("anyOf", []):
        _check_schema(child, depth, violations)
    _check_schema(node.get("items"), depth + 1, violations)


def prompt_inputs(body: dict) -> dict:
    """Pull the JSON inputs block that the context assembler puts in every prompt."""
    raw = body.get("input", "")
    text = raw if isinstance(raw, str) else _input_text(body)
    start = text.find("```json\n")
    end = text.find("\n```", start + 8)
    return json.loads(text[start + 8 : end]) if start >= 0 and end > start else {}


def _input_text(body: dict) -> str:
    parts = []
    for item in body.get("input", []):
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [c.get("text", "") for c in content if isinstance(c, dict)]
    return "\n".join(parts)


IMAGE_TOKENS = 1_500  # rough flat cost per attached image, instead of its base64 length
_DATA_URL = re.compile(rb'"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+"')


def _input_tokens(content: bytes) -> int:
    images = len(_DATA_URL.findall(content))
    return len(_DATA_URL.sub(b'""', content)) // 4 + images * IMAGE_TOKENS


CACHE_MIN_TOKENS, CACHE_BLOCK = 1_024, 128  # like the providers: whole blocks past a minimum


def _prefix_text(body: dict) -> str:
    """What a provider's prompt cache keys on: tools, output format, instructions, then input,
    with images reduced to a digest."""
    head = json.dumps({"tools": body.get("tools", []), "text": body.get("text", {}),
                       "instructions": body.get("instructions", "")}, sort_keys=True)
    rest = json.dumps(body.get("input", ""), sort_keys=True)
    return head + "\x00" + _DATA_URL.sub(b'"image"', rest.encode()).decode()


def _common(a: str, b: str) -> int:
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def output_name(body: dict) -> str:
    return body.get("text", {}).get("format", {}).get("name", "")


def form_fields(request: httpx.Request) -> dict:
    """The fields of a multipart image-edit request; the reference image comes back as bytes
    under "image"."""
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
        + request.read()
    )
    body: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        body[name] = data if part.get_filename() else data.decode()
    return body


class MetaFake:
    """Serves POST /v1/responses, /v1/images/generations and /v1/images/edits.
    `responder(body)` returns the output for each Responses request; `image_responder(body)`
    returns PNG bytes for each image request (an edit's body carries the reference under
    "image")."""

    def __init__(self, responder: Responder, image_responder: ImageResponder | None = None) -> None:
        self.responder = responder
        self.image_responder = image_responder
        self.requests: list[dict] = []
        self.violations: list[str] = []
        self.calls: list[dict] = []  # model, input/output tokens and searches per served call
        self._count = 0
        self._seen: dict[str, list[str]] = {}  # recent request prefixes per model

    def _cached_tokens(self, body: dict, input_tokens: int) -> int:
        """Simulate the providers' prefix cache: the longest prefix shared with a recent request
        to the same model, in whole blocks once it passes the minimum."""
        text = _prefix_text(body)
        seen = self._seen.setdefault(body.get("model", ""), [])
        shared = max((_common(text, prev) for prev in seen), default=0)
        seen.append(text)
        del seen[:-24]
        cached = shared // 4
        cached = cached // CACHE_BLOCK * CACHE_BLOCK if cached >= CACHE_MIN_TOKENS else 0
        return min(cached, input_tokens)

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url="https://api.meta.ai/v1",
            api_key="offline",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._respond)),
        )

    def _respond(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/images/edits":
            body = form_fields(request)
            self.requests.append({k: v for k, v in body.items() if k != "image"})
            if not body.get("image"):
                self.violations.append("Image edits need a reference image")
            return self._image(body)
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        if request.method == "POST" and request.url.path == "/v1/images/generations":
            return self._image(body)
        if request.method != "POST" or request.url.path != "/v1/responses":
            self.violations.append(f"Unsupported endpoint: {request.url.path}")
            return httpx.Response(404, json={"error": {"message": "Unknown endpoint"}})
        self.violations.extend(check_meta_request(body))
        self._count += 1
        obj = self.responder(body)
        output: list[dict] = []
        annotations: list[dict] = []
        if isinstance(obj, Searched):
            limit = body.get("max_tool_calls") or len(obj.queries)  # Meta honours this cap
            for query in obj.queries[:limit]:
                output.append(
                    {
                        "type": "web_search_call",
                        "id": f"ws_{self._count}_{len(output)}",
                        "status": "completed",
                        "action": {"type": "search", "query": query},
                    }
                )
            annotations = [
                {
                    "type": "url_citation",
                    "start_index": 0,
                    "end_index": 1,
                    "url": url,
                    "title": title,
                }
                for title, url in obj.sources
            ]
            obj = obj.output
        if isinstance(obj, FunctionCall):
            text = json.dumps(obj.arguments)
            item = {
                "type": "function_call",
                "id": f"fc_{self._count}",
                "call_id": f"call_{self._count}",
                "name": obj.name,
                "arguments": text,
                "status": "completed",
            }
        else:
            if isinstance(obj, RawText):
                text = obj.text
            else:
                text = json.dumps(
                    obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
                )
            item = {
                "type": "message",
                "id": f"msg_{self._count}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": annotations}],
            }
        output.append(item)
        # Rough token counts so offline runs still show plausible costs.
        input_tokens = _input_tokens(request.content)
        output_tokens = len(text) // 4
        cached_tokens = self._cached_tokens(body, input_tokens)
        searches = sum(1 for o in output if o["type"] == "web_search_call")
        self.calls.append(
            {
                "model": body.get("model"),
                "input_tokens": input_tokens,
                "cached_tokens": cached_tokens,
                "output_tokens": output_tokens,
                "searches": searches,
            }
        )
        return httpx.Response(
            200,
            json={
                "id": f"resp_{self._count}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": body.get("model"),
                "output": output,
                "parallel_tool_calls": True,
                "tool_choice": body.get("tool_choice", "auto"),
                "tools": body.get("tools", []),
                "text": body.get("text", {"format": {"type": "text"}}),
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "input_tokens_details": {"cached_tokens": cached_tokens},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    def _image(self, body: dict) -> httpx.Response:
        if body.get("model") not in IMAGE_MODELS:
            self.violations.append("Unsupported Meta image model")
        if body.get("response_format") != "b64_json":
            self.violations.append("Image requests must ask for b64_json")
        if self.image_responder is None:
            self.violations.append("No image responder configured")
            return httpx.Response(404, json={"error": {"message": "No image model"}})
        png = self.image_responder(body)
        self.calls.append(
            {"model": body.get("model"), "input_tokens": 0, "output_tokens": 0, "searches": 0}
        )
        return httpx.Response(
            200, json={"created": 0, "data": [{"b64_json": base64.b64encode(png).decode()}]}
        )
