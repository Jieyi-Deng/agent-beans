"""Acceptance tests for harness storage, the policy gate and checks."""

import sqlite3

import pytest

from pgt.harness.hashing import content_hash
from pgt.harness.policy import (
    ALLOWLIST,
    RULES,
    CostMeter,
    SpendCapExceeded,
    check_tool,
    gate_change_set,
)
from pgt.harness.verify import check_changes, check_findings, number_matches
from pgt.harness.workspace import Workspace
from pgt.schemas import (
    AccountState,
    AdState,
    CampaignState,
    Change,
    ChangeSet,
    ChangeSetDraft,
    Evidence,
    Finding,
    Findings,
    HouseRules,
    InsightRow,
    SharedFacts,
)


def facts(**overrides) -> SharedFacts:
    values = {
        "product": "Demo app",
        "audience": "Adults",
        "goal": "free-trial starts",
        "result_name": "trial",
        "target_cost_per_result": 4.0,
        "weekly_budget": 385.0,
        "brand_voice": "Plain",
        "house_rules": HouseRules.from_preset("balanced", daily_budget_cap=60.0),
    }
    values.update(overrides)
    return SharedFacts(**values)


def state(day=1, last_change=0, in_learning=True, budget=40.0, spend=5.0) -> AccountState:
    return AccountState(
        day=day,
        currency="USD",
        campaigns=[
            CampaignState(
                campaign_id="cmp_1",
                name="Broad",
                status="ACTIVE",
                objective="APP_PROMOTION",
                daily_budget=budget,
                in_learning=in_learning,
                results_since_edit=10,
                last_budget_change_day=last_change,
                test_id=None,
            ),
            CampaignState(
                campaign_id="cmp_2",
                name="Test",
                status="ACTIVE",
                objective="APP_PROMOTION",
                daily_budget=15.0,
                in_learning=False,
                results_since_edit=60,
                last_budget_change_day=None,
                test_id="test_1",
            ),
        ],
        ads=[
            AdState(
                ad_id="ad_1",
                campaign_id="cmp_1",
                name="Streak",
                status="ACTIVE",
                created_day=0,
                lifetime_spend=spend,
                lifetime_results=2,
            ),
            AdState(
                ad_id="ad_5",
                campaign_id="cmp_1",
                name="Streak v2",
                status="PAUSED",
                created_day=0,
                lifetime_spend=0.0,
                lifetime_results=0,
            ),
        ],
    )


def budget(target="cmp_1", new=60.0) -> Change:
    return Change(
        kind="set_budget",
        target_id=target,
        new_daily_budget=new,
        reason="r",
        evidence_ids=["ev-d1-ad_1"],
    )


def pause(target="ad_1") -> Change:
    return Change(
        kind="pause_ad",
        target_id=target,
        new_daily_budget=None,
        reason="r",
        evidence_ids=["ev-d1-ad_1"],
    )


def failed(result) -> dict[str, str]:
    return {r.rule: r.reason for r in result.results if not r.passed}


def draft(summary="Hold") -> ChangeSetDraft:
    return ChangeSetDraft(summary=summary, changes=[])


@pytest.fixture
def ws():
    workspace = Workspace()
    yield workspace
    workspace.close()


# --- Event log -----------------------------------------------------------------------------


def test_event_log_append_only_and_filters(ws):
    first = ws.events.append("tool.call", "analyst", {"name": "x"}, sim_day=1, loop_id="d1")
    ws.events.append("tool.blocked", "analyst", {"name": "y"}, loop_id="d1")
    ws.events.append("artifact.published", "system", loop_id="d2")
    assert first.id == 1 and first.ts
    assert [e.type for e in ws.events.list("tool.")] == ["tool.call", "tool.blocked"]
    assert len(ws.events.list("tool.call")) == 1
    assert len(ws.events.list(loop_id="d1")) == 2
    assert [e.id for e in ws.events.list(since_id=2)] == [3]
    conn = ws.events._conn
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET type = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events")


