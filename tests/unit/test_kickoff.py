"""The Growth Lead's first message on a newly connected account, from what the team read."""

from pgt.loops.kickoff import kickoff_text

DRAFT = {"id": "d1", "name": "Spring launch", "level": "campaign", "status": None,
         "details": {}, "nested": [], "children": []}


def test_nothing_live_with_drafts_points_at_ads_manager():
    from pgt.schemas import AccountState

    state = AccountState(day=7, currency="USD", campaigns=[], ads=[])
    text = kickoff_text("Test", state, [DRAFT], None, {"ad_sets": "mcp", "drafts": "mcp"}, False)
    assert text.startswith("I read Test through Meta's Ads MCP. Nothing was changed.")
    assert "Nothing is live" in text and "1 campaign (Spring launch)" in text
    assert text.endswith("Daily checks start once something is live.")


def test_a_failed_draft_read_is_named():
    text = kickoff_text("Test", None, None, "rate limited", {"ad_sets": "graph"}, False)
    assert "through the Graph API" in text
    assert "I could not read the drafts in Ads Manager: rate limited. Next:" in text


def test_the_sandbox_is_named_as_synthetic_and_counts_campaigns():
    from pgt.sandbox import SandboxAccount, fitness_app_scenario

    account = SandboxAccount(fitness_app_scenario())
    text = kickoff_text("Beans' Active sandbox", account.state(), account.drafts(), None, None, False)
    assert text.startswith("I read Beans' Active sandbox, a synthetic account. Nothing was changed.")
    assert "Live: 2 campaigns with 5 ads, nothing spent yet." in text
    assert ("1 campaign, 1 ad set and 3 ads (New Year resolutions, Testimonial v2)" in text)
    assert text.endswith("what needs your decision.")
