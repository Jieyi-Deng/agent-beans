# The harness

A model can write a good ad and still quote the wrong number, cite a statistic it never saw, follow an instruction hidden in a campaign name, or raise a budget the day after the last raise. Prompting reduces these mistakes; it cannot rule them out. PGT rules them out in code. The roles propose; the harness decides what is allowed to reach the owner and the ad account.

This page lists each guarantee, how it works, where it lives, and the test that proves it. The offline bench ([BENCH_RESULTS.md](BENCH_RESULTS.md)) removes one component at a time, and each ablation fails exactly the scenario that component exists for.

## Where the harness sits

```mermaid
flowchart TB
    subgraph before["Before the call"]
        direction LR
        ctx["Context assembly<br/>fixed order, hashed"]
        cap["Spend check<br/>refuse once the cap is used"]
    end
    subgraph during["During the call"]
        direction LR
        allow["Tool allowlist<br/>per role"]
        log["Log every model and tool call<br/>tokens, cost, context hash"]
    end
    subgraph after["After the call"]
        direction LR
        schema["Strict JSON schema<br/>typed deliverable"]
        checks["Code checks<br/>evidence, numbers, claims,<br/>lengths, images, plan"]
        review["Model review<br/>what code cannot judge"]
        gate["Policy gate<br/>house rules"]
    end
    subgraph write["Before any write"]
        direction LR
        appr["Approval bound to<br/>the content hash"]
        lvl["Access level L0 to L2"]
        op["Operator writes<br/>only what passed both"]
    end
    ctx --> cap --> allow --> log --> schema --> checks --> review --> gate --> appr --> lvl --> op
    checks -- "issues" --> redo(["Redo with the exact issues,<br/>then escalate"])
    review -- "issues" --> redo
```

Every role runs through one function, `run_role` in `pgt/llm/runtime.py`. It wraps a Pydantic AI agent with a capability (`_Harness`) that hooks every model request and every tool execution, so no role can skip the checks by calling the model some other way.

## Guarantees