def test_workspace_reopens_from_file(tmp_path):
    path = tmp_path / "ws.sqlite"
    ws = Workspace(path)
    ws.facts.set(facts(), "user")
    record = ws.artifacts.publish("day1/brief", draft(), "media_buyer", sim_day=1)
    ws.ledger.add(Evidence(evidence_id="ev-1", kind="derived", values={"x": 1.0}))
    ws.close()
    again = Workspace(path)
    assert again.facts.version == 1 and again.facts.current() == facts()
    assert again.artifacts.get(record.artifact_id) == record
    assert again.ledger.has("ev-1")
    assert len(again.events.list()) >= 2
    again.close()


# --- Hashing and artifacts -----------------------------------------------------------------


def test_content_hash_is_canonical():
    a = content_hash({"b": 1, "a": [1, 2]})
    assert a == content_hash({"a": [1, 2], "b": 1}) and len(a) == 64
    assert content_hash(draft("x")) != content_hash(draft("y"))
    assert content_hash({"a": 1, "gate": 2}, exclude={"gate"}) == content_hash({"a": 1})


def test_publish_versions_and_staleness(ws):
    findings = ws.artifacts.publish("w1/findings", Findings(findings=[]), "analyst")
    plan = ws.artifacts.publish(
        "w1/plan", draft("plan"), "growth_lead", inputs=[findings.artifact_id]
    )
    cs = ws.artifacts.publish(
        "w1/change_set", draft("cs"), "media_buyer", inputs=[plan.artifact_id]
    )
    other = ws.artifacts.publish("w1/insights", draft("other"), "strategist")
    assert findings.artifact_id == "w1/findings@v1" and findings.kind == "Findings"
    assert findings.content_hash == content_hash(Findings(findings=[]))

    v2 = ws.artifacts.publish("w1/findings", Findings(findings=[]), "analyst")
    assert v2.version == 2 and v2.artifact_id == "w1/findings@v2"
    assert ws.artifacts.get("w1/findings@v1").status == "superseded"
    assert ws.artifacts.get(plan.artifact_id).status == "stale"
    assert ws.artifacts.get(cs.artifact_id).status == "stale"
    assert ws.artifacts.get(other.artifact_id).status == "draft"
    assert ws.artifacts.latest("w1/findings") == v2
    assert len(ws.events.list("artifact.stale")) == 2


def test_evidence_ids_collected(ws):
    finding = Finding(
        subject_id="ad_1",
        claim="c",
        metric="ctr",
        value=0.01,
        evidence_ids=["ev-a", "ev-b"],
        severity="act",
    )
    record = ws.artifacts.publish("d1/findings", Findings(findings=[finding]), "analyst")
    assert sorted(record.evidence_ids) == ["ev-a", "ev-b"]


# --- Facts ----------------------------------------------------------------------------------


def test_fact_change_marks_only_dependents_stale(ws):
    v1 = ws.facts.set(facts(), "user")
    assert v1 == 1
    findings = ws.artifacts.publish(
        "w1/findings", Findings(findings=[]), "analyst", fact_version=1, fact_keys=["product"]
    )
    research = ws.artifacts.publish(
        "w1/insights", draft("r"), "strategist", fact_version=1, fact_keys=["audience"]
    )
    plan = ws.artifacts.publish(
        "w1/plan",
        draft("p"),
        "growth_lead",
        inputs=[findings.artifact_id],
        fact_version=1,
        fact_keys=["weekly_budget"],
    )
    cs = ws.artifacts.publish("w1/change_set", draft("c"), "media_buyer", inputs=[plan.artifact_id])

    v2 = ws.facts.set(facts(weekly_budget=210.0), "user")
    assert (
        v2 == 2
        and ws.facts.current().weekly_budget == 210.0
        and ws.facts.get(1).weekly_budget == 385.0
    )
    changed = ws.events.list("fact.changed")[-1]
    assert changed.payload == {"version": 2, "changed": ["weekly_budget"]}
    assert ws.artifacts.get(plan.artifact_id).status == "stale"
    assert ws.artifacts.get(cs.artifact_id).status == "stale"
    assert ws.artifacts.get(findings.artifact_id).status == "draft"
    assert ws.artifacts.get(research.artifact_id).status == "draft"

    assert ws.facts.set(facts(weekly_budget=210.0), "user") == 2


