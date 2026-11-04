# Architecture

Agent Beans is a small team of AI roles that runs Meta ads for one advertiser. The roles are language-model agents. Everything that decides whether their work is safe to use is ordinary code: the numbers they may quote, the tools they may call, the house rules a budget change must pass, the approval a write needs, and the money a run may spend.

This page shows how the pieces fit. Two companion pages go deeper:

- [harness.md](harness.md): the guarantees the harness enforces, where each one lives in code, and the tests and bench scenarios that prove it.
- [meta-models.md](meta-models.md): which Meta model each role runs on and why, the creative image pipeline, and the Meta ads integration.

Measured results are in [BENCH_RESULTS.md](BENCH_RESULTS.md) (offline, 9 arms), [LIVE_RESULTS.md](LIVE_RESULTS.md) (live, on Muse) and [HARNESS_COMPARISON.md](HARNESS_COMPARISON.md) (Agent Beans against the OpenAI Agents SDK, LangGraph and a single agent).

## System overview

```mermaid
flowchart TB
    owner(["Owner"]) --> web["Web app (React + Vite)<br/>Team Room, Deliverables, Dashboard,<br/>Creative Assets, Settings"]
    web <--> api

    subgraph server["Agent Beans server (FastAPI)"]
        direction TB
        api["REST API + SSE event stream"] --> loops["Loops<br/>daily check, weekly review, refresh"]
        loops --> roles["Roles<br/>Growth Lead, Analyst, Media Buyer,<br/>Strategist, Producer, Reviewer"]
        roles --> harness["Harness<br/>context, evidence, checks,<br/>policy gate, approvals, spend caps"]
        harness <--> ws[("Workspace (SQLite)<br/>events, artifacts, facts, evidence")]
        loops --> operator["Operator<br/>code, no model"]
    end

    subgraph models["Meta models"]
        direction LR
        open["Muse Spark Open<br/>on Baseten"]
        spark["Muse Spark 1.3<br/>+ hosted web search"]
        image["Muse Image 1.0"]
        sam["SAM 3.1"]
    end

    subgraph account["Ad account"]
        direction LR
        sandbox["Sandbox<br/>synthetic business"]
        real["Real Meta account<br/>Ads MCP reads, Graph API writes"]
    end

    harness --> open & spark
    loops --> image & sam
    loops -->|"reads"| account
    operator -->|"approved writes only"| account
    api -.->|"pgt replay"| pages["Static replay<br/>published from the website repo"]
```

A run is driven by loops. A loop gathers data from the account, hands tasks to roles, checks what comes back, and publishes typed deliverables to the workspace. The owner sees every step in the Team Room, which is a live view of the event log, and approves or sends back the deliverables that would change the account.

## Code map

