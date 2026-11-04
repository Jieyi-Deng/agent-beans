"""Reads through Meta's official Ads MCP server (https://mcp.facebook.com/ads).

A small synchronous MCP client: JSON-RPC 2.0 over streamable HTTP, with the owner's user access
token as a Bearer token (the Login for Business configuration needs `ads_mcp_management`). The
transport is injectable so tests run against a fake server and never reach the network.

Meta publishes the tool names but not their parameters, so the arguments are built from the input
schema the server sends in `tools/list`. Every reply is checked for the fields Agent Beans needs; a read
the MCP cannot answer in full raises McpError, and the account reads it from the Graph API instead.
Writes never go through here: the Operator writes through the Graph API under the L2 rules.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import urllib.error
import urllib.request
from collections.abc import Callable

MCP_URL = os.environ.get("META_ADS_MCP_URL", "https://mcp.facebook.com/ads")
PROTOCOL_VERSION = "2025-06-18"
ENTITIES_TOOL = "ads_get_ad_entities"

# (url, headers, body) -> (JSON-RPC reply or None for a notification, response headers)
McpTransport = Callable[[str, dict, dict], tuple[dict | None, dict]]


class McpError(RuntimeError):
    """The MCP call failed, or its reply lacked what Agent Beans needs. The read falls back to Graph."""


class McpUnsupported(McpError):
    """The server cannot answer this read at all; Agent Beans stops asking for the rest of the session."""


def _sse_message(text: str, want_id) -> dict | None:
    for event in text.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines()
                         if line.startswith("data:"))
        if not data:
            continue
        message = json.loads(data)
        if message.get("id") == want_id and ("result" in message or "error" in message):
            return message
    return None


def urllib_transport(url: str, headers: dict, body: dict) -> tuple[dict | None, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as res:
            head = {k.lower(): v for k, v in res.headers.items()}
            text = res.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise McpUnsupported("The Meta Ads MCP refused the sign-in. The Login for Business "
                                 "configuration needs the ads_mcp_management permission.") from None
        raise McpError(f"The Meta Ads MCP returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise McpError(f"The Meta Ads MCP could not be reached ({type(exc).__name__})") from None
    if "id" not in body or not text.strip():
        return None, head
    if head.get("content-type", "").startswith("text/event-stream"):
        return _sse_message(text, body["id"]), head
    return json.loads(text), head


class AdsMcp:
    """One MCP session. Nothing is sent until the first call."""

    def __init__(self, token: str, transport: McpTransport | None = None,
                 url: str = MCP_URL) -> None:
        self._token = token
        self._transport = transport or urllib_transport
        self._url = url
        self._session: str | None = None
        self._ready = False
        self._next_id = 0
        self._tools: dict[str, dict] | None = None

    def _send(self, method: str, params: dict | None = None, notify: bool = False):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self._token}"}
        if self._ready:
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        body: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._next_id += 1
            body["id"] = self._next_id
        reply, head = self._transport(self._url, headers, body)
        self._session = head.get("mcp-session-id", self._session)
        if notify:
            return None
        if not reply:
            raise McpError(f"The Meta Ads MCP sent no reply to {method}")
        if "error" in reply:
            message = (reply["error"] or {}).get("message") or "error"
            raise McpError(f"The Meta Ads MCP refused {method}: {self._scrub(message)}")
        return reply.get("result") or {}

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[token]") if self._token else text

    def _start(self) -> None:
        if self._ready:
            return
        self._send("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                  "clientInfo": {"name": "agent-beans", "version": "0.1"}})
        self._ready = True
        self._send("notifications/initialized", notify=True)

    def tools(self) -> dict[str, dict]:
        """Every tool the server offers, by name, with its input schema."""
        if self._tools is None:
            self._start()
            tools, cursor = {}, None
            while True:
                page = self._send("tools/list", {"cursor": cursor} if cursor else {})
                tools.update({t["name"]: t for t in page.get("tools", [])})
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            self._tools = tools
        return self._tools

    def call(self, name: str, arguments: dict):
        """Call a tool and return its parsed reply."""
        self._start()
        result = self._send("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result.get("content", [])
                         if c.get("type") == "text")
        if result.get("isError"):
            raise McpError(f"{name} failed: {self._scrub(text)[:300] or 'no detail'}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            raise McpError(f"{name} did not reply with data") from None


# --- the reads Agent Beans makes ---------------------------------------------------------------------

# Agent Beans' name for each argument, then the names the server's schema may use for it.
_ARG_NAMES = {
    "account": ("ad_account_id", "account_id", "act_id"),
    "level": ("level", "entity_level", "entity_type"),
    "fields": ("fields", "metrics"),
    "date_preset": ("date_preset",),
    "time_range": ("time_range", "date_range"),
    "time_increment": ("time_increment",),
    "filtering": ("filtering", "filters"),
    "limit": ("limit",),
    "object_state": ("object_state",),
}
# Optional arguments the official server documents, sent when its schema has them. Reads are
# plain lookups: no added benchmarks, only the fields asked for.
_EXTRAS = {"include_additional_context": False, "advertiser_job": "report_measure_or_look_up"}
_MAX_PAGES = 50
_ROW_KEYS = ("ad_entities", "data", "entities", "results", "rows", "items")
_ALIASES = {"adset_id": ("ad_set_id",), "date_start": ("date", "day_start"),
            "effective_status": ("status",), "spend": ("amount_spent",)}


def _decoded(value):
    """Some replies carry their rows as a JSON string."""
    if isinstance(value, str) and value.lstrip().startswith(("[", "{")):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _next_cursor(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    paging = payload.get("pagination")
    cursor = paging.get("next_cursor") if isinstance(paging, dict) else None
    return cursor or payload.get("next_cursor") or None


def _norm(word: str) -> str:
    return word.lower().replace("_", "").replace("-", "")


class McpReads:
    """The four reads a real account needs, answered from `ads_get_ad_entities`.

    Rows come back in the Graph API's shape, so MetaAccount maps both sources the same way."""

    def __init__(self, mcp: AdsMcp, account_id: str, result_action: str) -> None:
        self._mcp = mcp
        self.account_id = account_id.removeprefix("act_")  # the MCP lists accounts without it
        self._result_action = result_action
        # The server asks every call to carry one id per conversation (20 letters and digits).
        alphabet = string.ascii_letters + string.digits
        self._conversation = "".join(secrets.choice(alphabet) for _ in range(20))

    def tools(self) -> dict[str, dict]:
        return self._mcp.tools()

    def _schema(self) -> dict:
        tool = self._mcp.tools().get(ENTITIES_TOOL)
        if tool is None:
            raise McpUnsupported(f"The Meta Ads MCP does not offer {ENTITIES_TOOL}")
        return tool.get("inputSchema") or {}

    def _args(self, need: tuple[str, ...], purpose: str = "", **want) -> dict:
        schema = self._schema()
        props = schema.get("properties") or {}
        args, used = {}, set()
        for key, value in want.items():
            names = _ARG_NAMES[key]
            name = next((n for n in names if n in props), None if props else names[0])
            if name is None:
                if key in need:
                    raise McpUnsupported(f"{ENTITIES_TOOL} takes no {key} argument")
                continue
            args[name] = self._fit(props.get(name) or {}, value)
            used.add(name)
        extras = {**_EXTRAS, "client_conversation_id": self._conversation}
        if purpose:
            extras["reporting_purpose"] = [purpose]
        for name, value in extras.items():
            if name in props:
                args[name] = self._fit(props[name], value)
                used.add(name)
        missing = [r for r in schema.get("required", []) if r not in used]
        if missing:
            raise McpUnsupported(f"{ENTITIES_TOOL} needs {', '.join(missing)}, which Agent Beans "
                                 "does not know how to fill")
        return args

    @staticmethod
    def _fit(prop: dict, value):
        """Shape a value to the schema: enum spelling, list or comma string, JSON string."""
        kind = prop.get("type")
        if prop.get("enum") and isinstance(value, str):
            match = [e for e in prop["enum"] if isinstance(e, str) and _norm(e) == _norm(value)]
            if not match:
                raise McpUnsupported(f"{ENTITIES_TOOL} has no {value} option")
            return match[0]
        if isinstance(value, list) and kind == "string":
            return ",".join(map(str, value)) if all(isinstance(v, str) for v in value) \
                else json.dumps(value)
        if isinstance(value, dict) and kind == "string":
            return json.dumps(value)
        if isinstance(value, int) and kind == "string":
            return str(value)
        return value

    def _rows(self, args: dict, page=None) -> list[dict]:
        rows: list[dict] = []
        page = page or self._page
        can_page = "cursor" in (self._schema().get("properties") or {})
        for _ in range(_MAX_PAGES):
            payload = self._mcp.call(ENTITIES_TOOL, args)
            rows += page(payload)
            cursor = _next_cursor(payload)
            if not cursor or not can_page:
                return rows
            if isinstance(payload, dict) and isinstance(payload.get("client_conversation_id"), str):
                self._conversation = payload["client_conversation_id"]
                args = {**args, "client_conversation_id": self._conversation}
            args = {**args, "cursor": cursor}
        raise McpError(f"{ENTITIES_TOOL} returned more than {_MAX_PAGES} pages")

    def _page(self, payload) -> list[dict]:
        if isinstance(payload, dict):
            values = {k: _decoded(v) for k, v in payload.items()}
            found = next((values[k] for k in _ROW_KEYS if isinstance(values.get(k), list)),
                         None)
            if found is None:
                found = next((v for v in values.values() if isinstance(v, list)), None)
            payload = found
        if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
            raise McpError(f"{ENTITIES_TOOL} replied in a shape Agent Beans does not read")
        return [self._flat(r) for r in payload]

    def _flat(self, row: dict) -> dict:
        row = dict(row)
        for nested in ("metrics", "insights"):
            inner = row.pop(nested, None)
            if isinstance(inner, dict) and isinstance(inner.get("data"), list):
                inner = inner["data"][0] if inner["data"] else {}
            if isinstance(inner, dict):
                row = {**inner, **row}
        for key, value in row.items():  # a metric may come as {"value": ..., "unit": ...}
            if isinstance(value, dict) and "value" in value:
                row[key] = value["value"]
        for key, others in _ALIASES.items():
            found = next((o for o in others if o in row), None)
            if key not in row and found:
                row[key] = row[found]
        actions = row.get("actions")
        if isinstance(actions, dict):
            row["actions"] = [{"action_type": k, "value": v} for k, v in actions.items()]
        elif actions is None:
            flat = row.get(f"actions:{self._result_action}", row.get(self._result_action))
            if flat is not None:
                row["actions"] = [{"action_type": self._result_action, "value": flat}]
        return row

    @staticmethod
    def _require(rows: list[dict], what: str, *fields: str) -> None:
        """Each field (or one of a|b) must appear in the reply wherever it can."""
        for field in fields:
            options = field.split("|")
            if rows and not any(o in r for r in rows for o in options):
                raise McpError(f"{ENTITIES_TOOL} did not return {field} for {what}")

    def _has_results(self, rows: list[dict], what: str) -> None:
        spent = any(float(r.get("spend") or 0) > 0 for r in rows)
        if spent and not any("actions" in r for r in rows):
            raise McpError(f"{ENTITIES_TOOL} did not return results for {what}")

    def adsets(self) -> list[dict]:
        args = self._args(("account", "level"), "entity_lookup", account=self.account_id,
                          level="adset",
                          fields=["id", "name", "status", "effective_status", "daily_budget",
                                  "lifetime_budget", "learning_stage_info", "campaign_id",
                                  "campaign"])
        rows = self._rows(args)
        self._require(rows, "ad sets", "id", "effective_status",
                      "daily_budget|lifetime_budget|campaign")
        if any(r.get("effective_status") == "ACTIVE" for r in rows):
            self._require(rows, "ad sets", "learning_stage_info")
        for r in rows:  # an ad set on its campaign's budget needs that budget too
            if "daily_budget" not in r and "lifetime_budget" not in r \
                    and not isinstance(r.get("campaign"), dict):
                raise McpError(f"{ENTITIES_TOOL} did not return the budget for ad set {r['id']}")
        return rows

    def ads(self) -> list[dict]:
        args = self._args(("account", "level"), "entity_lookup", account=self.account_id,
                          level="ad", fields=["id", "name", "adset_id", "status", "created_time"])
        rows = self._rows(args)
        for r in rows:
            r.setdefault("status", r.get("effective_status"))
        self._require(rows, "ads", "id", "adset_id", "status", "created_time")
        return rows

    def lifetime(self) -> list[dict]:
        args = self._args(("account", "level", "date_preset"), "metric_lookup",
                          account=self.account_id,
                          level="ad", date_preset="maximum", fields=["ad_id", "spend", "actions"])
        rows = self._rows(args)
        for r in rows:
            r.setdefault("ad_id", r.get("id"))
        self._require(rows, "lifetime totals", "ad_id", "spend")
        self._has_results(rows, "lifetime totals")
        return rows

    def daily(self, since: str, until: str) -> list[dict]:
        args = self._args(("account", "level", "time_range", "time_increment"),
                          "metric_lookup", account=self.account_id, level="ad",
                          time_range={"since": since, "until": until}, time_increment=1,
                          fields=["ad_id", "adset_id", "spend", "impressions", "reach", "clicks",
                                  "actions"])
        rows = self._rows(args)
        for r in rows:
            r.setdefault("ad_id", r.get("id"))
        self._require(rows, "daily insights", "date_start", "ad_id", "adset_id", "spend",
                      "impressions", "reach")
        self._has_results(rows, "daily insights")
        return rows

    def drafts(self) -> list[dict]:
        """Unpublished changes staged in Ads Manager, as the server nests them. A draft has not
        run, so there are no metrics to ask for."""
        args = self._args(("account", "object_state"), "entity_lookup", account=self.account_id,
                          object_state="draft")
        return self._rows(args, _draft_page)