def test_only_user_or_lead_set_facts(ws):
    with pytest.raises(LookupError):
        ws.facts.current()
    with pytest.raises(PermissionError):
        ws.facts.set(facts(), "media_buyer")
    assert ws.events.list("fact.rejected")
    assert ws.facts.set(facts(), "growth_lead") == 1


# --- Ledger and verification --------------------------------------------------------------


def row(day=1, ad="ad_1") -> InsightRow:
    return InsightRow(
        evidence_id=f"ev-d{day}-{ad}",
        day=day,
        ad_id=ad,
        campaign_id="cmp_1",
        spend=20.0,
        impressions=2000,
        reach=1500,
        clicks=40,
        results=0,
        ctr=0.02,
        cost_per_result=None,
        frequency=1.33,
    )


def test_ledger(ws):
    assert ws.ledger.add_rows([row()]) == ["ev-d1-ad_1"]
    stored = ws.ledger.get("ev-d1-ad_1")
    assert stored.kind == "insight" and stored.values["ctr"] == 0.02
    assert "cost_per_result" not in stored.values and "day" not in stored.values
    ws.ledger.add_rows([row()])
    derived = Evidence(
        evidence_id="ev-x", kind="derived", values={"ctr_change": -0.38}, inputs=["ev-d1-ad_1"]
    )
    ws.ledger.add(derived)
    ws.ledger.add(derived)
    with pytest.raises(ValueError):
        ws.ledger.add(Evidence(evidence_id="ev-x", kind="derived", values={"ctr_change": -0.5}))


def test_number_matching():
    assert number_matches(-0.38, [-0.3801])
    assert number_matches(-38, [-0.38])
    assert number_matches(1.2, [0.0123])  # "1.2%" for a CTR of 0.0123
    assert not number_matches(-50, [-0.38])
    assert not number_matches(0.016, [0.012])


def test_check_findings_and_changes(ws):
    ws.ledger.add(Evidence(evidence_id="ev-x", kind="derived", values={"ctr_change": -0.38}))

    def finding(value, ids):
        return Finding(
            subject_id="ad_1", claim="c", metric="m", value=value, evidence_ids=ids, severity="act"
        )

    assert check_findings([finding(-0.38, ["ev-x"]), finding(-38, ["ev-x"])], ws.ledger) == []
    assert len(check_findings([finding(-0.5, ["ev-x"])], ws.ledger)) == 1
    assert len(check_findings([finding(-0.38, [])], ws.ledger)) == 1
    assert len(check_findings([finding(-0.38, ["ev-nope"])], ws.ledger)) >= 1
    ok = budget()
    ok.evidence_ids = ["ev-x"]
    assert check_changes([ok], ws.ledger) == []
    bare = budget()
    bare.evidence_ids = []
    assert len(check_changes([bare], ws.ledger)) == 1
    assert len(check_changes([budget()], ws.ledger)) == 1  # ev-d1-ad_1 not in this ledger


# --- Policy: tools and spend -----------------------------------------------------------------


def test_tool_allowlist(ws):
    assert "get_insights" in ALLOWLIST["analyst"]
    assert check_tool("analyst", "get_insights", ws.events) is None
    error = check_tool("analyst", "set_campaign_budget", ws.events, loop_id="d1")
    assert error and "set_campaign_budget" in error
    assert check_tool("intruder", "get_insights", ws.events)
    blocked = ws.events.list("tool.blocked")
    assert len(blocked) == 2 and blocked[0].payload["tool"] == "set_campaign_budget"