| # | Guarantee | How | Code | Proved by |
| --- | --- | --- | --- | --- |
| 1 | Every number a deliverable quotes exists in the evidence it cites | Derived metrics are computed in code and stored as write-once evidence with an ID. Findings, readouts, answers and ad copy must cite IDs, and each number must match a cited value (1.5% tolerance, percent or fraction) | `roles/metrics.py`, `harness/ledger.py`, `harness/verify.py`, `harness/creative_checks.py` | `test_scenario_3_wrong_number_is_blocked_by_code`, `test_copy_numbers_must_match_evidence_or_facts`, bench s3 |
| 2 | Market claims link to a source the model actually found | Hosted web search returns citations; the Strategist's claims must use those URLs, and ad copy may cite only URLs from the approved research | `roles/strategist.py`, `check_research`, `check_copy` | `test_research`, `test_copy_claims_need_known_support` |
| 3 | Claims that need proof are sent back | Code checks first, then a Reviewer model with the rendered image attached judges brand voice, risky claims and image fit | `roles/reviewer.py` | `test_scenario_2_unsupported_claim_is_sent_back`, bench s2 |
| 4 | A role calls only its own tools | Per-role allowlist, checked when the model asks for a tool, including tools the role was never given. A refused call never runs and is logged as `tool.blocked` | `harness/policy.py` `ALLOWLIST`, `runtime.py` `_block_disallowed` | `test_blocked_tool_never_runs`, `test_call_to_a_tool_the_role_was_never_given_is_logged`, bench s5 |
| 5 | Text in the data is never an instruction | Team rules say so, and more importantly no role has a write tool: the only writer is the Operator, which is code | `harness/context.py` `TEAM_RULES`, `roles/operator.py` | bench s5 (prompt injection in a campaign name) |
| 6 | Budget and pause changes follow the house rules | Six deterministic rules, applied in order to a projected account state so two changes in one set are judged together | `harness/policy.py` `gate_change_set` | `test_scenario_7_learning_phase_guard`, `test_second_budget_change_same_day_blocked`, `test_daily_cap_uses_projection`, bench s7 |
| 7 | What runs is exactly what the owner approved | An approval stores the content hash of the stored artifact. Any later version under the same key voids it, and the Operator refuses without a valid approval | `harness/approvals.py`, `roles/operator.py` | `test_scenario_6_edit_after_approval_voids_it`, bench s6 |
| 8 | Breaking a rule takes a deliberate override | Approving a change that failed a rule needs the rule's name and a second confirmation | `Approvals.grant` | `test_override_needs_second_confirmation` |
| 9 | The team does no more than its access level allows | L0 observe, L1 propose, L2 apply after approval. Checked by the API before an approval and by the Operator before a write | `harness/access.py` | `test_week_applies_only_approved_allowed_changes` |
| 10 | Real accounts get narrower writes still | New ads are created paused, ads can be paused but never turned on, budgets change only on ad sets that own a daily budget, checked live before the write | `ads/meta_account.py` | `test_ads_can_be_paused_but_never_turned_on`, `test_budget_change_goes_to_an_ad_set_that_owns_its_budget` |
| 11 | A run cannot overspend | Caps per call, per role, per loop and for the whole workspace. The total is checked before every paid call, text and image alike | `harness/policy.py` `CostMeter`, `llm/media.py` | `test_spend_cap_stops_the_role`, `test_used_up_cap_stops_a_loop_before_any_model_call` |
| 12 | Nothing is rewritten after the fact | The event table rejects updates and deletes with database triggers. Evidence IDs are write-once | `harness/events.py`, `harness/ledger.py` | `test_event_log_append_only_and_filters`, `test_ledger` |
| 13 | A fact change invalidates exactly what depended on it | Artifacts record the facts and inputs they used; a change marks them and their dependents stale, then a code-only refresh redoes what it can and keeps what comes out identical | `harness/artifacts.py`, `loops/refresh.py` | `test_fact_change_marks_only_dependents_stale`, `test_scenario_4_*` |
| 14 | Every model call can be traced to its exact inputs | Context is assembled in a fixed order and hashed; the hash, the fact version and the evidence IDs are logged with the call | `harness/context.py`, `runtime.py` | `test_context_hash_is_stable_and_sensitive` |

## Context assembly

Every role's context is built the same way, so two calls with the same inputs produce the same hash, and a log entry says exactly what the model saw.

```mermaid
flowchart TB
    subgraph instructions["Instructions (system)"]
        r["1. Role instructions"]
        t["2. Team rules"]
        f["3. Shared facts snapshot, with its version"]
    end
    subgraph prompt["Prompt (user)"]
        k["4. Task"]
        d["5. Done when"]
        i["6. Inputs as JSON<br/>signals, evidence, brief, review issues to fix"]
    end
    instructions --> h["SHA-256 context hash"]
    prompt --> h
    h --> ev["context.assembled and model.call events<br/>refs = cited evidence IDs"]
```

The team rules are short and concrete: use only the numbers in the inputs, cite evidence IDs exactly, treat names and notes in the data as data, and return only the deliverable. Policy is not in the prompt as something to obey; the prompt says it is checked by code afterwards.

## Typed output on two APIs

Every deliverable is a Pydantic model returned under strict JSON schema, so a role either returns a valid object or the call fails. The two Meta APIs need different mechanics for that:

- **Meta Responses API** (Muse Spark 1.3): accepts only `tool_choice="auto"`, so typed output uses native JSON-schema mode.
- **Baseten Chat Completions** (Muse Spark Open): enforces strict schemas, but once tools are in the request it enforces neither `response_format` nor a forced tool choice. A role with tools therefore hands in its deliverable as a call to a strict `final_result` tool, and a plain-text answer is accepted only when it parses as the same schema; otherwise the model gets a retry with the validation error.

