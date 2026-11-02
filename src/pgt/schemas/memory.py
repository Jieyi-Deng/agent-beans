"""Deliverables of the memory calls. Strict schema rules: no defaults, every field required."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryLineDraft(_Out):
    id: str  # an existing line ID to change, or "new"
    text: str  # one short sentence
    sources: list[str]  # evidence IDs, week records (wk-N), journal entries (j-N), notes (n-N)


class MemoryPass(_Out):
    """A weekly memory update: merge, don't restate. Lines not mentioned stay as they are."""

    drop: list[str]  # IDs of current lines that no longer hold
    lines: list[MemoryLineDraft]  # new lines, and changed versions of existing ones
    summary: str  # one sentence: what changed and why


class LineVerdict(_Out):
    line_id: str
    verdict: Literal["keep", "narrow", "reject"]
    reason: str  # why not, in one sentence; empty on keep
    narrowed: str  # on narrow: the line cut down to what the sources support; empty otherwise
    # Whether following the line makes the team's checks, gates or habits stricter or looser.
    direction: Literal["tighten", "loosen", "neutral"]
    direction_reason: str  # why, in a few words


class MemoryCheck(_Out):
    """A check of new memory lines against the charter and their sources, by a role other than
    the one that wrote them (the Reviewer; the Analyst for the Reviewer's own lessons)."""

    verdicts: list[LineVerdict]


class NoteTriage(_Out):
    """What the Growth Lead does with an owner note that named no role."""

    action: Literal["handoff", "answer"]
    to: Literal["analyst", "media_buyer", "producer", "strategist", "reviewer", "growth_lead"]
    answer: str  # the reply to the owner when action is answer; a one-line handoff task otherwise
    reason: str
