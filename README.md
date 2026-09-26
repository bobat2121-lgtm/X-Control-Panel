# 🛰️ X Control Panel

AI agents draft your X posts (80% Bitcoin and digital credit, 20% AI). You edit, press **Post on X**, and the system learns from what you post.

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
  Showcase gallery (public Streamlit app) ◄── the builds you post Mon / Wed / Fri
```

**Schedule (New York time):**

| When | What happens |
|---|---|
| ☀️ 7:05 Mon–Fri | Pre-market drafts ready by about 7:20 for your 8:00 post. **Monday is a showcase slot.** |
| 🤖 11:20 daily | AI ideas for noon (posting is optional) |
| 🕜 12:50 Mon–Fri | Midday drafts for 1:30. **Wednesday is a showcase slot.** |
| 🔔 16:10 Fri | After-close drafts for 4:30. **Showcase slot.** |
| 🌙 21:30 daily | Pull your post metrics, generate 3 new build ideas, warn if tomorrow's showcase isn't ready |
| 📅 Sun 17:00 | Weekly memo, 6 bigger ideas, auto-fill empty showcase slots |

## Try it locally (no keys needed)

```powershell
.venv\Scripts\python -m jobs.run seed-demo
.venv\Scripts\python -m streamlit run streamlit_app.py
```

`seed-demo` uses a mock LLM with live market, SEC and RSS data. Delete `data\app.db` to start clean.

---

## Go live in the cloud (about 45 minutes, once)

### 1. Private GitHub repo
Create a **private** repo (e.g. `x-control-panel`) and push this folder to it. `.gitignore` already keeps `.env`, `data/` and logins out.

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
| `DISCORD_WEBHOOK_URL` | your Discord webhook |
| `SEC_USER_AGENT` | `Your Name you@email.com` (the SEC requires a contact) |
| `FRED_API_KEY` | optional, [free key](https://fred.stlouisfed.org/docs/api/api_key.html) |

Variables (same page, Variables tab): `PANEL_URL` (your panel link, used in Discord alerts) and optionally `CODEX_MODEL`.

### 6. The panel on Streamlit Community Cloud (free, private)
At [share.streamlit.io](https://share.streamlit.io), click **Create app**, pick your private repo, and set the main file to `streamlit_app.py`.

- **Settings → Secrets:** paste the values from `.streamlit/secrets.toml.example`.
- **Sharing:** only people you invite can view the app. Invite only your own email.

For instant rewrites and "Run now", create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to **only this repo** and the **Actions: Read and write** permission. Use it as `GH_DISPATCH_TOKEN`. Without it, button requests wait for the next scheduled run.

### 7. First run
Actions → **agent** → Run workflow → job `premarket`. About 3 minutes later there are drafts in the Feed and a ping in Discord.

### 8. Showcase gallery (public)
See [showcase_gallery/README.md](showcase_gallery/README.md). It's a separate public repo and app for the Mon/Wed/Fri builds, and each build gets its own link.

---

## Daily use
- **Feed:** pick option A/B/C and edit inline (it saves automatically). Use one-click AI rewrites, 🚀 Post on X (opens X's composer), then ✅ Posted with the URL.
- **Build Lab:** assign builds to the Mon/Wed/Fri lineup and 🧩 copy the build prompt into Claude Code or Codex. Mark an idea ✅ Ready with its link, and the showcase slot writes the launch post.
- **Radar:** stories behind the drafts, ✍️ Draft this, reply opportunities, and raw signals.
- **Scoreboard:** the 80/20 mix and what's working.
- **Control Room:** watchlist, voice profile, schedule, market inputs (BTC holdings, STRC and SATA rates), calendar, and run logs.

## Things to keep current
- **Voice** (Control Room → Voice): paste 20+ of your best posts. This is the biggest quality lever.
- **Market inputs:** Strategy's and Strive's BTC holdings after their 8-Ks, and the STRC rate each month.
- **Calendar:** CPI, FOMC, jobs, earnings. The writer plans around them.

## Troubleshooting
- **"No Codex login in the database" or 401 errors:** redo step 4.
- **An X search returns 400:** some operators (e.g. `$cashtag`) may not be enabled for your API tier. Edit the query in Control Room → Watchlist → Keyword searches.
- **A run starts late:** GitHub's cron can lag 5–15 minutes. The pre-market job starts at 7:05 to leave room.
- **Costs:**
  - X reads are capped per day.
  - Codex uses your ChatGPT plan's limits: about 5–7 agent runs a day, plus rewrites.
  - GitHub Actions uses roughly 600–1,000 of the 2,000 free minutes a month on a private repo.
