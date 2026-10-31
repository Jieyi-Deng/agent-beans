"""Creative Strategist: looks at what the market is doing (hosted web search) and turns it into
angles and hooks. Every market claim carries a source link."""

from __future__ import annotations

from pydantic_ai.capabilities import WebSearch
from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.harness.policy import check_tool
from pgt.llm.runtime import RoleRun, Scope, run_role_full
from pgt.schemas import MarketResearch, SharedFacts, Source, WeeklyBrief

INSTRUCTIONS = """\
You are the Creative Strategist on a small AI growth team that runs Meta ads for one advertiser.
You study how competitors and the category advertise, then suggest angles and hooks worth
testing. Search the web for current examples. Every claim about the market needs a source link
you actually found; never invent a URL. Respect the brand voice and the constraints in the facts."""

# Each search adds its results to the context, so a long search spree gets expensive fast:
# 22 searches once cost $0.54 in one call. Eight is enough for three to five sourced insights.
MAX_SEARCHES = 8


async def run_research(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    week: int,
    brief: WeeklyBrief,
    issues: list[str] = (),
) -> RoleRun[MarketResearch]:
    data = {"week": week, "focus": brief.focus, "summary": brief.summary}
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "strategist",
        INSTRUCTIONS,
        facts,
        fact_version,
        task="Find three to five market insights for this week's focus: what similar apps say in "
        "their ads, which angles they lean on, and which hooks look fresh for our audience.",
        done_when="Each insight has a one-sentence claim, an angle, a short hook and at least one "
        "source with a real http(s) URL from your search."
        + (" Every review issue is fixed." if issues else ""),
        data=data,
    )
    capabilities, settings = [], None
    if check_tool("strategist", "web_search", scope.ws.events, scope.loop_id) is None:
        capabilities.append(WebSearch(search_context_size="low", local=False))
        settings = {"extra_body": {"max_tool_calls": MAX_SEARCHES}}
    run = await run_role_full(
        "strategist", MarketResearch, pack, scope, model, capabilities=capabilities,
        settings=settings,
    )
    return _fill_sources(run)


def _fill_sources(run: RoleRun[MarketResearch]) -> RoleRun[MarketResearch]:
    """If the model left an insight without sources but the search returned citations, attach
    the citations, so links come from the search rather than the model's memory."""
    if not run.citations:
        return run
    cited = [Source(title=title, url=url) for title, url in run.citations]
    insights = [
        i if i.sources else i.model_copy(update={"sources": cited}) for i in run.output.insights
    ]
    run.output = MarketResearch(insights=insights)
    return run