def _draft_page(payload) -> list[dict]:
    """Drafts come back under `ad_drafts`; live entities there mean the draft read was ignored."""
    if not isinstance(payload, dict):
        raise McpError(f"{ENTITIES_TOOL} replied to the draft read in a shape Agent Beans does not read")
    values = {k: _decoded(v) for k, v in payload.items()}
    found = values.get("ad_drafts")
    if isinstance(found, dict):
        found = [found]
    if isinstance(found, list):
        return [d for d in found if isinstance(d, dict)]
    if "ad_entities" in values:
        raise McpError(f"{ENTITIES_TOOL} returned live entities instead of drafts")
    if "ad_drafts" in values and found is None:
        return []
    raise McpError(f"{ENTITIES_TOOL} did not return ad_drafts")


# Where a child list sits tells its level when the node does not say.
_DRAFT_LEVELS = {"campaigns": "campaign", "adsets": "ad set", "ad_sets": "ad set", "ads": "ad"}
_NEXT_LEVEL = {"account": "campaign", "campaign": "ad set", "ad set": "ad", "ad": "ad"}
_DRAFT_IDS = ("id", "draft_id", "object_id", "campaign_id", "adset_id", "ad_id")
_DRAFT_SKIP = {"name", "status", "effective_status", "level", "object_type", "entity_type"}
_MAX_DETAILS = 12


