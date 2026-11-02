"""Which ad account the team works on, and how much it may do there.

Both are events, so every change shows in the room's history. Only the owner may change them.
The API checks the level before it accepts an approval, and the Operator checks it again in code
before it writes anything.

  L0 Observe               reads results and writes findings; asks for nothing
  L1 Propose               drafts plans and change sets; the owner applies them in Ads Manager
  L2 Apply after approval  the Operator writes exactly what was approved; new ads start paused

On a real Meta account L2 is narrower still: new ads are created paused for the owner to turn on
in Ads Manager, an ad can be paused but never turned on, and a budget changes only after approval
and the house rules. See pgt.ads.meta_account.
"""

from __future__ import annotations

from pgt.harness.workspace import Workspace

OBSERVE, PROPOSE, APPLY = 0, 1, 2
DEFAULT_LEVEL = APPLY  # the sandbox demo runs the whole loop

LEVELS = [
    {"level": OBSERVE, "code": "L0", "name": "Observe",
     "text": "Reads results every morning and writes findings. Proposes nothing.",
     "scopes": ["ads_read"]},
    {"level": PROPOSE, "code": "L1", "name": "Propose",
     "text": "Drafts test plans, creative and change sets for you to approve. Nothing reaches the "
             "account; you apply approved changes yourself in Ads Manager.",
     "scopes": ["ads_read"]},
    {"level": APPLY, "code": "L2", "name": "Apply after approval",
     "text": "The Operator writes exactly what you approved, nothing else. New ads are always "
             "created paused; you turn them on in Ads Manager.",
     "scopes": ["ads_read", "ads_management"]},
]

SANDBOX = {
    "kind": "sandbox", "account_id": "act_sandbox_0001", "name": "Beans' Active sandbox",
    "currency": "USD", "timezone": "America/New_York",
}


def _last(ws: Workspace, *types: str):
    found = [e for t in types for e in ws.events.list(t)]
    return max(found, key=lambda e: e.id) if found else None


def connection(ws: Workspace) -> dict | None:
    """The connected account, or None before the first run is set up or after a disconnect."""
    e = _last(ws, "account.connected", "account.disconnected")
    if e is None or e.type == "account.disconnected":
        return None
    return {**e.payload, "ts": e.ts, "by": e.actor}


def level(ws: Workspace) -> int:
    e = _last(ws, "access.level")
    return DEFAULT_LEVEL if e is None else int(e.payload["level"])


def describe(n: int) -> str:
    item = LEVELS[n]
    return f"{item['code']} {item['name']}"


def _owner_only(ws: Workspace, actor: str, what: str) -> None:
    if actor != "user":
        ws.events.append("access.rejected", actor, {"actor": actor, "what": what})
        raise PermissionError(f"Only the owner may {what}")


def set_level(ws: Workspace, n: int, actor: str = "user", sim_day: int | None = None) -> None:
    _owner_only(ws, actor, "change how much the team may do")
    if n not in (OBSERVE, PROPOSE, APPLY):
        raise ValueError(f"Unknown level {n}")
    if n != level(ws) or _last(ws, "access.level") is None:
        ws.events.append("access.level", actor, {"level": n, "from": level(ws)}, sim_day=sim_day)


def connect(ws: Workspace, account: dict, n: int, actor: str = "user",
            sim_day: int | None = None) -> None:
    _owner_only(ws, actor, "connect an ad account")
    with ws.events._transaction():
        ws.events.append("account.connected", actor, {**account, "level": n},
                         sim_day=sim_day)
        set_level(ws, n, actor, sim_day)


def disconnect(ws: Workspace, actor: str = "user", sim_day: int | None = None) -> None:
    _owner_only(ws, actor, "disconnect the ad account")
    current = connection(ws)
    if current is not None:
        ws.events.append("account.disconnected", actor,
                         {"account_id": current.get("account_id")}, sim_day=sim_day)


def can_apply(ws: Workspace) -> bool:
    return level(ws) >= APPLY


def can_propose(ws: Workspace) -> bool:
    return level(ws) >= PROPOSE
