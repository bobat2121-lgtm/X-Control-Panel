# 🛰️ X Control Panel

A news monitor and research desk for your X account (80% Bitcoin and digital credit, 20% AI). It watches your watchlist on X, crypto outlets, regulators, Google News topics and SEC filings, and pings you as stories break. At your slot times an AI desk writes briefs (what happened, why it matters, the numbers, angle questions); you write the posts. Writer mode **Drafts** (Control Room → Settings) brings back AI-written drafts when you want them.

```
                    GitHub Actions (cloud, on a schedule)
  ┌───────────────────────────────────────────────────────────────────────┐
  │ Market Desk ─┐                                                        │
  │ X API scan ──┼─► Scout + Writer + Editor ──► drafts ─┐                │
  │ SEC 8-Ks ────┤   (Codex CLI signed in with          │                │
  │ RSS news ────┘    your ChatGPT account)             ├─► Postgres ◄────┼──► Streamlit panel (private)
  │ Nightly: your post metrics ─► Analyst + Strategist ─┘      │         │     Feed · Build Lab · Radar
  └──────────────────────────────────────────────────────────────┼───────┘     Scoreboard · Control Room
                                                                 └──► Discord alerts
  Showcase watcher (separate workflow) ──► renders + audits your Digital Credit Report panel ──► Feed + Discord
      reads bobat2121-lgtm/digital-exposure (read-only) and its 8-K feed
```

**Schedule (New York time):**

| When | What happens |
|---|---|
| 📡 every ~15 min | **Monitor**: your watchlist on X, news feeds (Cointelegraph, CoinDesk, The Block, Decrypt, Bitcoin Magazine, The Defiant), SEC/CFTC/Fed press, Google News topics (digital credit, stablecoins, legislation, AI × stablecoins, frontier and physical AI) and SEC 8-Ks. Groups the same story across outlets and flags ⚡ priority news on the Monitor page (no Discord pings unless you switch them on). GitHub cron can run hours late, so an open panel also dispatches it whenever news is over 20 minutes old |
| ☀️ 7:05 Mon–Fri | Pre-market desk brief for your 8:00 post (Drafts mode: post drafts) |
| 🧾 Mon 7:40–11:30 | **Showcase: The Accretion Ledger.** Waits for both weekly 8-Ks, then renders, audits and drafts it (usually 8:10–8:30). Tuesday after an EDGAR Monday holiday |
| 🤖 11:20 daily | AI-lane desk brief for noon (posting is optional) |
| 🕜 12:50 Mon–Fri | Midday desk brief for 1:30 |
| 🎟 Wed 12:30 | **Showcase: The Coupon Sheet**, audited for the 1:30 post |
| 🔔 16:10 Fri | After-close desk brief for 4:30 |
| 🔔 Fri 16:05 | **Showcase: The Closing Mark**, once the 4:00 pm closes have settled (about 4:10–4:20) |
| 🌙 21:30 daily | Pull your post metrics, generate 3 new build ideas |
| 🧪 Sun/Tue/Thu ~20:13 | Preflight: will tomorrow's panel render cleanly with digital-exposure's current code? Pings only if not |
| 📅 Sun 17:00 | Weekly memo, 6 bigger ideas, this week's showcase lineup |

**How a showcase is checked.** The watcher runs digital-exposure's own `render_previews.py` (the same code as the page's *Download X image* button), its `audit_panels.py` (independent PASS/WARN/FAIL checks) and, on Monday, `check_monday_publication.py`. It drafts the post only when:

- **Monday:** both weekly 8-Ks are in the feed and validated, and the image shows their balance dates. Every 8-K figure must match the image: BTC bought and held, USD reserve and cash, common and preferred ATM, Strive cash and SATA. The edition must also be complete, with no "retaining last edition" notice.
- **Wednesday:** the flow ledger includes the latest 8-K week, and strategy.com, Strive and the STRC/SATA quotes are current.
- **Friday:** it's past 4:00 pm plus 10 minutes, the panel has rolled to this week, and today's closes and the BTC 4 pm mark are in.
- **Every day:** digital-exposure's audit has 0 FAIL, and no source used by that image fell back to a saved snapshot. The image has no blank values or text overflow, and is 1440 wide and no taller than 3:4.