| Package | What it holds |
| --- | --- |
| `pgt.loops` | `daily.py` (daily check), `weekly.py` (weekly review in three stages), `refresh.py` (re-runs only what a fact change made stale), `kickoff.py` (first message after connecting an account), `simulate.py` |
| `pgt.roles` | One module per role: prompt, typed output and inputs. `metrics.py` computes every derived number in code, so no role does arithmetic. `operator.py` is the only writer |
| `pgt.harness` | `events.py` (append-only log), `artifacts.py` (versioned deliverables with stale propagation), `facts.py` (versioned shared facts), `ledger.py` + `verify.py` (evidence and number checks), `creative_checks.py`, `policy.py` (tool allowlist, house-rule gate, spend caps), `approvals.py` (hash-bound approvals), `protocol.py` (handoff, ask/answer, escalation), `context.py` (context assembly), `access.py` (L0 to L2) |
| `pgt.llm` | `runtime.py` (runs a role as a Pydantic AI agent inside the harness), `routing.py` (model per role), `meta.py` and `baseten.py` (providers), `media.py` (Muse Image and SAM 3.1), `offline.py` and `fake.py` (deterministic stand-ins for tests) |
| `pgt.creative` | `compose.py` (product cutouts and shared drawing helpers), `design.py` (the band, panel, poster and offer layouts in 1:1, 4:5 and 9:16), `video.py` (the 10-second 9:16 video and the Reviewer's contact sheet) |
| `pgt.ads` | `meta_account.py` (a real account behind the sandbox interface), `ads_mcp.py` (Meta Ads MCP client), `graph.py` (Graph API client), `meta_login.py` (Facebook Login for Business) |
| `pgt.sandbox` | Synthetic businesses (an indie fitness app and a pet-supplies store) with simulated delivery |
| `pgt.schemas` | Pydantic models for every deliverable. Model-returned types use strict JSON schema |
| `pgt.api` | `app.py` (routes), `views.py` (Team Room, Dashboard), `board.py` (Deliverables, Creative Assets) |
| `pgt.replay` | Records a running team as a static, read-only site |
| `web/` | React 18 + TypeScript front end. A `replay` build mode reads JSON files instead of the API |
| `bench/` | The offline and live harness comparison (9 arms, 6 scenarios) |

## The team

```mermaid
flowchart TB
    owner(["Owner"])
    gl["Growth Lead<br/>intake, test plan, assigns work"]
    an["Analyst<br/>findings, test readouts, answers"]
    mb["Media Buyer<br/>budget and pause proposals"]
    st["Creative Strategist<br/>market research with sources"]
    pr["Creative Producer<br/>one per test, in parallel"]
    rv["Reviewer<br/>code checks, then a vision model"]
    op["Operator<br/>code only"]
    acct[("Ad account")]

    owner -- "facts, approvals, notes" --> gl
    gl -- "handoff" --> an & st & pr & mb
    pr -. "ask (max 3 per task)" .-> an
    an & mb & st & pr --> rv
    rv -- "redo with issues" --> an & mb & st & pr
    rv -- "passed" --> owner
    owner -- "approval bound to content hash" --> op
    op --> acct
```

| Role | Runs on | Output | Tools it may call |
| --- | --- | --- | --- |
| Growth Lead | Muse Spark Open | Weekly brief, test plan | `get_account_state`, `get_evidence` |
| Analyst | Muse Spark Open | Findings, test readouts, answers to the Producer | `get_account_state`, `get_insights`, `get_evidence` |
| Media Buyer | Muse Spark Open | Change set draft (budgets, pauses) | `get_account_state`, `get_insights`, `get_evidence` |
| Creative Strategist | Muse Spark 1.3 on the Meta API | Market research, every claim with a source URL | `web_search` (hosted) |
| Creative Producer | Muse Spark Open | Ad copy, image scene prompt | `ask_analyst` |
| Reviewer | Code, then Muse Spark Open with the image attached | Verdict with one line per fix | `get_evidence` |
| Operator | Code | Execution receipt | none; it is the only component that writes to the account |

Only the Growth Lead (or the loop itself) can assign work. A role can ask one other role a question, one hop deep, at most three times per task, and the answer is checked against the evidence before the asker sees it. A role can propose a change to the shared facts, but only the owner can apply it.

## Daily check

Every simulated or real day, the team reads yesterday's results and decides whether anything should change.

```mermaid
sequenceDiagram
    autonumber
    participant L as Daily loop
    participant A as Account
    participant M as metrics.py (code)
    participant E as Evidence ledger
    participant AN as Analyst
    participant RV as Reviewer checks
    participant MB as Media Buyer
    participant G as Policy gate
    participant O as Owner

    L->>A: state() and insights()
    A-->>L: campaigns, ads, daily rows
    L->>M: compute_signals
    M-->>E: every derived number as evidence with an ID
    L->>AN: findings task (signals + evidence IDs)
    AN-->>RV: findings
    RV-->>AN: redo once if a cited ID or number does not match
    loop each test that is due
        L->>AN: readout task
        AN-->>RV: readout (winner must match the computed rule)
    end
    L->>MB: change set task (reviewed findings only)
    MB-->>RV: changes with evidence IDs
    L->>G: gate every change against house rules
    G-->>L: allowed or blocked, with the rule and reason
    L->>O: daily brief in the Team Room
    O-->>L: approve, approve some, or send back with a note
```

The Media Buyer only sees findings that passed review. A change the gate blocks stays visible in the brief with the rule it broke, so the owner knows what was considered and why it was held back. Overriding a failed rule takes the rule's name and a second confirmation.

## Weekly review

The weekly review makes new ads. It runs in three stages, split by the owner's two approvals. Each stage refuses to run without a valid approval of the stage before it.

```mermaid
flowchart TB
    subgraph s1["Stage 1: plan_week"]
        sig["Weekly signals and carried daily findings"] --> intake["Growth Lead: intake brief"]
        intake --> par{{"in parallel"}}
        par --> wf["Analyst: weekly findings"]
        par --> rs["Strategist: market research<br/>hosted web search"]
        wf --> ck1["Code: evidence and number checks"]
        rs --> ck2["Code: every claim has a source URL"]
        ck1 & ck2 --> plan["Growth Lead: test plan<br/>each test names one product with a photo"]
        plan --> ck3["Code: check_plan, budget split"]
    end

    ck3 --> ap1{"Approval 1<br/>test plan"}

    subgraph s2["Stage 2: produce_week"]
        seg["SAM 3.1: find each product<br/>in the owner's photo, once"] --> prod
        prod["Producer per test, in parallel<br/>copy, three scenes, storyboard;<br/>may ask the Analyst"] --> mi["Muse Image: three scenes<br/>with the product, owner's photo as reference"]
        mi --> sam2["SAM 3.1: find the product in each scene"]
        sam2 --> comp["Layouts 1:1, 4:5, 9:16<br/>and the 10-second video"]
        comp --> ck4["Code: lengths, numbers, claims,<br/>files, product in frame, words off it"]
        ck4 --> vis["Reviewer model sees the owner's photo,<br/>every scene and the video's frames"]
        ck4 -- "redo" --> prod
        vis -- "redo, up to 2 times" --> prod
        vis -- "pass" --> cs["Change set: new ads"]
    end

    ap1 --> s2
    cs --> ap2{"Approval 2<br/>new ads"}

    subgraph s3["Stage 3: apply_week"]
        opw["Operator: create approved ads PAUSED"]
    end

    ap2 --> s3
    ck1 -. "fails twice" .-> esc(["Escalate to the owner"])
    ck3 -. "fails twice" .-> esc
    vis -. "fails 3 times" .-> esc
    seg -. "product not found" .-> esc
```

Every redo carries the exact issues the checks found, so the role fixes those and nothing else. When a stage cannot finish, the team escalates with a plain reason instead of shipping something weaker.

## Workspace and data model

All state lives in one SQLite file per workspace. The event log is append-only, enforced by database triggers, and every store writes its change and the matching event in one transaction. The Team Room, the dashboard and the replay are all views of this file.

```mermaid
erDiagram
    EVENTS {
        int id PK
        text ts
        int sim_day
        text loop_id
        text actor
        text type
        json payload
        json refs
    }
    ARTIFACTS {
        text artifact_id PK
        text key
        int version
        json record "kind, author, status, content, content_hash, inputs, fact_version, fact_keys, evidence_ids"
    }
    FACTS {
        int version PK
        json content "product, audience, goal, target cost, budgets, house rules, brand voice"
    }
    EVIDENCE {
        text evidence_id PK
        json content "kind, values, inputs, note"
    }
    ARTIFACTS ||--o{ ARTIFACTS : "inputs (built from)"
    ARTIFACTS }o--|| FACTS : "fact_version used"
    ARTIFACTS }o--o{ EVIDENCE : "cites"
    EVENTS }o--o{ ARTIFACTS : "refs"
    EVENTS }o--o{ EVIDENCE : "refs"
```

- **Artifacts** are typed deliverables (findings, change set, test plan, creative set, brief). Publishing under an existing key makes a new version; the old one is kept. Each record lists the artifacts it was built from and the facts it used, so a fact change marks exactly the right deliverables stale, transitively.
- **Facts** are versioned. Only the owner and the Growth Lead may set them.
- **Evidence** is write-once: re-adding an ID with different content is an error, so a number can never silently change under a claim that cites it.
- **Events** record everything else: every model call with its token counts, cost, context hash and fact version; every tool call and blocked tool call; handoffs, questions and answers; approvals, voided approvals and writes.
- **The sandbox account** (campaigns, ads, daily results and the Operator's writes) is saved as one row after every team run, so a server restarted on the same file carries on where it stopped. A real account's state is never saved: it is read from Meta again.

In the live e-commerce run recorded for the public demo (18 days, two full weekly reviews), the log holds 204 model calls, 81 tool calls, 449 evidence entries and 120 published deliverables.

## When a fact changes

```mermaid
flowchart TB
    f["Owner changes a fact<br/>for example the weekly budget"] --> v["New fact version"]
    v --> st["Mark stale: every artifact that used the key,<br/>and everything built on it"]
    st --> r["refresh.py (code, no model)"]
    r --> p{"Re-split pending test plan.<br/>Same split?"}
    p -- "yes" --> keep1["Keep it, approval stays valid"]
    p -- "no" --> new1["New version waits on the owner;<br/>old approval no longer matches its hash"]
    r --> c{"Re-gate today's change set.<br/>Same result?"}
    c -- "yes" --> keep2["Keep it"]
    c -- "no" --> new2["New version waits on the owner"]
    r --> m["Model work that is stale<br/>(findings, research, creative)"] --> note["Summary says a new run is needed"]
```

This works like a build system: redo the cheap, mechanical part, stop where a rebuilt input comes out identical, and never spend a model call to find out.

## Real ad accounts

The same loops run against a real Meta ad account. `MetaAdAccount` implements the sandbox interface, so no loop knows which one it has.

```mermaid
flowchart LR
    login["Facebook Login for Business<br/>ads_read, ads_management"] --> acct["MetaAdAccount"]
    acct -- "reads" --> mcp["Meta Ads MCP server<br/>mcp.facebook.com/ads"]
    mcp -. "a read it cannot answer in full" .-> graphr["Graph API reads"]
    op["Operator"] -- "approved, gate-allowed writes" --> acct
    acct -- "writes" --> graphw["Graph API writes"]
    lvl{"Access level"} --> op
```

| Level | What the team may do |
| --- | --- |
| L0 Observe | Read results and write findings. Proposes nothing. |
| L1 Propose | Draft test plans, creative and change sets. The owner applies them in Ads Manager. |
| L2 Apply after approval | The Operator writes exactly what was approved. On a real account new ads are created paused inside an existing ad set, an ad can be paused but never turned on, and a budget changes only on an ad set that owns its daily budget, checked on the live ad set right before the write. |

The API checks the level before it accepts an approval, and the Operator checks it again before it writes. A write Meta refuses is recorded on the receipt; the Operator never retries or works around a refusal.

## The public replay

The public site is a homepage plus two recordings of real live runs, served as static files: Northpaw Supply, a pet-supplies store (`--template ecommerce`), and Beans' Active, a fitness app starting cold (`--template app --cold`). It calls no model and holds no key. The site and its recordings live in a separate repository, [Pallas-ads/agent-beans](https://github.com/Pallas-ads/agent-beans); this repository only produces the recordings.

```mermaid
flowchart TB
    run["pgt serve --live<br/>--template ecommerce or app --cap 5"] --> db[("Workspace")]
    db --> api["Read endpoints"]
    api --> rep["pgt replay<br/>fetch every view and image"]
    rep --> chk{"Synthetic only?<br/>no local paths, keys,<br/>tokens or real account IDs"}
    chk -- "no" --> stop(["Refuse and delete the output"])
    chk -- "yes" --> files["replay/&lt;run&gt;/data/*.json<br/>replay/&lt;run&gt;/media/*"]
    files -- "copied into" --> site["Website repo<br/>Pallas-ads/agent-beans"]
    site --> ci["GitHub Actions<br/>site/build.sh: homepage at the root,<br/>one replay per run under demo/"]
    ci --> pages["GitHub Pages"]
```

The replay build of the web app maps each API path to a JSON file (`/api/dashboard?days=7` becomes `data/dashboard-7.json`) and hides every control that would write. The owner's name and picture are not part of any prompt, only of the pages, so each recording carries its own owner: Rosa, shown by initials, runs Northpaw, and Jieyi runs Beans' Active.

## Testing

- `uv run pytest` runs the whole suite. Unit tests cover each harness component; acceptance tests run whole daily and weekly loops on the offline model; API tests drive the FastAPI app and the replay export.
- The offline model (`pgt.llm.offline`) is deterministic and can plant specific mistakes (a wrong number, an unsupported claim, an injected instruction), so the tests prove the harness catches them rather than hoping the model avoids them.
- The Meta Ads MCP, the Graph API and both model providers are behind injectable transports, so no test touches the network.
- `bench/` runs the same scenarios against Agent Beans, five ablations, a single agent, an OpenAI Agents SDK team and a LangGraph team. See [BENCH_RESULTS.md](BENCH_RESULTS.md).
