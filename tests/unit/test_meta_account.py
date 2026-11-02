"""The real-account adapter and the L2 write rules, against a fake Graph API (no network)."""

from datetime import date

import pytest

from pgt.ads.graph import Graph, GraphError
from pgt.ads.meta_account import MetaAccount, WriteRefused
from pgt.harness.workspace import Workspace
from pgt.roles.operator import apply_change_set
from pgt.schemas import Change, ChangeSet, CreativeSet, GateResult
from pgt.schemas.weekly import CreativeCopy, CreativeImage

TOKEN = "test-token-never-shown"
ORIGIN = date(2026, 10, 23)  # day 1; "today" is Oct 30, so day 7 is the newest complete day


class FakeGraph:
    """Answers the Marketing API calls Agent Beans makes, and records every write."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.next_id = 900
        self.adsets = [
            {"id": "111", "name": "Prospecting US", "status": "ACTIVE",
             "effective_status": "ACTIVE", "daily_budget": "5000",
             "learning_stage_info": {"status": "SUCCESS", "conversions": 61},
             "campaign": {"id": "c1", "objective": "OUTCOME_APP_PROMOTION"}},
            {"id": "222", "name": "Advantage campaign budget", "status": "ACTIVE",
             "effective_status": "ACTIVE", "learning_stage_info": {"status": "LEARNING"},
             "campaign": {"id": "c2", "objective": "OUTCOME_SALES", "daily_budget": "8000"}},
            {"id": "333", "name": "Out of scope", "status": "ACTIVE",
             "effective_status": "ACTIVE", "daily_budget": "1000",
             "campaign": {"id": "c3", "objective": "OUTCOME_SALES"}},
        ]
        self.ads = [
            {"id": "a1", "name": "Hook A", "adset_id": "111", "status": "ACTIVE",
             "created_time": "2026-10-20T10:00:00+0000"},
            {"id": "a2", "name": "Hook B", "adset_id": "111", "status": "PAUSED",
             "created_time": "2026-10-25T10:00:00+0000"},
            {"id": "a9", "name": "Elsewhere", "adset_id": "333", "status": "ACTIVE",
             "created_time": "2026-10-01T10:00:00+0000"},
        ]

    def __call__(self, method, url, params):
        assert params.pop("access_token") == TOKEN
        path = url.split("/v23.0/", 1)[1]
        if method == "POST":
            self.posts.append((path, params))
            if path.endswith("/adimages"):
                return {"images": {"bytes": {"hash": "img-hash-1"}}}
            if path.endswith(("/adcreatives", "/ads")):
                self.next_id += 1
                return {"id": str(self.next_id)}
            if path == "bad-ad":
                return {"error": {"message": "Invalid parameter", "code": 100}}
            return {"success": True}
        if path == "act_42":
            return {"currency": "USD"}
        if path in {a["id"] for a in self.adsets}:  # the writer checks the live ad set
            return next(a for a in self.adsets if a["id"] == path)
        if path == "act_42/adsets":
            return {"data": self.adsets}
        if path == "act_42/ads":
            return {"data": self.ads}
        if path == "act_42/insights" and params.get("date_preset") == "maximum":
            return {"data": [{"ad_id": "a1", "spend": "210.50",
                              "actions": [{"action_type": "mobile_app_install", "value": "30"}]}]}
        if path == "act_42/insights":
            assert params["time_increment"] == 1
            return {"data": [
                {"date_start": "2026-10-23", "ad_id": "a1", "adset_id": "111", "spend": "40",
                 "impressions": "2000", "reach": "1000", "clicks": "50",
                 "actions": [{"action_type": "mobile_app_install", "value": "4"},
                             {"action_type": "link_click", "value": "50"}]},
                {"date_start": "2026-10-24", "ad_id": "a1", "adset_id": "111", "spend": "45",
                 "impressions": "3000", "reach": "1000", "clicks": "60", "actions": []},
                {"date_start": "2026-10-24", "ad_id": "a9", "adset_id": "333", "spend": "9",
                 "impressions": "100", "reach": "100", "clicks": "1"},
            ]}
        raise AssertionError(f"unexpected call {method} {path}")


def account(fake=None, **kw) -> MetaAccount:
    kw.setdefault("adset_ids", ["111", "222"])
    return MetaAccount(Graph(TOKEN, fake or FakeGraph()), "42", ORIGIN,
                       today=lambda: date(2026, 10, 30), **kw)


def test_ad_sets_read_as_campaigns_within_scope():
    acc = account()
    state = acc.state()
    assert acc.day == 7 and state.day == 7 and state.currency == "USD"
    assert [c.campaign_id for c in state.campaigns] == ["111", "222"]
    own, cbo = state.campaigns
    assert own.daily_budget == 50.0 and not own.in_learning and own.results_since_edit == 61
    assert cbo.daily_budget == 80.0 and cbo.in_learning
    assert [a.ad_id for a in state.ads] == ["a1", "a2"]  # a9 is in an ad set out of scope
    a1 = state.ad("a1")
    assert a1.lifetime_spend == 210.5 and a1.lifetime_results == 30 and a1.created_day == -2


def test_daily_rows_count_days_from_the_origin():
    rows = account().insights()
    assert [(r.day, r.ad_id) for r in rows] == [(1, "a1"), (2, "a1")]
    first, second = rows
    assert first.evidence_id == "ev-d1-a1" and first.results == 4  # installs, not link clicks
    assert first.cost_per_result == 10.0 and first.ctr == 0.025
    assert second.results == 0 and second.cost_per_result is None
    assert second.frequency == 2.5  # cumulative: 5000 impressions over 2000 reached
    assert account().insights(2, 2)[0].day == 2


def test_budget_change_goes_to_an_ad_set_that_owns_its_budget():
    fake = FakeGraph()
    acc = account(fake)
    acc.writer().set_campaign_budget("111", 55.0)
    assert fake.posts == [("111", {"daily_budget": "5500"})]
    assert acc.state().campaign("111").last_budget_change_day == 7
    with pytest.raises(WriteRefused, match="Advantage campaign budget"):
        acc.writer().set_campaign_budget("222", 70.0)
    with pytest.raises(WriteRefused, match="not in the team's scope"):
        acc.writer().set_campaign_budget("333", 12.0)
    assert len(fake.posts) == 1


def test_ads_can_be_paused_but_never_turned_on():
    fake = FakeGraph()
    acc = account(fake)
    acc.writer().set_ad_status("a1", "PAUSED")
    with pytest.raises(WriteRefused, match="Ads Manager"):
        acc.writer().set_ad_status("a2", "ACTIVE")
    assert fake.posts == [("a1", {"status": "PAUSED"})]


def creative(tmp_path) -> CreativeSet:
    image = tmp_path / "square.png"
    image.write_bytes(b"\x89PNG fake")
    return CreativeSet(
        creative_id="w1-t1-c1", test_id="w1-t1", attempt=1,
        ad_copy=CreativeCopy(name="Progress photo v1", headline="See your week",
                             primary_text="Track every session.", description="Free trial",
                             claims=[], image_prompt="a calm gym"),
        images=[CreativeImage(ratio="9:16", width=1080, height=1920, path="missing.png",
                              sha256="x"),
                CreativeImage(ratio="1:1", width=1080, height=1080, path=str(image),
                              sha256="y")],
        question_ids=[],
    )


def test_new_ads_are_created_paused_from_the_reviewed_creative(tmp_path):
    fake = FakeGraph()
    acc = account(fake, page_id="p1", link_url="https://example.com/app")
    ad_id = acc.writer().create_ad("111", "w1-t1 Progress photo v1", creative(tmp_path))
    paths = [p for p, _ in fake.posts]
    assert paths == ["act_42/adimages", "act_42/adcreatives", "act_42/ads"]
    spec = fake.posts[1][1]["object_story_spec"]
    assert spec["page_id"] == "p1" and spec["link_data"]["image_hash"] == "img-hash-1"
    assert spec["link_data"]["name"] == "See your week"
    ad = fake.posts[2][1]
    assert ad["status"] == "PAUSED" and ad["adset_id"] == "111"
    assert ad["creative"] == {"creative_id": "901"} and ad_id == "902"
    assert acc.changes()[-1]["kind"] == "ad_created"


def test_new_ads_need_a_page_and_a_link(tmp_path):
    fake = FakeGraph()
    with pytest.raises(WriteRefused, match="META_PAGE_ID"):
        account(fake).writer().create_ad("111", "x", creative(tmp_path))
    assert fake.posts == []


def test_errors_carry_metas_message_but_never_the_token():
    graph = Graph(TOKEN, FakeGraph())
    with pytest.raises(GraphError) as caught:
        graph.post("bad-ad", status="PAUSED")
    assert "Invalid parameter" in str(caught.value) and TOKEN not in str(caught.value)


def test_operator_records_refusals_and_applies_the_rest(tmp_path):
    fake = FakeGraph()
    writes = []
    acc = account(fake, page_id="p1", link_url="https://example.com/app",
                  on_write=writes.append)
    ws = Workspace()
    made = creative(tmp_path)
    source = ws.artifacts.publish("week1/creative/w1-t1", made, "creative_producer")
    from pgt.schemas import AdDraft

    def change(kind, target, budget=None):
        return Change(kind=kind, target_id=target, new_daily_budget=budget, reason="r",
                      evidence_ids=[])

    cs = ChangeSet(
        change_set_id="cs-d7", day=7, author="media_buyer", summary="s",
        changes=[change("set_budget", "111", 55.0), change("set_budget", "222", 70.0),
                 change("activate_ad", "a2"), change("pause_ad", "a1")],
        gate=[GateResult(change_index=i, allowed=True, results=[]) for i in range(4)],
        ad_drafts=[AdDraft(campaign_id="111", name="w1-t1 Progress photo v1",
                           creative_id="w1-t1-c1", test_id="w1-t1", evidence_ids=[])],
    )
    ws.artifacts.publish("day7/change_set", cs, "media_buyer", inputs=[source.artifact_id])
    ws.approvals.grant("day7/change_set", ws.artifacts.latest("day7/change_set"), actor="user")

    receipt = apply_change_set(ws, acc, "day7/change_set")

    assert receipt.applied == [0, 3] and receipt.skipped == [1, 2]
    assert receipt.created_ad_ids == ["902"]
    assert len(receipt.errors) == 2 and "Advantage campaign budget" in receipt.errors[0]
    assert "never turns ads on" in receipt.errors[1]
    assert [w["kind"] for w in writes] == ["campaign_budget", "ad_status", "ad_created"]
    assert ws.events.list("changeset.applied")[-1].payload["errors"] == receipt.errors