"Not yet" results retry every few minutes. A real problem (an image figure that disagrees with the 8-K, a FAIL) shows in Control Room → Showcase (and in Discord only if you switch on "showcase waits"). If digital-exposure's `main` stops rendering, the watcher uses the last commit that rendered cleanly and says so.

## Try it locally (no keys needed)

```powershell
.venv\Scripts\python -m jobs.run seed-demo
.venv\Scripts\python -m streamlit run streamlit_app.py
```

`seed-demo` uses a mock LLM with live market, SEC and RSS data. Delete `data\app.db` to start clean.

---

## Go live in the cloud (about 45 minutes, once)

### 1. GitHub repo
Push this folder to a GitHub repo. `.gitignore` already keeps `.env`, `data/`, `.streamlit/secrets.toml` and logins out. The repo can be public: every secret lives in GitHub and Streamlit secrets, and a weekly keepalive stops GitHub from switching off the schedule on a public repo.

### 2. Database (free)
Create a free Postgres database at [neon.tech](https://neon.tech) (or Supabase) and copy the connection string. That is your `DATABASE_URL`.

### 3. X API (pay-per-use)
At [developer.x.com](https://developer.x.com), create a project and app, load a small amount of credit, and copy the app's **Bearer Token** (`X_BEARER_TOKEN`). Reads cost about $0.005 per post, with hard caps in Control Room → Settings (default 900 posts per day, about $4.50).

### 4. Your ChatGPT account for the agents (Codex CLI)
Use a **dedicated** Codex login for the cloud so it never fights with Codex on your PC:

```powershell
npm install -g @openai/codex
mkdir .codex-cloud
Set-Content .codex-cloud\config.toml 'cli_auth_credentials_store = "file"'
$env:CODEX_HOME = "$PWD\.codex-cloud"; codex login; Remove-Item Env:CODEX_HOME
```

Sign in with your ChatGPT account in the browser window that opens. Then encrypt the login and store it in the cloud database:

```powershell
.venv\Scripts\python -m jobs.run gen-key          # copy the output: this is CODEX_AUTH_KEY
# put DATABASE_URL and CODEX_AUTH_KEY into .env, then:
.venv\Scripts\python -m jobs.run upload-codex-auth
Remove-Item -Recurse .codex-cloud                # the DB copy is now the only one
```

The agent restores this login on every run and saves refreshed tokens back automatically. If it ever expires, repeat step 4.

### 5. GitHub Actions secrets
Repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `DATABASE_URL` | from step 2 |
| `CODEX_AUTH_KEY` | from step 4 |
| `X_BEARER_TOKEN` | from step 3 |
| `DISCORD_WEBHOOK_URL` | your Discord webhook (by default it only gets a ping when a Digital Credit Report panel goes live for its post time; Control Room → Settings → "Discord pings for" adds more) |
| `SEC_USER_AGENT` | `Your Name you@email.com` (the SEC requires a contact) |
| `FRED_API_KEY` | optional, [free key](https://fred.stlouisfed.org/docs/api/api_key.html) |

Variables (same page, Variables tab): `PANEL_URL` (your panel link, used in Discord alerts) and optionally `CODEX_MODEL`.

### 6. The panel on Streamlit Community Cloud (free, private)
At [share.streamlit.io](https://share.streamlit.io), click **Create app**, pick your private repo, and set the main file to `streamlit_app.py`.

- **Settings → Secrets:** paste the values from `.streamlit/secrets.toml.example`, including `PANEL_PASSWORD`.
- **Access:** the free tier allows one private app, so this panel can run as a public app. Visitors are **view-only**: they can read drafts, ideas and stats, but every control that changes something (editing, rewrites, runs, settings) stays hidden until you unlock it with **🔑 Owner** and your `PANEL_PASSWORD`. The Control Room is owner-only. Unlocking remembers that browser for 30 days (a signed cookie), so reloads and redeploys don't sign you out; **🔒 Lock** forgets it, and changing `PANEL_PASSWORD` signs out every remembered browser. If you have a free private slot, make the app private instead.

For instant rewrites and "Run now", create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to **only this repo** and the **Actions: Read and write** permission. Use it as `GH_DISPATCH_TOKEN`. Without it, button requests wait for the next scheduled run.

### 7. First run
Actions → **agent** → Run workflow → job `premarket`. About 3 minutes later the pre-market desk picks are in the Monitor idea feed.

### 8. Showcase (Digital Credit Report)
Nothing to set up: the **showcase** workflow uses the same `DATABASE_URL` and `DISCORD_WEBHOOK_URL` secrets and reads the public digital-exposure repo. To test it any time: Actions → **showcase** → Run workflow → mode `preflight` (renders all three panels, posts nothing), or Control Room → 🛠 Showcase → 🧪 Preflight. Windows and titles are in Control Room → Settings → Showcase panels.

The old Build Lab gallery (`showcase_gallery/`) is no longer in the showcase rotation. Build launches go out in regular slots.

---

## Daily use
- **The look:** no panel, just floating islands (paper cards and slabs in wooden frames, lacquer plaques for loose captions) over a living pixel-art world (panel/scene.js): the Chinese countryside with farms, villages, rice terraces and a lake under misty mountains, and Japanese castles floating overhead. One full day every 5 minutes (synced to the clock, so every page shows the same hour). Farmers, samurai and villagers walk the paths, and at night they launch sky lanterns and set lanterns on the lake. Islands cast shadows on the world and glow like lanterns at night; the 🏮 button (bottom left) hides everything to watch the world (Esc returns). Add `?tod=0.9` to the URL to start at a given hour (0 = midnight, 0.5 = noon). It runs in your browser only; nothing is added to the server or the database.
- **Bookmark icons:** every page has its own tab title and pixel icon (panel/icons: 🏮 Monitor, 📜 Feed, ⚒️ Build Lab, 🧮 Scoreboard, 🏯 Control Room, plus the floating-castle app icon). Bookmark each page and the bookmark picks up its icon. `python panel/icons/make_icons.py` rebuilds them.
- **Monitor** (home page): the 🗞 **idea feed** puts post ideas three to a row under each post time (up next first; category pills and ⚡ priority-only filter). Each idea is a header saying what the post is on; click it and it opens full width: what happened, why it matters, the numbers, ways in, every news item with its source underneath, and related coverage. Write next to it, or press ↗ Writer tab to pin it beside a bigger editor (✍️ Writer keeps pins and your text). ☆ Save or ✕ Pass on each. Also 📡 Live wire (every story, refreshes every minute), 🎙 Your 7 (plus replies worth making) and ⭐ Saved. X reads stay under the monthly cap, with a share reserved for your watchlist so keyword searches can't crowd it out.
- **Feed:** pick option A/B/C and edit inline (it saves automatically). Use one-click AI rewrites, 🚀 Post on X (opens X's composer), then ✅ Posted with the URL.
- **Showcase days:** the 🟢 Discord message carries the audited image and caption A (facts only, taken from the image). Options B–D with more voice follow a couple of minutes later. Save the image, open X with the caption, and attach it. The Feed card has the same image with a Download button and a 🔄 Re-check.
- **Build Lab:** 🧩 copy the build prompt into Claude Code or Codex. **Add launch draft** puts a finished build's post in any slot.
- **Radar:** stories behind the drafts, ✍️ Draft this, reply opportunities, and raw signals.
- **Scoreboard:** the 80/20 mix and what's working.
- **Control Room:** watchlist, voice profile, schedule, market inputs (BTC holdings, STRC and SATA rates), calendar, and run logs.

## Things to keep current
- **Voice** (Control Room → Voice): paste 20+ of your best posts. This is the biggest quality lever.
- **Market inputs:** automatic. BTC holdings come from the latest weekly 8-K, STRC's rate from strategy.com and SATA's from strive.com, each with its date and source (Control Room → Market inputs). The values you type there are only a fallback.
- **Calendar:** automatic nightly: FOMC, GDP and PCE, STRC record/pay dates, MSTR/ASST earnings, and your report's curated STRC/SATA events. CPI and jobs dates need the BLS switch in Control Room → Calendar (BLS requires a contact email). Add your own events there; they're never touched.

## Troubleshooting
- **"No Codex login in the database" or 401 errors:** redo step 4.
- **An X search returns 400:** some operators (e.g. `$cashtag`) may not be enabled for your API tier. Edit the query in Control Room → Watchlist → Keyword searches.
- **A run starts late:** GitHub's cron can lag 5–15 minutes. The pre-market job starts at 7:05 to leave room.
- **Costs:**
  - X reads are capped per day.
  - Codex uses your ChatGPT plan's limits: about 5–7 agent runs a day, plus rewrites.
  - GitHub Actions uses roughly 600–1,000 of the 2,000 free minutes a month on a private repo.