def test_cost_meter(ws):
    meter = CostMeter(ws.events, per_call=0.10, per_role=0.25, per_loop=0.40)
    meter.record("analyst", "d1", 0.05, input_tokens=100)
    meter.record("analyst", "d1", 0.05)
    assert meter.spent("analyst", "d1") == pytest.approx(0.10)
    with pytest.raises(SpendCapExceeded):
        meter.record("analyst", "d1", 0.20)
    with pytest.raises(SpendCapExceeded):
        meter.record("media_buyer", "d1", 0.11)
    assert len(ws.events.list("model.cost")) == 4
    assert len(ws.events.list("budget.exceeded")) == 2


def test_total_cap_refuses_the_next_call(ws):
    """A live workspace's total cap: the call that crosses it is recorded, the next is refused."""
    meter = CostMeter(ws.events, total=0.10)
    meter.check("analyst", "d1")
    meter.record("analyst", "d1", 0.06)
    with pytest.raises(SpendCapExceeded):
        meter.record("reviewer", "d2", 0.05)
    with pytest.raises(SpendCapExceeded, match="spend cap"):
        meter.check("analyst", "d3")
    assert meter.remaining() == 0.0
    caps = [e.payload["cap"] for e in ws.events.list("budget.exceeded")]
    assert caps == ["total", "total"]
    assert CostMeter(ws.events).remaining() is None


# --- Policy: house rules ---------------------------------------------------------------------


def test_rules_are_listed():
    names = [rule.name for rule in RULES]
    assert names == [
        "target_exists",
        "min_days_between_budget_changes",
        "max_budget_change",
        "learning_phase_protection",
        "min_spend_before_pause",
        "daily_budget_cap",
    ]


def test_acceptance_7_learning_phase_guard():
    """A +50% budget increase on a campaign changed yesterday is blocked, with reasons."""
    [result] = gate_change_set([budget(new=60.0)], state(day=1, last_change=0), facts())
    assert not result.allowed
    reasons = failed(result)
    assert set(reasons) >= {
        "min_days_between_budget_changes",
        "max_budget_change",
        "learning_phase_protection",
    }
    assert (
        "1" in reasons["min_days_between_budget_changes"]
        and "3" in reasons["min_days_between_budget_changes"]
    )
    assert "50%" in reasons["max_budget_change"] and "20%" in reasons["max_budget_change"]
    assert "learning" in reasons["learning_phase_protection"].lower()


def test_small_change_allowed_when_rules_met():
    [result] = gate_change_set(
        [budget(new=44.0)], state(day=5, last_change=1, in_learning=False), facts()
    )
    assert result.allowed and failed(result) == {}
    assert {r.rule for r in result.results} == {
        "target_exists",
        "min_days_between_budget_changes",
        "max_budget_change",
        "learning_phase_protection",
        "daily_budget_cap",
    }


def test_presets_change_outcomes():
    aggressive = facts(house_rules=HouseRules.from_preset("aggressive", daily_budget_cap=100.0))
    current = state(day=3, last_change=1, in_learning=True)
    [result] = gate_change_set(
        [budget(new=50.0)], current, aggressive
    )  # +25%, 2 days, learning off
    assert result.allowed
    [result] = gate_change_set([budget(new=50.0)], current, facts())
    assert not result.allowed


def test_daily_cap_uses_projection():
    current = state(day=9, last_change=None, in_learning=False, budget=40.0)
    rules = HouseRules.from_preset("aggressive", daily_budget_cap=60.0)
    results = gate_change_set(
        [budget("cmp_2", 19.5), budget("cmp_1", 44.0)], current, facts(house_rules=rules)
    )
    assert results[0].allowed  # 40 + 19.5 = 59.5
    assert not results[1].allowed and "daily_budget_cap" in failed(results[1])  # 44 + 19.5 = 63.5
    assert current.campaign("cmp_2").daily_budget == 15.0  # input not mutated


