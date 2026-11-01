"""With-key smoke test: a role's second call reports cached input tokens on the live providers.

Skipped unless PGT_LIVE_SMOKE=1. It loads keys from .env without printing them and usually
spends under one cent per provider (two short calls each). The cap is $0.25 because a reasoning
model sometimes thinks for a few thousand tokens on even a short task.
"""

import asyncio
import os

import pytest

from pgt.harness.context import assemble
from pgt.harness.policy import CostMeter
from pgt.harness.workspace import Workspace
from pgt.llm.runtime import Scope, run_role
from pgt.sandbox import fitness_app_facts
from pgt.schemas import Findings

pytestmark = pytest.mark.skipif(not os.environ.get("PGT_LIVE_SMOKE"),
                                reason="live smoke test: set PGT_LIVE_SMOKE=1")

# Long enough that the shared prefix passes the providers' 1,024-token cache minimum.
INSTRUCTIONS = "You are the Analyst. " + " ".join(
    f"Rule {n}: read every signal, copy numbers exactly, and cite the evidence ID." for n in
    range(1, 120))


def _pack(day: int):
    return assemble("analyst", INSTRUCTIONS, fitness_app_facts(), 1,
                    task="Write the findings. There are no signals today, so return none.",
                    done_when="findings is an empty list.", data={"day": day, "signals": []},
                    topic=f"day{day}/findings")


def _model(provider: str):
    from dotenv import load_dotenv

    load_dotenv(override=False)
    if provider == "meta":
        from pgt.llm.meta import meta_client, responses_model

        if not os.environ.get("MODEL_API_KEY"):
            pytest.skip("no Meta key")
        return responses_model(meta_client())
    from pgt.llm.baseten import KEY_VARS, baseten_client, chat_model

    if not any(os.environ.get(v) for v in KEY_VARS):
        pytest.skip("no Baseten key")
    return chat_model(baseten_client())


@pytest.mark.parametrize("provider", ["meta", "baseten"])
def test_second_call_reports_cached_tokens(provider):
    model = _model(provider)
    ws = Workspace()
    meter = CostMeter(ws.events, total=0.25)

    async def two_calls():  # one event loop: the provider's HTTP client is bound to it
        for day in (1, 2):
            scope = Scope(ws, meter, f"daily-d{day}", day, memory=False)
            await run_role("analyst", Findings, _pack(day), scope, model)

    asyncio.run(two_calls())
    calls = [e.payload for e in ws.events.list("model.call") if e.payload["failure"] is None]
    assert len(calls) >= 2
    assert calls[-1]["cached_tokens"] > 0, calls[-1]
