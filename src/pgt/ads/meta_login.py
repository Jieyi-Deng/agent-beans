"""Facebook Login for Business, for a local install only.

The owner signs in on facebook.com, picks the ad account and grants access there. Agent Beans never sees
a password; it gets a one-time code back on its own callback URL, checks the `state` it sent, and
swaps the code for a token. The token stays in memory and is never written to the workspace.

Needs META_APP_ID, META_APP_SECRET and META_LOGIN_CONFIG_ID (a Login for Business configuration
that asks for ads_read, ads_mcp_management for reads through the Ads MCP, and ads_management for
L2). Off on the public demo, which runs on the labelled sandbox instead.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.parse
import urllib.request

GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v23.0")


def settings() -> dict | None:
    app_id = os.environ.get("META_APP_ID")
    config_id = os.environ.get("META_LOGIN_CONFIG_ID")
    if not (app_id and config_id and os.environ.get("META_APP_SECRET")):
        return None
    return {"app_id": app_id, "config_id": config_id}


def unavailable(public: bool) -> str | None:
    """Why the real login cannot run here, or None when it can."""
    if public:
        return ("The public demo runs on the sandbox account only. Connecting a real ad account "
                "works when you run Agent Beans on your own machine.")
    if settings() is None:
        return ("Set META_APP_ID, META_APP_SECRET and META_LOGIN_CONFIG_ID for your Meta app to "
                "turn this on.")
    return None


def new_state() -> str:
    return secrets.token_urlsafe(24)


def login_url(redirect_uri: str, state: str) -> str:
    conf = settings()
    if conf is None:
        raise RuntimeError("Meta login is not configured")
    query = urllib.parse.urlencode({
        "client_id": conf["app_id"],
        "config_id": conf["config_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "override_default_response_type": "true",
    })
    return f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?{query}"


def _get(path: str, params: dict) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as res:
        return json.loads(res.read())


def exchange(code: str, redirect_uri: str) -> str:
    """Swap the one-time code for a user access token."""
    conf = settings()
    if conf is None:
        raise RuntimeError("Meta login is not configured")
    data = _get("oauth/access_token", {
        "client_id": conf["app_id"],
        "client_secret": os.environ["META_APP_SECRET"],
        "redirect_uri": redirect_uri,
        "code": code,
    })
    return data["access_token"]


def ad_accounts(token: str) -> list[dict]:
    """The ad accounts the owner granted."""
    data = _get("me/adaccounts", {
        "fields": "account_id,name,currency,timezone_name", "access_token": token,
    })
    return [{"kind": "meta", "account_id": f"act_{a['account_id']}", "name": a.get("name", ""),
             "currency": a.get("currency", ""), "timezone": a.get("timezone_name", "")}
            for a in data.get("data", [])]