def test_second_budget_change_same_day_blocked():
    current = state(day=9, last_change=None, in_learning=False)
    results = gate_change_set([budget(new=44.0), budget(new=46.0)], current, facts())
    assert results[0].allowed and not results[1].allowed
    assert "min_days_between_budget_changes" in failed(results[1])


def test_pause_needs_min_spend():
    [low] = gate_change_set([pause()], state(spend=5.0), facts())
    assert not low.allowed and "$8" in failed(low)["min_spend_before_pause"]
    [high] = gate_change_set([pause()], state(spend=9.0), facts())
    assert high.allowed


def test_unknown_target_skips_other_rules():
    [result] = gate_change_set([pause("ad_404")], state(), facts())
    assert not result.allowed
    assert [r.rule for r in result.results] == ["target_exists"]
    assert "ad_404" in result.results[0].reason
    [result] = gate_change_set([budget("ad_1", 50.0)], state(), facts())
    assert not result.allowed


# --- Approvals --------------------------------------------------------------------------------


def change_set(changes, gate=None) -> ChangeSet:
    return ChangeSet(
        change_set_id="cs-1",
        day=1,
        author="media_buyer",
        summary="s",
        changes=changes,
        gate=gate or [],
    )


def test_acceptance_6_edit_after_approval_voids_it(ws):
    record = ws.artifacts.publish("d1/change_set", change_set([pause()]), "media_buyer")
    ws.approvals.grant("d1/change_set", record)
    assert ws.approvals.is_valid("d1/change_set")
    assert ws.approvals.approved_indexes("d1/change_set") == [0]

    edited = change_set([pause(), budget(new=44.0)])
    ws.artifacts.publish("d1/change_set", edited, "media_buyer")
    assert not ws.approvals.is_valid("d1/change_set")
    assert not ws.approvals.is_valid("d1/change_set")
    assert ws.approvals.approved_indexes("d1/change_set") == []
    assert len(ws.events.list("approval.voided")) == 1


def test_override_needs_second_confirmation(ws):
    gate = gate_change_set([budget(new=60.0)], state(day=1, last_change=0), facts())
    record = ws.artifacts.publish(
        "d1/change_set", change_set([budget(new=60.0)], gate), "media_buyer"
    )
    broken = sorted(failed(gate[0]))
    with pytest.raises(PermissionError):
        ws.approvals.grant("d1/change_set", record)
    with pytest.raises(PermissionError):
        ws.approvals.grant("d1/change_set", record, override_rules=broken)
    with pytest.raises(PermissionError):
        ws.approvals.grant(
            "d1/change_set", record, override_rules=broken[:1], confirm_override=True
        )
    with pytest.raises(PermissionError):
        ws.approvals.grant("d1/change_set", record, actor="media_buyer")
    event = ws.approvals.grant(
        "d1/change_set", record, override_rules=broken, confirm_override=True
    )
    assert event.type == "approval.granted" and ws.approvals.is_valid("d1/change_set")
    # Approving only the allowed subset needs no override.
    ws.approvals.grant("d1/change_set", record, approved=[])
    assert ws.approvals.approved_indexes("d1/change_set") == []


def test_a_reader_holding_the_file_open_never_blocks_the_team(tmp_path):
    """Someone browsing the workspace file mid-run (a viewer, a script) must not fail a day."""
    path = tmp_path / "workspace.db"
    ws = Workspace(path)
    ws.events.append("tool.call", "analyst", {"name": "x"})
    reader = sqlite3.connect(path, timeout=0)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM events").fetchone() == (1,)
    ws.events.append("tool.call", "analyst", {"name": "y"})  # a rollback journal would lock here
    reader.rollback()
    assert reader.execute("SELECT count(*) FROM events").fetchone() == (2,)
    reader.close()
    ws.close()
