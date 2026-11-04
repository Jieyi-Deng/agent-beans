# Meta models and products

Agent Beans is built on Meta's stack end to end: Muse models think, write and review; Muse Image and SAM 3.1 make the ads; the Meta Ads MCP server and the Marketing API connect the team to a real ad account. This page shows which part runs where, why, and what it cost on a recorded live run.

## What Agent Beans uses

| Meta product | Used for | Code |
| --- | --- | --- |
| Muse Spark Open (`muse-spark-open-1026`, open weights, served on Baseten) | Every text role except the Strategist: Growth Lead, Analyst, Media Buyer, Creative Producer, and the Reviewer with an image attached | `pgt/llm/baseten.py` |
| Muse Spark 1.3 on the Meta Model API | The Creative Strategist, because it needs hosted web search with citations | `pgt/llm/meta.py`, `pgt/roles/strategist.py` |
| Hosted web search on the Meta Model API | Current competitor and category examples, each claim with a source URL | `pgt/roles/strategist.py` |
| Muse Image 1.0 | Ad scenes: photographs with the owner's product in them, rendered from the owner's own photo, with no text | `pgt/llm/media.py`, `pgt/roles/producer.py` |
| SAM 3.1 | Finds the product in the owner's photo and in every rendered scene, so layouts keep the words off it and the video's end card shows the real product | `pgt/llm/media.py`, `pgt/creative/` |
| Meta Ads MCP server (`mcp.facebook.com/ads`) | Reads from a real ad account: campaigns, ad sets, ads, insights | `pgt/ads/ads_mcp.py` |
| Marketing API (Graph API) | Reads the MCP cannot answer in full, and every write | `pgt/ads/graph.py`, `pgt/ads/meta_account.py` |
| Facebook Login for Business | The owner connects an ad account: `ads_read` and `ads_mcp_management` for reads, `ads_management` for writes at L2. The token stays in memory and is never written to the workspace | `pgt/ads/meta_login.py` |

## Model per role

```mermaid
flowchart LR
    subgraph baseten["Baseten: Muse Spark Open (open weights)"]
        gl["Growth Lead"]
        an["Analyst"]
        mb["Media Buyer"]
        pr["Creative Producer"]
        rv["Reviewer (vision)"]
    end
    subgraph meta["Meta Model API"]
        st["Creative Strategist<br/>Muse Spark 1.3 + web_search"]
        img["Muse Image 1.0"]
        sam["SAM 3.1"]
    end
    router["RoleRouter<br/>pgt/llm/routing.py"] --> baseten
    router --> st
    pr -. "scene prompt" .-> img
    pr -. "product name from the brief" .-> sam
```

Routing is one small table. `RoleRouter` maps a role name to a Pydantic AI model; roles listed in `META_ONLY` (today only the Strategist) go to the Meta API, everything else to the default. `pgt serve --provider meta` puts every role on the Meta API instead, and the offline model replaces both for tests and the bench.

Muse Spark Open runs at a configurable reasoning effort (`--effort`, default `medium`). Baseten returns the reasoning in `reasoning_content` and already counts reasoning tokens as output, so the logged cost covers them.

## What one live run cost

The public demo is a recording of this run: the pet-supplies store, 18 days of daily checks and two full weekly reviews, each with market research and three new ads. Costs are list-price estimates from each call's token counts, logged as the run happened.

| Model | Role | Calls | Web searches | Cost (USD) |
| --- | --- | --- | --- | --- |
| Muse Spark Open | Analyst | 56 | | 0.4898 |
| Muse Spark Open | Media Buyer | 37 | | 0.3054 |
| Muse Spark Open | Creative Producer | 19 | | 0.2755 |
| Muse Spark Open | Reviewer | 14 | | 0.2221 |
| Muse Spark Open | Growth Lead | 10 | | 0.1191 |
| Muse Spark 1.3 | Creative Strategist | 4 | 16 | 0.2225 |
| Muse Image 1.0 | Creative Producer | 30 | | 0.3000 |
| SAM 3.1 | Creative Producer | 34 | | 0.0850 |
| **Total** | | **204** | **16** | **2.0195** |

