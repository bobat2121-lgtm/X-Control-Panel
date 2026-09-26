# X Control Panel — Plan (v0.2 · 2026-09-25)

A Streamlit control panel, run mostly by AI agents, that surfaces ready-to-post drafts and build ideas for an X account focused on **Bitcoin (80%)** and **AI (20%)**. A human stays in the loop: agents draft, you edit and post.

## Decisions (v0.2)
- **LLM:** no Anthropic API. The agents run **Codex CLI signed in with your ChatGPT account** (your plan's usage, not API billing). ChatGPT's own Scheduled Tasks can't push results into an outside app unattended: writes need confirmation, and output stays in ChatGPT.
- **Cloud:** GitHub Actions runs the agents on cron (gated on New York time). Postgres (Neon/Supabase) stores everything. The panel is a private Streamlit Community Cloud app.
- **Showcase slots (v0.3, 2026-09-26):** Monday pre-market, Wednesday midday and Friday after-close post the owner's **Digital Credit Report** panels (digital-exposure repo): The Accretion Ledger, The Coupon Sheet and The Closing Mark. A separate `showcase` workflow renders each one with that repo's own code and audits it. It drafts the post only after the new data is in: Monday after both 8-Ks are ingested and match the image, Friday after the 4 pm close. Regular drafts are still generated as a backup. (Replaces the Build Lab gallery.)
- **Monitor pivot (v0.4, 2026-09-26):** the agents now brief and the owner writes. A 15-minute monitor (no AI) watches the owner's 7 watchlist accounts on X first (they carry the most weight), then news outlets, regulators, Google News topics and 8-Ks, groups stories across outlets and pings priority news. The slot runs write desk briefs instead of drafts. AI drafting returns later via writer mode "drafts" once the owner's voice has settled.
- **X Premium:** long posts are allowed. The editor flags hooks that land after 280 characters.
- **Alerts:** Discord webhook.
- **Tabs:** Feed, Build Lab, Radar, Scoreboard, Control Room.
- **Watchlist scanning:** X recent search with `from:` queries and `since_id`, so no X List is needed and only new posts are paid for.

Status: all five tabs, the agents, scheduler, workflow and gallery scaffold are built and tested locally with a mock LLM and live market, SEC and RSS data. What's left is the account setup in README.md.

---

## 1. Content strategy

### Pillars (targets are defaults you can change in Control Room)

| Lane | Pillar | Share of posts | What counts |
|---|---|---|---|
| BTC | **Digital credit** | 35% | Strategy's STRC (plus STRF/STRK/STRD), Strive's SATA, MSTR mNAV, ASST, ATM issuance, dividend-rate changes, coverage ratios, the DGCR digital-credit ETF |
| BTC | **Bitcoin** | 30% | Price action, ETF flows, on-chain, treasury companies, policy |
| BTC | **Macro** | 15% | Fed, rates, CPI/jobs, DXY, liquidity, credit spreads (useful for comparing digital-credit yields) |
| AI | **Frontier AI** | 20% | New LLM releases, benchmarks/evals, physical AI (robotaxis, self-driving trucks, humanoids, industrial automation when the update is big) |

**Tone mix (default):** 50% analytical · 30% timely/news · 20% funny. Every draft carries a tone tag so the mix can be tracked.

### Daily slots (America/New_York, so the EST/EDT switch happens automatically)

| Slot | Drafts ready | You post | Content |
|---|---|---|---|
| ☀️ Pre-market BTC | ~7:20 | 8:00 | Overnight BTC move, MSTR/STRC/SATA pre-market, today's macro calendar (8:30 data drops), Monday Strategy 8-K. **Mon = showcase** |
| 🤖 AI ideas | ~11:35 | whenever (optional) | 2–3 AI stories × 2 variants |
| 🕜 Midday BTC | ~13:05 | 13:30 | Market reaction, digital-credit moves, ETF flows, breaking news. **Wed = showcase** |
| 🔔 Friday close | ~16:25 | 16:30 | Weekly wrap after the close. **Fri = showcase** |
| 🌙 Nightly (no post) | — | — | Pull your post metrics, update the scoreboard, run the Build Strategist |
| 📅 Sunday (no post) | — | — | Weekly review, the week ahead, deeper build ideas |

**Holding the 20/80 split:** a Mix Meter tracks the rolling 7-day share of what you *actually posted*. When AI goes over 20%, the AI slot is labelled "optional" and produces fewer options. When BTC falls short, the BTC slots add more.

---

## 2. Agents

Plain Python jobs. The LLM steps call Codex CLI (your ChatGPT account) with a JSON schema, so output is always structured. Each job does one clear thing, and the scheduler runs them on the slot timetable.

| # | Agent | Kind | Job |
|---|---|---|---|
| 1 | **Scout** | LLM + code | Pulls the X watchlist timeline, keyword searches, RSS/news and SEC EDGAR filings. Dedupes items, groups them into **Stories**, and scores each one for timeliness, insight, engagement velocity, pillar fit and humor potential. |
| 2 | **Market Desk** | Code only | Takes a numeric snapshot at run time (BTC, MSTR, STRC vs $100 par, SATA, mNAV, yields, DXY, ETF flows, fees/hashrate) and renders chart PNGs. It supplies the drafts' numbers so the LLM never invents one. |
| 3 | **Writer** | LLM | For each slot, turns the top stories into 2–3 variants in your voice (Analyst · Punchy · Funny · Thread opener · Morning Brief). Each variant links to its inspiration and lists the numbers it used. |
| 4 | **Editor** | LLM + code | Checks every figure against the snapshot, checks X character weighting, rejects angles repeated from the last 7 days, catches wording that reads like financial advice, and flags drafts too close to the source post. |
| 5 | **Analyst** | Code (+LLM summary) | Runs nightly. Pulls your posts and their metrics, computes the mix and what's working, and diffs your edits against the AI drafts. That diff is how the system learns your voice. |
| 6 | **Build Strategist** | LLM | Runs a light daily pass and a deeper Sunday pass over your posts, their performance and the signal archive. Proposes **Build Lab** ideas with full specs and a ready-to-paste Claude Code prompt. |

**Learning loop:** your edits, dismiss reasons and post performance go back into the Writer's prompt as few-shot examples. The drafts should sound more like you each week.

---

## 3. Data sources

| Need | Source | Cost |
|---|---|---|
| Watchlist posts | X API v2 (pay-per-use): recent search with batched `from:` queries, read incrementally with `since_id` | ~$0.005/post read |
| Keyword/ticker search | X API recent search (`$STRC`, `$SATA`, "digital credit", model names…) with a per-run cap | ~$0.005/post |
| Your own posts + metrics | X API owned reads | ~$0.001/post |
| Discovery beyond watchlist (optional) | xAI Grok API `x_search` tool (semantic search) | ~$5/1k posts + tokens |
| BTC price | Coinbase / Kraken public API | free |
| Equities & preferreds | yfinance (MSTR, STRC, STRF, STRK, STRD, ASST, SATA, IBIT) incl. pre-market | free |
| Filings (big timing edge) | SEC EDGAR JSON: Strategy (CIK 1050446), Strive (CIK 1920406) 8-Ks | free |
| Macro | FRED API (rates, DXY, spreads), plus an economic calendar | free key |
| On-chain | mempool.space API (fees, hashrate, difficulty) | free |
| ETF flows | Farside (daily) | free |
| News | RSS (CoinDesk, The Block, Bitcoin Magazine, AI lab blogs) + Claude web search | tokens |
| AI benchmarks | LMArena, Artificial Analysis, SWE-bench, ARC-AGI, Epoch AI | free |

---

## 4. The Streamlit app

Top navigation with five pages. Each page has a sticky **Market Strip** across the top: BTC · 24h · MSTR · mNAV · STRC vs par · SATA · 10Y · DXY.

### Tab 1 — 📰 Feed (drafts, X-style)

- **Today bar:** slot checklist (☐ Pre-market ☐ Midday ☐ AI optional), Mix Meter, and a "next slot in 2h 14m" countdown.
- **Filters:** slot · date · status (New / Edited / Posted / Dismissed / Banked) · pillar · tone.
- **Draft card:**
  - Header: slot badge, pillar chip, tone chip, score, and a freshness timer (the story goes stale in N hours).
  - Variant switcher (**A / B / C**) with an editable text box and a live **X-weighted character count** (URLs count as 23, emoji as 2).
  - **One-click rewrites:** Shorter · Punchier · More data · Funnier · Stronger hook · Make it a thread · Plain (no emoji or hashtags). A free-text box handles custom instructions. Every rewrite saves a new version, so undo is always available.
  - **Inspiration panel:** embedded source posts (author, snippet, engagement, link), news and filing links, and the **numbers used** with source and timestamp.
  - **Attach:** a suggested auto-chart (PNG download).
  - **Actions:** 🚀 **Post on X** (opens X's composer prefilled, with no API cost) · 💬 Quote/Reply (prefilled against the source post) · 📋 Copy · ✅ Mark posted (paste the URL so the post gets tracked) · ⏰ Snooze to next slot · ⭐ Bank as evergreen · 🔁 Regenerate · 🗑 Dismiss (asks for a reason, which feeds learning).
- **Evergreen bank:** good unused drafts saved for slow news days.

### Tab 2 — 🛠 Build Lab (AI build creation ideas)

- **Top strip, "This week's picks":** the 3 ideas with the best Impact ÷ Effort, weighted by timeliness.
- **Pipeline:** 💡 Inbox → ⭐ Shortlist → 🔨 Building → 🚢 Shipped → 🗄 Archive. Switch between board and table views, and sort or filter by format, pillar, effort and expiry.
- **Idea card fields:**
  - **Title + one-line hook** (the sentence the launch post would open with)
  - **Format:** GIF/short-video simulation · interactive Streamlit page · chart pack · calculator/tool · thread series · meme template · live tracker/dataset
  - **Why now:** links to the signals and posts that sparked it, plus an **expires** date for timely ideas
  - **Concept sketch:** what the viewer sees (frame by frame for GIFs and videos)
  - **Data inputs:** which sources, and whether they're already in Market Desk
  - **Build spec:** stack, steps, and what "done" looks like
  - **Scores:** Impact · Novelty · Effort (S ≤1h · M ≈ half-day · L multi-day) · Evergreen vs timely
  - **Launch kit:** launch-post draft + 2 follow-up posts
  - **🧩 Copy build prompt:** one click copies a complete prompt to paste into Claude Code
  - **Buttons:** Remix · Make it simpler · Make it wilder · Split into a series
  - **After shipping:** link to the artifact plus its post performance, which feeds the Strategist
- **Series:** recurring formats built from past ideas, e.g. "Monday 8-K Breakdown" and "STRC Rate Watch" (monthly).

### Tab 3 — 📡 Radar (raw signals)

- Every scanned post, article and filing, grouped into stories with scores. Star a story, or click **Draft from this**.
- **Paste any X URL or link → get drafts** for on-demand posting.
- **Reply Radar:** fast-rising posts from large accounts in your lanes, each with a suggested reply. Early, smart replies are one of the biggest growth levers on X.

### Tab 4 — 📊 Scoreboard

- Posted history with metrics, and the Mix Meter over time (target vs actual by pillar and tone).
- What's working: best pillar, format and time of day, and which draft variant types you choose most.
- The Weekly Analyst memo (Sunday).

### Tab 5 — ⚙️ Control Room

- Watchlist editor (accounts grouped by lane, keywords and tickers), voice profile, split and tone targets, and slot times.
- **Run now** buttons for each job, the run log with status, cost per run, and daily spend caps.
- Week-ahead calendar: FOMC, CPI, jobs, Strategy/Strive earnings, the monthly STRC rate announcement, SATA rate changes.

---

## 5. Data model (SQLite first; the schema ports to Postgres)

- `items`: raw scanned things (kind: x_post / news / filing / data, author, url, text, metrics, lane, pillar, fetched_at)
- `stories`: clustered items, with summary, scores, `item_ids` and status
- `snapshots`: Market Desk JSON for each run
- `drafts`: slot, story, pillar, tone, status, chosen variant
- `variants`: draft_id, label, text, version, parent_version, created_by (ai / me / rewrite:punchier)
- `posts` + `post_metrics`: your actual posts, linked back to drafts
- `feedback`: posted / edited / dismissed + reason
- `build_ideas`: every card field above, plus status and timestamps
- `runs`: job, timing, status, tokens, X reads, cost, log

---

## 6. Tech stack & layout

- **Python 3.12+**, Streamlit, Codex CLI with `--output-schema` (structured outputs for drafts and ideas, on your ChatGPT account), `httpx` for the X API and EDGAR, `yfinance`, `feedparser`, SQLite, Plotly for in-app charts, matplotlib + `imageio-ffmpeg` for PNGs, GIFs and MP4s. `xai-sdk` is optional.
- Jobs run **outside** Streamlit as CLI commands (`python -m jobs.run premarket`) on a scheduler. The app only reads and writes the database, which keeps it fast and stable.

```
X Agent/
  app/              streamlit_app.py + pages/ (feed, build_lab, radar, scoreboard, control_room)
  agents/           scout.py, writer.py, editor.py, analyst.py, strategist.py
  sources/          x_api.py, grok_search.py, market.py, edgar.py, rss.py, macro.py
  core/             db.py, llm.py, charts.py, notify.py, x_text.py (char weighting)
  jobs/             run.py (slot orchestration)
  config/           watchlist.yaml, pillars.yaml, voice.md, settings.yaml
  data/             app.db, charts/
  .env              API keys (never committed)
```

---

## 7. What's needed to run smoothly

1. **Python 3.12+.** It isn't installed yet: the machine only has the Windows Store stub. Install it from python.org and check "Add to PATH", or run `winget install Python.Python.3.13`. Git 2.55 and Node 24 are already present. ffmpeg isn't needed separately because `imageio-ffmpeg` bundles it.
2. **API accounts:**
   - ChatGPT account signed in to Codex CLI (a dedicated cloud login)
   - X developer account on pay-per-use with credits loaded, using an OAuth app on your own account
   - (optional) xAI API key
   - FRED key (free)
3. **Watchlist handles** entered in Control Room → Watchlist (no X List needed).
4. **Voice material:**
   - Your handle, plus 30–50 of your best posts or your X archive export
   - A "never say" list (phrases, emoji, hashtags)
   - A short "what I believe" note, with your stances on BTC, digital credit and AI
5. **Always-on scheduling.** A 7:10 job needs a machine that's awake, which is covered in decision #1.
6. **Notifications.** A push when drafts are ready, e.g. "☀️ Pre-market drafts ready" with the top draft and a one-tap Post link. Delivered through your Discord webhook.
7. **Guardrails:**
   - No auto-posting in v1. You always press Post.
   - The numbers in drafts come only from the Market Desk snapshot.
   - Posts about STRC, SATA, MSTR and ASST are securities commentary, so avoid advice language and consider disclosing positions.
   - Credit or quote-post when building on someone else's take.
   - Hard daily caps on X reads and LLM spend.

### Rough monthly cost (estimates to tune once real usage is visible)

| Item | Estimate |
|---|---|
| X API reads (30–60 account watchlist + capped searches + own metrics) | ~$25–70 |
| LLM (Codex on your ChatGPT plan) | $0 extra; uses your plan's Codex limits (about 5–7 runs/day + rewrites) |
| Grok X search (optional discovery) | ~$10–30 |
| Hosting | $0 (local, or free tiers) |

---

## 8. Build phases

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Setup: Python, keys, repo skeleton, config files, DB schema | `streamlit run` shows an empty app, and keys are verified |
| 1 | Market Desk + Scout (X search + EDGAR + RSS) + Writer + Editor → **Feed tab**, with manual "Run pre-market" | You get real pre-market drafts with inspiration links and verified numbers |
| 2 | Scheduler for all 3 slots, notifications, **Radar** tab, paste-a-URL drafting | Drafts arrive on time without you touching anything |
| 3 | Analyst: own-post metrics, **Scoreboard**, Mix Meter, learning loop | The split and performance are visible, and edits feed the Writer |
| 4 | Build Strategist + **Build Lab** tab, with copy-able build prompts | Ideas show up daily with full specs |
| 5 | Polish: X-style cosmetics, Reply Radar, EDGAR 8-K "breaking" watcher, cloud move if wanted | — |

---

## 9. Seed Build Lab ideas (examples of what the Strategist should produce)

1. **"The Par Keeper" (GIF):** STRC's price oscillates around $100 while monthly rate changes pull it back to par, an animated control-loop simulation.
2. **Digital-credit yield curve (live chart):** STRC, STRF, STRK, STRD and SATA effective yields vs Treasuries and high-yield credit, refreshed daily.
3. **Coverage stress test (Monte Carlo):** How far BTC can fall before Strategy's BTC reserve covers less than N years of preferred dividends, shown as a fan chart.
4. **"Sats per day" ticker (GIF):** SATA now pays dividends daily, so an animation shows what $10k of SATA streams in sats per day at today's BTC price.
5. **mNAV flywheel (animated diagram):** Issuance, then BTC bought, then BTC per share, as an interactive Streamlit page with sliders.
6. **Benchmark race (bar-chart-race video):** Frontier model scores on SWE-bench and ARC-AGI over time, by lab.
7. **Robotaxi expansion map (GIF):** Waymo, Tesla and Zoox service areas growing month by month.
8. **AI vs Bitcoin energy (crossover sim):** Data-center compute demand vs mining hashrate as flexible load, a good fit for the 20% lane.
