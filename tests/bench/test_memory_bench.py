"""The memory safeguard scenarios, offline, on one seed: every planted memory mistake is caught,
and each scenario's mistake really was made (the planted text reached the harness)."""

import asyncio

import pytest

from bench.memory import BY_KEY, run_one


@pytest.mark.parametrize("key", list(BY_KEY))
def test_each_memory_safeguard_holds(key):
    g = asyncio.run(run_one(BY_KEY[key], 7))
    assert g.passed, g.why
    assert g.weeks_closed == 4 and g.lines_proposed >= 1


def test_the_lesson_that_hurt_quality_reached_the_prompt_before_it_was_rolled_back():
    g = asyncio.run(run_one(BY_KEY["m3"], 7))
    assert g.planted["prompts_with_lesson"] >= 5 and g.rolled_back == 1
