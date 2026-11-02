"""A small Graph API client for the Marketing API calls Agent Beans makes.

The transport is injectable so tests run against a fake Graph and never reach the network.
Errors carry Meta's message but never the access token.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v23.0")

# (method, url, params) -> parsed JSON reply. Raises GraphError on an error reply.
Transport = Callable[[str, str, dict], dict]


class GraphError(RuntimeError):
    """An error reply from the Graph API, with Meta's own message."""


def _encode(params: dict) -> dict:
    return {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()
            if v is not None}


def urllib_transport(method: str, url: str, params: dict) -> dict:
    data = urllib.parse.urlencode(_encode(params))
    if method == "GET":
        request = urllib.request.Request(f"{url}?{data}")
    else:
        request = urllib.request.Request(url, data=data.encode(), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error", {})
        except (ValueError, AttributeError):
            error = {}
        raise GraphError(error.get("error_user_msg") or error.get("message")
                         or f"Meta returned HTTP {exc.code}") from None


class Graph:
    def __init__(self, token: str, transport: Transport | None = None,
                 version: str = GRAPH_VERSION) -> None:
        self._token = token
        self._transport = transport or urllib_transport
        self._base = f"https://graph.facebook.com/{version}"

    def _call(self, method: str, path: str, params: dict) -> dict:
        reply = self._transport(method, f"{self._base}/{path}",
                                {**params, "access_token": self._token})
        if isinstance(reply, dict) and "error" in reply:
            error = reply["error"] or {}
            raise GraphError(error.get("error_user_msg") or error.get("message") or "Meta error")
        return reply

    def get(self, path: str, **params) -> dict:
        return self._call("GET", path, params)

    def post(self, path: str, **params) -> dict:
        return self._call("POST", path, params)

    def rows(self, path: str, limit: int = 2000, **params) -> list[dict]:
        """Every row of a list edge, following Meta's cursor paging."""
        out: list[dict] = []
        params = {**params, "limit": 500}
        while True:
            reply = self.get(path, **params)
            out += reply.get("data", [])
            after = (reply.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not (reply.get("paging") or {}).get("next") or len(out) >= limit:
                return out
            params["after"] = after
