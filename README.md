# Agent Beans

![Agent Beans, created by Jieyi Deng for the Meta Global AI Developer Hackathon: the Bean Crew, seven bean characters standing in a row](docs/images/cover.png)

An always-on Meta ads growth team for small businesses and indie developers. Built on Meta's Muse models for the Meta Global AI Developer Hackathon (Indie developer track).

Six AI specialists check your ad account every day, then plan and make new ads each week. A seventh member, the Operator, applies the changes. The Operator is plain code and acts only on what you approved. Every number the team shows traces back to source data.

**Live demo:** [pallas-ads.github.io/pallas-growth](https://pallas-ads.github.io/pallas-growth/). The homepage embeds read-only recordings of two live runs on synthetic sandbox accounts: Northpaw Supply, an online pet store, and Beans' Active, a new fitness app. Because it is the real app, you can click through the Team Room, deliverables, dashboard, and creative.

## The problem

Running Meta ads well requires daily work. Someone has to review yesterday's results, catch ads that are wearing out, cut wasted budget, adjust spend without resetting the learning phase, and test new creative every week. Larger advertisers have a growth team or an agency for this. Small businesses and indie developers have neither, so the work happens only when there is time, which is rarely every day.

AI could handle this work, but current tools usually fall into two groups: assistants that give general advice and leave the execution to you, and agents that act on the account on their own. For an owner spending their own money, neither option works. An agent that quotes a number not found in the data, makes a claim the business cannot back up, or changes a budget nobody approved is worse than no agent at all.

Agent Beans is built to close that gap: the team handles the daily and weekly work, while the owner stays in control of every change and can verify every number.

## Who it is for

- **Small businesses already running Meta ads.** For example, an online store with a few products and some ad history. The team reviews the account every day, proposes budget adjustments and pauses within your house rules, and creates new ads using photos of the products you actually ship.
- **Indie developers launching with no ad history.** For example, a new fitness app. The team starts cold, plans its first tests from zero, and reviews results as they come in.
- **Anyone without the time or budget for a marketer**, who still wants to decide what happens to their account and their money.

## What the team does

| Role | What it does | Runs on |
| --- | --- | --- |
| Growth Lead | Plans the week, writes the test plan, assigns work | Muse Spark Open |
| Analyst | Reads the account, writes findings and test readouts, answers teammates' questions | Muse Spark Open |
| Media Buyer | Proposes budget and pause changes | Muse Spark Open |
| Creative Strategist | Researches the market with web search; every claim links to its source | Muse Spark 1.3 with hosted web search |
| Creative Producer | Writes the copy and composes each ad, one Producer per test, in parallel | Muse Spark Open, Muse Image 1.0, SAM 3.1 |
| Reviewer | Checks every deliverable in code, then looks at the rendered ad | Code, then Muse Spark Open with the image |
| Operator | The only part that writes to the ad account, and only what you approved | Code, no model |

**Every day**, the team reviews yesterday's results. Code computes every derived number and stores it as evidence with an ID. The Analyst writes findings that cite that evidence, the Reviewer checks them, and the Media Buyer proposes changes based only on reviewed findings. A policy gate checks each change against your house rules. The daily brief then arrives in the Team Room, where you can approve all of it, approve part of it, or send it back with a note.

**Every week**, the team creates new ads in three stages, separated by two approvals. The Growth Lead writes a test plan where each test names one product with a photo. Once you approve the plan, the Strategist researches the market, and one Producer per test writes copy and builds the ad: SAM 3.1 cuts your product out of your own photo, and Muse Image 1.0 paints the scene around it. The Reviewer checks each ad. Once you approve the change set, the Operator creates the new ads in a paused state. At the end of the week, each role turns its results into lessons for next time, and you can edit any of them.

You talk to the team in the Team Room. Owner notes go directly to the role they concern, and a role can ask one teammate a question when it needs one.

## How it is built

```mermaid
flowchart LR
    owner(["Owner"]) <--> web["Web app<br/>Team Room, deliverables,<br/>dashboard, creative, settings"]
    web <--> loops["Loops<br/>daily check, weekly review"]
    loops --> roles["Six roles<br/>on Muse models"]
    roles <--> harness["Harness<br/>evidence, checks, tool allowlists,<br/>policy gate, approvals, spend caps"]
    harness <--> ws[("Workspace<br/>one SQLite file,<br/>append-only event log")]
    loops --> operator["Operator<br/>code only"]
    operator -->|"approved writes only"| account["Ad account<br/>sandbox, or real Meta account"]
    loops -->|"reads"| account
```

Loops drive the work. A loop reads the account, assigns tasks to roles, checks what comes back, and publishes typed, versioned deliverables to the workspace. Each role is a Pydantic AI agent with its own tools and typed output. Around the roles sits a harness that enforces in code what a prompt can only request. The server uses FastAPI with a server-sent event stream, and the web app uses React with Vite. Everything the team does lives in a single SQLite file per workspace, and the Team Room provides a live view of its event log.

The same loops run against a synthetic sandbox account, used for the demo and tests, or a real Meta ad account. The team connects a real account through Facebook Login for Business, reads it through Meta's Ads MCP server, and writes to it through the Marketing API.

## Technical highlights

- **Every number is checked against evidence.** Code computes metrics and stores them as write-once evidence. Findings, readouts, answers, and ad copy must cite evidence IDs, and every quoted number must match a cited value. A wrong number is sent back before you ever see it.
- **Market claims need a real source.** The Strategist's research includes the URLs that hosted web search actually returned, and ad copy may cite only URLs from research you approved.
- **What runs is exactly what you approved.** An approval stores the content hash of the deliverable. Any later edit voids it, and the Operator refuses to act without a valid approval. Overriding a failed house rule requires the rule's name and a second confirmation.
- **Rules live in code, not in prompts.** Each role can call only its own tools, and a refused call never runs. Budget and pause changes must pass six deterministic house rules: the target exists, enough days have passed since the last budget change, the change stays within a maximum step, a campaign in its learning phase keeps its budget, an ad has spent enough before it is paused, and the total daily budget stays under a cap. A change set is evaluated together on a projected account state. Access levels (L0 observe, L1 propose, L2 apply after approval) are checked by the API and checked again by the Operator. On a real account, new ads are created paused, and the team can pause ads but can never turn them on.
- **Spend is capped before every call.** Caps are enforced per call, per role, per loop, and across the whole workspace before every paid text or image call.
- **Everything can be traced.** The event log is append-only, enforced by database triggers. Each model call's full request is stored under its hash, and `pgt inspect-call` prints back exactly what the model saw.
- **A fact change redoes only what depended on it.** Deliverables record the facts and inputs they used. When you change a fact, such as the weekly budget, dependent items are marked stale and a code-only refresh redoes what it can, like a build system.
- **Memory that stays honest.** Each role keeps private memory that no other role can read. Every lesson must cite an existing source, your edits are final, and the feedback loop can narrow what the team does but never loosen it.
- **Built for open-weight models.** Every call has a deadline and one retry, and every failed call records its failure type. Targeted repairs fix good answers that arrive in the wrong shape, such as a tool call written as text or raw tab characters inside a JSON string, without ever adding content.
- **The demo is the real app.** `pgt replay` records a running team as a static, read-only copy of the app for GitHub Pages, and refuses to export anything that is not synthetic sandbox data.

The [harness guide](docs/harness.md) lists 19 guarantees, each with the code that enforces it and the test that proves it.

## Built on Meta

| Meta product | Used for |
| --- | --- |
| Muse Spark Open (open weights, on Baseten) | Every daily role: Growth Lead, Analyst, Media Buyer, Creative Producer, and the Reviewer with the image attached |
| Muse Spark 1.3 with hosted web search, on the Meta API | The Creative Strategist's market research, with a source URL for every claim |
| Muse Image 1.0 | Ad backgrounds: a scene with no text and no product |
| SAM 3.1 | Cuts the advertised product out of the owner's own photo, so the ad shows the real product |
| Meta Ads MCP server | Reads from a real ad account: campaigns, ad sets, ads, insights |
| Marketing API | Every write, and the reads the MCP cannot answer in full |
| Facebook Login for Business | Connects an ad account with only the permissions the owner grants |

Open-weight models make an always-on team affordable. In the recorded pet-store run, 14 daily checks cost $0.51 combined, or under four cents a day. The whole run, including a weekly review with market research and three new ads, cost $1.14 across 117 model calls. [Meta models and products](docs/meta-models.md) breaks down the cost per role and explains why Meta's models matter here.

## Results

An offline benchmark runs the same scenarios against Agent Beans, a single agent, an OpenAI Agents SDK team, and a LangGraph team, with three seeds each. The scenarios include a wrong number, an unsupported claim, a prompt injection in a campaign name, an ad edited after approval, and a budget raised again one day after the last change. The offline model plants each mistake, so the bench tests the harness, not the model.

| Arm | Runs passed |
| --- | --- |
| Agent Beans | 18 of 18 |
| OpenAI Agents SDK team | 12 of 18 |
| LangGraph team | 9 of 18 |
| Single agent | 3 of 18 |

Five ablations of Agent Beans each remove one component, and each fails only the specific scenario that component was built to prevent. See the [framework comparison](docs/HARNESS_COMPARISON.md), the [bench results](docs/BENCH_RESULTS.md), and the [live results](docs/LIVE_RESULTS.md).

## Run it

Requirements: Python 3.12 or later with [uv](https://docs.astral.sh/uv/), and Node 18 or later.

```bash
git clone https://github.com/Jieyi-Deng/agent-beans.git
cd agent-beans
uv sync
cd web && npm install && npm run build && cd ..
uv run pgt serve --template ecommerce
```

Open http://127.0.0.1:8000. Without `--live`, the team runs on an offline model against a synthetic sandbox account, so nothing costs money. `--template app` runs the fitness app instead of the pet store, and `--cold` starts with no history. The first screen asks for your name, which the team uses to mention you, and an optional photo (otherwise the app shows your initials). Neither ever goes into a prompt.

To run on Muse, copy `.env.example` to `.env`, fill in the keys, and set a spend cap in US dollars:

```bash
uv run --env-file .env pgt serve --live --template ecommerce --cap 5
```

To connect a real ad account on a local install, fill in the Facebook Login settings in `.env.example`; the comments there explain each setting.

Other commands: `pgt simulate` runs daily checks over simulated days, `pgt weekly` runs one weekly review, `pgt replay` records a running team as a static site, and `pgt inspect-call` prints a stored model request.

Tests and the offline bench need no keys:

```bash
PYDANTIC_AI_NO_BANNER=1 uv run pytest -q
uv run python -m bench.run
```

## Repository layout

The Python package and the command line are named `pgt`, after the project's working name, Pallas Growth Team. Everything else is Agent Beans.

| Path | Contents |
| --- | --- |
| `src/pgt/harness/` | Event log, artifacts, facts, evidence ledger, checks, policy gate, approvals, access levels, memory, context assembly |
| `src/pgt/roles/` | One module per role |
| `src/pgt/loops/` | The daily check, the weekly review, the week's close, and the refresh after a fact changes |
| `src/pgt/llm/` | Model routing, the Meta and Baseten clients, Muse Image and SAM 3.1, answer repairs, the offline model |
| `src/pgt/sandbox/` | The synthetic ad account and the businesses the demo and tests run on |
| `src/pgt/ads/` | Real ad accounts: Ads MCP reads, Marketing API writes, Facebook Login |
| `src/pgt/creative/` | Product cutout and ad composition |
| `src/pgt/api/`, `web/` | The FastAPI server and the React app |
| `site/`, `replay/` | The public homepage and the recorded runs it embeds |
| `bench/` | The comparison bench and its arms |
| `spike/` | The Day 1 spike that chose Pydantic AI for the per-role agent loop |
| `tests/` | Unit, API and scenario tests |

## Documentation

| Document | What it covers |
| --- | --- |
| [Architecture](docs/architecture.md) | System overview, the team, the daily and weekly loops, the data model, real ad accounts, the public replay |
| [The harness](docs/harness.md) | Each guarantee, how it works, where it lives, and the test that proves it |
| [Meta models and products](docs/meta-models.md) | Which Meta model or product runs each part, the cost of a live run, and why Meta's models matter here |
| [Framework comparison](docs/HARNESS_COMPARISON.md) | Agent Beans against a single agent, an OpenAI Agents SDK team and a LangGraph team |
| [Bench results](docs/BENCH_RESULTS.md), [live results](docs/LIVE_RESULTS.md) | Full tables from the offline and live runs |

## License

The code in this repository is MIT licensed. The fonts in `src/pgt/creative/fonts/` come with their own licenses, included next to them.

**Built with Muse Spark Open.** The team's roles run on Muse Spark Open, used under the Muse Model License, which asks for this notice. The model weights are not part of this repository.