def _draft_level(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    word = _norm(raw)
    return {"campaign": "campaign", "adset": "ad set", "ad": "ad"}.get(word.removesuffix("s"))


def draft_node(node: dict, level: str = "campaign") -> dict:
    """One draft in a fixed shape the app shows: its id, name, level, status, the plain values
    it carries and its children. Nested objects (targeting, creative specs) are only named."""
    level = (_draft_level(node.get("level")) or _draft_level(node.get("object_type"))
             or _draft_level(node.get("entity_type")) or level)
    children: list[dict] = []
    details: dict = {}
    nested: list[str] = []
    for key, value in node.items():
        value = _decoded(value)
        if key in _DRAFT_SKIP:
            continue
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            child = _DRAFT_LEVELS.get(key, _NEXT_LEVEL[level])
            children += [draft_node(v, child) for v in value]
        elif isinstance(value, dict) and "value" in value:
            details[key] = value["value"]
        elif isinstance(value, dict | list):
            nested.append(key)
        elif value not in (None, "") and len(details) < _MAX_DETAILS:
            details[key] = value
    ident = next((str(node[k]) for k in _DRAFT_IDS if node.get(k) not in (None, "")), "")
    details.pop("id", None)
    return {"id": ident, "name": str(node.get("name") or ""), "level": level,
            "status": node.get("status") or node.get("effective_status") or None,
            "details": details, "nested": nested, "children": children}


def draft_counts(drafts: list[dict]) -> dict[str, int]:
    """How many campaigns, ad sets and ads a draft tree holds."""
    counts = {"campaign": 0, "ad set": 0, "ad": 0}
    stack = list(drafts)
    while stack:
        node = stack.pop()
        counts[node["level"]] = counts.get(node["level"], 0) + 1
        stack += node["children"]
    return counts
