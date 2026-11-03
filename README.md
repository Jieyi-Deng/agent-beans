# Agent Beans

An always-on AI growth team for Meta ads, built on Meta's Muse models for the Meta Global AI Developer Hackathon (Indie developer track).

Agent Beans checks your ad account every day, plans and makes new ads every week, and never changes anything without your approval. Every number it shows traces back to the source data.

**Live demo:** [pallas-ads.github.io/pallas-growth](https://pallas-ads.github.io/pallas-growth/), the project homepage with read-only recordings of two live runs on synthetic sandbox accounts: Northpaw Supply, a pet-supplies store (14 daily checks and one weekly review with three new ads, $1.14 of model calls), and Beans' Active, a fitness app with no ad history ($0.96).

## How it works

Six roles work as one team. A Growth Lead plans the week, an Analyst reads the account, a Creative Strategist researches the market with web search, a Creative Producer writes and composes ads, a Media Buyer proposes budget changes, and a Reviewer checks the work. A seventh role, the Operator, is plain code: it is the only part that writes to the ad account, and only what the owner approved.

Around the roles sits a harness that enforces in code what prompts can only ask for: every quoted number must match cited evidence, each role can call only its own tools, budget changes pass a policy gate, approvals are bound to the exact content approved, and spend is capped before every call.

| Document | What it covers |
| --- | --- |
| [Architecture](docs/architecture.md) | System overview, the team, the daily and weekly loops, the data model, real ad accounts, the public replay |
| [The harness](docs/harness.md) | Each guarantee, how it works, where it lives, and the test that proves it |
| [Meta models and products](docs/meta-models.md) | Which Meta model or product runs each part, the cost of a live run, and why Meta's models matter here |
| [Framework comparison](docs/HARNESS_COMPARISON.md) | Agent Beans against a single agent, an OpenAI Agents SDK team and a LangGraph team |
| [Bench results](docs/BENCH_RESULTS.md), [live results](docs/LIVE_RESULTS.md) | Full tables from the offline and live runs |

## Run it

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), and Node 20 or later.

```bash
uv sync
cd web && npm install && npm run build && cd ..
uv run pgt serve --template ecommerce
```

Open http://127.0.0.1:8000. Without `--live` the team runs on an offline model against a synthetic sandbox account, so nothing costs money. The first screen asks for your name, which the team uses to mention you, and an optional photo (your initials otherwise); both stay in the workspace, never in a prompt, and can be changed in Settings or set with `--owner NAME --owner-avatar PHOTO`.

To run on Muse, copy `.env.example` to `.env`, fill in the keys, and add a spend cap:

```bash
uv run --env-file .env pgt serve --live --template ecommerce --cap 5
```

Other commands: `pgt simulate` runs daily checks over simulated days, `pgt weekly` runs one weekly review, and `pgt replay` records a running team as the static site above.

Tests and the offline bench:

```bash
PYDANTIC_AI_NO_BANNER=1 uv run pytest -q
uv run python -m bench.run
```

## Repository layout

| Path | Contents |
| --- | --- |
| `src/pgt/harness/` | Event log, artifacts, facts, evidence ledger, checks, policy gate, approvals, access levels, context assembly |
| `src/pgt/roles/` | One module per role |
| `src/pgt/loops/` | The daily check, the weekly review, and the refresh after a fact changes |
| `src/pgt/llm/` | Model routing, the Meta and Baseten clients, Muse Image and SAM 3.1, the offline model |
| `src/pgt/sandbox/` | The synthetic ad account and businesses the demo and tests run on |
| `src/pgt/ads/` | Real ad accounts: Meta Ads MCP reads, Marketing API writes, Facebook Login |
| `src/pgt/creative/` | Product cutout and ad composition |
| `src/pgt/api/`, `web/` | FastAPI server and the React app |
| `site/`, `replay/` | The public homepage and the recorded runs it embeds |
| `bench/` | The comparison bench and its arms |
| `spike/` | The Day 1 spike that chose Pydantic AI for the per-role agent loop |

## Built during the hackathon (Oct 27 – Nov 4, 2026)

Everything in this repository was written during the event.

## Background

Pallas started before the event as an ad-analysis plugin for coding agents. None of that code is in this repository.

## License

The code in this repository is MIT licensed.

**Built with Muse Spark Open.** The team's roles run on Muse Spark Open, used under the Muse Model License, which asks for this notice. The model weights are not part of this repository.