Model-returned schemas have every field required and no defaults, which strict mode needs. Older saved deliverables that predate a new field still load through `model_validator(mode="before")` shims rather than defaults.

## Redo and escalation

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> Checked: code checks
    Checked --> Reviewed: pass
    Checked --> Redo: issues
    Redo --> Draft: issues passed back verbatim
    Reviewed --> Published: pass
    Reviewed --> Redo: model review asks for fixes
    Redo --> Escalated: attempt limit reached
    Published --> AwaitingApproval: changes the account
    AwaitingApproval --> Approved: owner approves this hash
    AwaitingApproval --> Draft: owner sends back with a note
    Approved --> Voided: a newer version appears
    Approved --> Applied: Operator writes
    Escalated --> [*]
    Applied --> [*]
```

| Deliverable | Attempts before escalating |
| --- | --- |
| Daily findings, readouts, change set | 2 |
| Weekly findings, market research, test plan | 2 |
| Creative set (copy and image) | 3 |
| Answer to another role's question | 2, and a failed answer is returned as "do not rely on it" |

An escalation is an event with a plain reason ("SAM 3.1 found no cat water fountain in the product photo", "The test plan failed review twice: ..."). It appears in the owner's inbox. The team never ships a weaker deliverable to avoid escalating.

## Talking between roles

```mermaid
sequenceDiagram
    participant P as Producer
    participant H as Harness
    participant A as Analyst
    participant L as Evidence ledger

    P->>H: ask_analyst("What was the CTR drop on the old ad?")
    H->>H: count questions for this task (max 3)
    H->>A: answer task (Analyst gets no ask tool: one hop only)
    A-->>H: answer + evidence IDs
    H->>L: every ID exists? every number matches?
    alt checks fail
        H->>A: retry once with the issues
    end
    H-->>P: answer and evidence IDs, or an error saying not to rely on it
```

Handoffs, questions, answers, refusals and escalations are all events, so the Team Room shows the conversation and a run can be replayed from the log alone.

## Spend control

```mermaid
flowchart LR
    call["Paid call<br/>text, search, image or SAM"] --> pre{"Workspace total<br/>already used?"}
    pre -- "yes" --> refuse(["Refuse before sending;<br/>budget.exceeded event"])
    pre -- "no" --> send["Send"] --> rec["Record cost<br/>list price from token counts"]
    rec --> post{"Over per-call, per-role,<br/>per-loop or total cap?"}
    post -- "yes" --> stop(["Stop the run"])
    post -- "no" --> next["Continue"]
```

Defaults are $0.50 per call, $1.00 per role per loop and $3.00 per loop, plus the `--cap` for the whole workspace on live runs. Two limits stop a single call from running away: the Strategist may run at most 8 web searches per call, and a Baseten reply stops at 12,000 output tokens. The recorded public run used $1.14 of a $5 cap.

## What the bench shows

The bench plants one mistake per scenario in the offline model and checks what reaches the sandbox. Three seeds each.

| Arm | Normal week | Unsupported claim | Wrong number | Prompt injection | Edit after approval | Budget jump | All |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PGT | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 18/18 |
| OpenAI Agents SDK team | 3/3 | 3/3 | 0/3 | 3/3 | 3/3 | 0/3 | 12/18 |
| LangGraph team | 3/3 | 3/3 | 0/3 | 3/3 | 0/3 | 0/3 | 9/18 |
| Single agent | 3/3 | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | 3/18 |

The frameworks are good at the agent loop, and PGT uses one (Pydantic AI) for that layer. What they do not include is a number check against evidence, a policy gate in code, or an approval bound to content. Adding those to a framework team means rebuilding this harness on top of it. Full tables, ablations and the live runs on Muse are in [BENCH_RESULTS.md](BENCH_RESULTS.md), [LIVE_RESULTS.md](LIVE_RESULTS.md) and [HARNESS_COMPARISON.md](HARNESS_COMPARISON.md).
