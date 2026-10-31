"""Muse Spark (open weights) on Baseten, through Pydantic AI's Chat Completions model.

Baseten serves an OpenAI-compatible Chat Completions API. It supports strict JSON-schema output,
function tools (auto and forced), image input and `reasoning_effort`, so every text role can run
on it. It has no hosted web search, so the Creative Strategist stays on the Meta Responses API.
The reasoning text comes back in `reasoning_content`, and `completion_tokens` already counts the
reasoning tokens, so the usual output price covers them."""

from __future__ import annotations

import os

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

BASETEN_BASE_URL = "https://inference.baseten.co/v1"
BASETEN_MODEL = "meta-models/muse-spark-open-1026"
KEY_VARS = ("BASETEN_API_KEY", "Baseten_API")
EFFORTS = ("low", "medium", "high", "xhigh")
# Recorded runs never needed more than about 4,500 output tokens in one call. Without a limit a
# runaway answer once ran to the 32,768-token default, twice, at four minutes each.
MAX_OUTPUT = 12_000


def baseten_client() -> AsyncOpenAI:
    """Client for the live API. Reads the key from the environment and never prints it."""
    key = next((os.environ[v] for v in KEY_VARS if os.environ.get(v)), None)
    if not key:
        raise RuntimeError("BASETEN_API_KEY is not set. Put it in .env (see .env.example).")
    return AsyncOpenAI(
        api_key=key, base_url=os.environ.get("BASETEN_BASE_URL", BASETEN_BASE_URL), max_retries=2
    )


def chat_model(
    client: AsyncOpenAI, model: str = BASETEN_MODEL, effort: str = "medium"
) -> OpenAIChatModel:
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
    return OpenAIChatModel(
        model,
        provider=OpenAIProvider(openai_client=client),
        profile=OpenAIModelProfile(
            openai_chat_thinking_field="reasoning_content",
        ),
        settings=OpenAIChatModelSettings(openai_reasoning_effort=effort, max_tokens=MAX_OUTPUT),
    )