Two and a half weeks of an always-on growth team for about two dollars. The 18 daily checks made 76 of the calls and cost $0.62 together, under four cents a day. The Week 1 review, from market research to three reviewed ads, cost $0.38. The Week 2 review cost $0.71: its three ads used more than twice as many images and product cutouts, and four drafts went back for a redo before they passed. Closing each week, when every role writes down what it learned, cost $0.30 for both weeks together. By day 18 a purchase cost $13.16, against the owner's goal of $16.

## How an ad is made

Each ad shows the owner's real product. Muse Image renders every scene from the owner's own photo, SAM 3.1 finds the product in each scene, and the words are set in code, so the image model never writes a headline. An image model can still change a detail of the product, so the Reviewer compares every scene with the owner's photo and sends back any ad where the product looks different.

```mermaid
flowchart TB
    photo[("Owner's product photo")] --> sam1["SAM 3.1<br/>find the named product"]
    sam1 --> cut["Cutout for the<br/>video's end card"]
    brief["Test brief<br/>names one product"] --> prod["Producer (Muse Spark Open)<br/>copy, claims, three scenes,<br/>a layout per scene, storyboard"]
    prod -- "three scene prompts" --> mi["Muse Image 1.0<br/>a photograph with the product in it,<br/>the owner's photo as reference, no text"]
    photo -.->|"reference"| mi
    mi --> sam2["SAM 3.1<br/>find the product in each scene"]
    sam2 --> lay["Layout code<br/>1:1, 4:5, 9:16: words in brand fonts,<br/>kept off the product, inside safe zones"]
    sam2 --> vid["Video code (ffmpeg)<br/>10-second 9:16 cut from the three shots"]
    cut --> vid
    lay & vid --> code["Code checks<br/>lengths, numbers, claims, files,<br/>product in frame, words off it"]
    code --> vis["Reviewer (Muse Spark Open)<br/>contact sheet: owner's photo,<br/>each scene, video frames"]
    code -- "redo with fixes" --> prod
    vis -- "redo with fixes" --> prod
    vis -- "pass" --> cs["Change set for approval"]
```

Each product is segmented once per week and shared by every test that shows it. If SAM 3.1 cannot find the product the brief names, the week stops and the owner is told why, rather than shipping an ad with the wrong product. Each draft gets two redos; if the third attempt still fails a check, the test goes to the owner instead of the change set.

Meta's model API has no video model, so the video is not generated. Code moves the camera toward the product in each shot, slides the storyboard lines in, cross-fades between shots and ends on a card with the product cutout, the offer and the button.

In the Northpaw run, the Reviewer sent back a Week 2 ad for a cat water fountain: one scene showed a black cord floating in the water, which the owner's photo does not have. The redo passed.

## Why Meta's models matter here

**Open weights make an always-on team affordable and portable.** A growth team that checks the account every day runs hundreds of model calls a month for one small business. In this run the daily checks, all on Muse Spark Open, cost under four cents a day, and because the weights are open the same team can move to another host, or to the owner's own infrastructure, without changing a prompt. The routing table is the only thing that changes.

**One model family for planning, tools, structured output and vision.** The same Muse model drafts a test plan, calls account tools, returns strict JSON every deliverable depends on, and reviews a rendered ad by looking at it. That keeps behavior consistent across roles and lets the harness treat every role the same way.

**Hosted web search with citations makes market claims checkable.** The Strategist's research comes back with the URLs the search actually found, and the harness only lets ad copy cite those. A claim about the market is either linked to a real page or it does not ship.

**Muse Image and SAM 3.1 keep the product real.** For an e-commerce seller the product in the ad has to be the product they ship. Muse Image renders every scene from the owner's own photo, so each test gets a fresh setting with the same product. SAM 3.1 finds the product in each scene, so code can keep the words off it, and the Reviewer compares each scene with the owner's photo before an ad can ship.

**Meta's own ads interfaces close the loop.** The official Ads MCP server reads the account, the Marketing API writes only what the owner approved, and Facebook Login for Business scopes access to exactly the account and permissions the owner grants. The team can go from reading results to proposing and creating paused ads without leaving Meta's platform.

## Data handling

- The public demo runs on synthetic sandbox businesses only. The replay export refuses local paths, anything shaped like a key or token, and real ad account IDs.
- Real ad data is never sent to a `-contributor` model: the server refuses to connect a real account when the team runs on one.
- Keys are read from the environment and never logged; errors from Meta carry Meta's message but never the access token.
