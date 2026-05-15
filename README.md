# Kalshi Vibe Bot

Mostly vibe-coded, autonomous [Kalshi](https://kalshi.com/) helper for **binary** markets: **local vetting** rejects bad books quickly, **xAI Grok** returns **P(YES)** (0–100) plus a **direction** (YES / NO / SKIP), the server computes **edge** vs executable asks and **full Kelly** whole-contract order sizing (**cash-capped**; **one** contract when fractional Kelly is below one contract but edge at the ask remains), and open legs exit on **stop-loss only** (drawdown of **open cash basis** vs dashboard **Est. Value** after a grace period when auto-sell is enabled).

Fair warning: there is a *lot* going on under the hood. The backend is full of moving parts—parsing Kalshi payloads that do not always line up cleanly, normalizing timestamps and book fields, batching unrelated props apart, ranking line ladders, and running a pile of gates and Kelly math before anything hits the exchange. Trying to get toward **consistent** profits while also planning for every flavor of market (sports, weather bins, politics, crypto-adjacent stuff, etc.) and whatever shape the API happens to return that day is a genuinely hard puzzle. This project is still very much trial and error; knobs get tuned, edge cases show up, and behavior shifts as we learn what actually holds up in production.

DISCLAIMER: Betting markets are a gamble and you can lose money easily. Nobody can predict the future, including xAI. 

The included `.bat` files are for convenient Windows start/stop.

The strategy is to target short-horizon markets across diverse categories.

---

## Quick start

```
double-click start.bat (Windows users)
```

Activates the venv, starts backend + frontend, opens the UI. Use `stop.bat` to shut down.

---

## Operating modes

| Mode | Behavior |
|------|----------|
| **Play** | Full automation: scan, open positions, manage exits |
| **Pause** | No new positions; existing positions still managed |
| **Stop** | No scanning and **no automatic exits** (manual closes in the UI still work) |

**Play / Pause / Stop** resets to **Stop** when the backend restarts (safety). Strategy numbers in **Settings** / SQLite persist per paper or live mode.

---

## How decisions flow

1. Pull open **binary** markets from Kalshi (contractual `max_close_ts` window), then vet by **time to event end**: **`expected_expiration_time`** when present and still in the future (so sports props are not blocked by a distant contractual `close_time`). If it is missing or already past, the bot uses the soonest future instant among **vetting horizon** (earlier of `expected_expiration_time` and `occurrence_datetime` when present) and contractual **`close_time`**. Rows missing usable timestamps are skipped.
2. **Local vetting** (`is_tradeable_market` in `backend/src/bot/loop.py`) is pass/fail: volume, status, hardcoded **extreme YES/NO snapshot** skip (YES>90¢ and NO<10¢, or the reverse, on normalized ``yes_price``/``no_price``), spread and depth on at least one leg, the `BOT_MAX_HOURS` horizon rule (see above), and—when `LOCAL_MIN_RESIDUAL_PAYOFF` > 0—at least one leg that is **both** liquid and has gross upside `1 − ask` at or above that floor (same idea as the post–xAI buy gate, so skewed books do not spend tokens on analysis that cannot execute).
3. Passing markets are grouped by Kalshi **`event_ticker`**, then **partitioned** so unrelated props do not share one xAI call (see `backend/src/bot/event_batch_partition.py`): e.g. **line ladders** (same player stem, different numeric thresholds), **outcome codes** (home / away / tie), and **exclusive bins** (same-day high/low temperature brackets like `B88.5` / `T93` under `KXHIGH…` / `KXLOW…`). Each partition becomes **one** batched Grok call. **Line-ladder** partitions with more than three locally vetted legs are **shortlisted to the top three** by server-side volume, depth, and spread tightness before xAI (saves tokens; **``codes:``** and **``exclusive_bins:``** batches are not cut). The model picks a single **best** contract (or SKIP all), and the dashboard logs **only that** market’s analysis row.
4. The server derives **market-implied probability** from the executable **ask** on the buy side, **edge** (percentage points), and **full Kelly** whole-contract size (rounded down from Kelly stake, then **capped by deployable cash** at the execution premium). If Kelly rounds to **zero** contracts but the Kelly fraction at the ask is still **positive**, the bot buys **one** contract when cash can cover it. After xAI returns **BUY_YES** or **BUY_NO**, the bot executes only if **edge** meets the **effective** minimum **and** xAI’s win probability on that purchased side meets the **effective** minimum (defaults **1** edge points and **51%** win on the buy side from `.env` / **`tuning_state`**), with **stricter** edge and AI floors when a **baked-in contrarian buy tier** applies (buy-side implied ≤ 25%, xAI buy-side minus implied ≥ 15 points, then +5 edge points and +4 win-% points on top of your saved floors). **Scanning** for new ideas is gated by bot mode, deployable cash, and related checks — **not** by edge (edge is enforced at this pre-buy step). **No** edge at the ask, or **cannot** afford one whole contract at the execution price, blocks the buy.
5. **Live** buys use Kalshi **V2** create-order with **IOC** (immediate-or-cancel) **limit** orders sized by contract count: **whole-contract** fills only (partial fills possible when the book is thin; unfilled size is canceled). **Paper** uses simulated fills against the same quotes.
6. **Open positions** store an optional **`stop_loss_drawdown_pct_at_entry`** audit snapshot. After **`EXIT_GRACE_MINUTES`**, when **stop-loss auto-sell** is enabled (Settings / `STOP_LOSS_SELLING_ENABLED`), the monitor compares **open cash basis** (contract notional at open plus buy-side fees in `fees_paid`) to **display Est. Value** (per contract × quantity — same numbers as the dashboard) using the **current** **`STOP_LOSS_DRAWDOWN_PCT`** / Settings value (not the frozen snapshot). If **(basis − est value) / basis** meets the threshold, the bot issues a **stop-loss** exit (IOC reduce-only sells stepped down the book when bids exist). No automated take-profit or counter-trend exits.
7. **`REENTRY_COOLDOWN_MINUTES`** can block a new entry on the same ticker after a stop-loss.

---

## Efficiency notes

- The backend keeps **long-lived HTTP pools** for Kalshi and xAI and closes them on **shutdown** (`lifespan` in `backend/src/main.py`).
- The dashboard UI polls **`GET /dashboard/bundle`** so portfolio + positions stay in sync with **one** request per interval (plus slower polling when the browser tab is hidden).
- SQLite adds a **partial index** on xAI decision timestamps (`escalated_to_xai = 1`) to speed cooldown-style queries.
- **Line-ladder** xAI batches are **capped at three legs** after local ranking (volume, book depth, spread); see `LINE_LADDER_MAX_LEGS_FOR_XAI` in `event_batch_partition.py`.
- **Order search** (Kalshi market fetch for new entries + xAI) stays off while **Vault-adjusted deployable cash** is below **\$1** (`MIN_DEPLOYABLE_USD_FOR_ORDER_SEARCH` in `scan_eligibility.py`); the dashboard shows **Holding — deployable funds under \$1**.

---

## Configuration (`backend/.env`)

Copy **`backend/.env.template`** → **`backend/.env`**. Settings load from `backend/src/config.py` (Pydantic). Names are **case-insensitive**. **Restart the backend** after `.env` edits.

**Never commit `backend/.env`** — it holds API keys. The template has no secrets.

The **Settings** UI persists **`tuning_state`** per paper/live: **minimum edge to buy**, **xAI buy-side win-prob floor**, **max open positions**, **stop-loss drawdown**, and **stop-loss auto-sell**. Values apply immediately after save. **Restore configuration defaults** reloads strategy fields from your `.env` + `config.py` defaults into SQLite. Stricter **contrarian buy tier** thresholds and **Kelly order sizing** are fixed in code (`strategy_math.py`).

### Variables in `.env.template`

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_API_KEY` | _(required for live)_ | Kalshi key id |
| `KALSHI_PRIVATE_KEY_PATH` | `./kalshi_private_key.pem` | RSA PEM (resolved under `backend/`) |
| `KALSHI_BASE_URL` | `https://api.elections.kalshi.com` | REST base |
| `XAI_API_KEY` | _(required for AI)_ | xAI inference key |
| `XAI_MODEL` | `grok-3` | Model id |
| `AI_TEMPERATURE` | `0.1` | Sampling temperature |
| `XAI_TEAM_ID` | _(empty)_ | Optional: prepaid balance tile (with Management key) |
| `XAI_MANAGEMENT_API_KEY` | _(empty)_ | Optional: xAI Management API key |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `PAPER_STARTING_BALANCE` | `1000.0` | Starting paper cash (USD) |
| `MIN_EDGE_TO_BUY_PCT` | `1` | Min edge (pts) to execute a buy |
| `STOP_LOSS_DRAWDOWN_PCT` | `0.80` | Stop-loss drawdown vs open cash basis |
| `STOP_LOSS_SELLING_ENABLED` | `false` | Auto stop-loss exits when `true` |
| `BOT_MAX_OPEN_POSITIONS` | `20` | Max open positions before scan pauses |
| `CLOSED_RESOLUTION_REFRESH_INTERVAL_SEC` | `450` | Poll closed rows for official outcome (`0` = off) |
| `CLOSED_RESOLUTION_REFRESH_BATCH` | `25` | Max closed rows per outcome poll |
| `EXIT_GRACE_MINUTES` | `10` | Minutes after entry before stop-loss applies |
| `BOT_SCAN_INTERVAL` | `10` | Seconds between full scans |
| `BOT_MIN_VOLUME` | `1000.0` | Min 24h volume (contracts) |
| `BOT_MAX_HOURS` | `6` | Buy vetting window (hours to event end) |
| `BOT_MARKETS_FETCH_MAX_CLOSE_HOURS` | `720` | Kalshi list fetch horizon (contractual close) |
| `BOT_MAX_SPREAD` | `0.15` | Max spread on purchased leg (decimal $) |
| `BOT_MIN_TOP_SIZE` | `1.0` | Min top-of-book ask size (contracts) |
| `REENTRY_COOLDOWN_MINUTES` | `120` | Cooldown after stop-loss on same ticker (`0` = off) |
| `LOCAL_MIN_RESIDUAL_PAYOFF` | `0.10` | Min gross upside `1 − ask` on buy leg (`0` = off) |
| `PORT` | `8000` | API port |
| `HOST` | `0.0.0.0` | Bind address (`127.0.0.1` for local-only) |
| `ENABLE_DEBUG_RAW_KALSHI` | `false` | Enables `GET /debug/raw` when `true` |

### Vault (UI reserve)

The dashboard has a **Vault** tile. Vault cash is **reserved from trading**: `Available Cash = Uninvested Cash − Vault`. Transfers are internal budgeting only (they do not move money at Kalshi).

### Code-only defaults (not in `.env.template`)

These are set in `config.py` or client code. You can still add them to `.env` manually if needed (unknown keys are ignored unless defined on `Settings`).

| Setting | Default | Where |
|---------|---------|--------|
| `MIN_AI_WIN_PROB_BUY_SIDE_PCT` | `51` | `config.py` — edit in **Settings** UI |
| `BOT_LOOP_SCAN_TIMEOUT_SEC` | `1800` | `config.py` |
| `BOT_MAX_SCAN_QUEUE_UNITS_PER_SWEEP` | `0` (no cap) | `config.py` |
| `KALSHI_MARKETS_PAGE_DELAY_SEC` | `0.05` | `config.py` |
| `CORS_ORIGINS` | localhost:3000 | `config.py` (comma-separated) |
| `DATABASE_URL` | `backend/trading_bot.db` | `config.py` |
| xAI HTTP retries / timeouts | `3` / `55s` / `150s` batch | `xai_client.py` |

---

## UI

- **Dashboard:** portfolio tiles; open / closed tables; paper/live toggle (confirm for live).
- **AI Analysis:** decision, confidence, YES/NO context, reasoning; action taken (executed vs skipped).
- **Settings:** minimum edge to buy, stop-loss drawdown (as a **percentage** in the UI, stored as a fraction — **open cash basis** vs **Est. Value**), stop-loss auto-sell master switch, **Restore configuration defaults** (reloads strategy fields from `.env` into SQLite), **Reconcile with Kalshi** (live repair — see scripts below).

---

## Setup

**Prerequisites:** Python **3.12+** (install exactly from `backend/requirements.txt` so SQLAlchemy/httpx match). Node **18+**.

### Virtualenv (repo root)

```
cd "path\to\Kalshi Vibe Bot"
python -m venv venv
venv\Scripts\activate
pip install -r backend\requirements.txt
```

On macOS/Linux: `source venv/bin/activate` and the same `pip install`.

### Credentials

```
copy backend\.env.template backend\.env
```

Edit **`backend/.env`**: `KALSHI_API_KEY`, `XAI_API_KEY`, and place Kalshi’s PEM at **`backend/kalshi_private_key.pem`** (or set `KALSHI_PRIVATE_KEY_PATH`).

### Frontend

```
cd frontend
npm install
npm run lint
cd ..
```

Lint uses ESLint 9 flat config (`frontend/eslint.config.js`). If the UI is not on the default dev URL, set `VITE_API_BASE_URL` / `VITE_WS_URL` in `frontend/.env` and add the same origin to **`CORS_ORIGINS`** in `backend/.env` (optional; see code-only table above).

### Tests (optional)

```
pip install -r backend\requirements-dev.txt
pytest backend\tests -q
```

### Live DB repair scripts (from `backend/`)

| Script | Purpose |
|--------|---------|
| `python scripts/verify_kalshi_parsing.py` | Auth + read-only REST smoke test |
| `python scripts/refresh_open_live_positions_from_kalshi.py` | Resync open live rows |
| `python scripts/backfill_open_position_bid_marks.py` | Recompute bid marks for open live rows |
| `python scripts/refinalize_live_closed_pnl.py` | Recompute closed live P&amp;L |

---

## Strategy knobs

SQLite **`tuning_state`** (per **paper** / **live**) stores **`min_edge_to_buy_pct`**, **`min_ai_win_prob_buy_side_pct`**, **`max_open_positions`**, **`stop_loss_drawdown_pct`**, and **`stop_loss_selling_enabled`**. The **Settings** page edits these; **`POST /tuning/strategy-knobs`**, **`POST /tuning/stop-loss-selling`**, **`POST /tuning/reset-to-config-defaults`** persist and broadcast **`tuning_update`**. **Restore configuration defaults** reloads from **`config.py`** / your **`.env`** (fields present in the template plus code defaults for min xAI win %).

**Tuning endpoints** (see **`/docs`**): `GET /tuning/state`, `POST /tuning/strategy-knobs`, `POST /tuning/stop-loss-selling`, `POST /tuning/reset-to-config-defaults`.

---

## Manual start (no `start.bat`)

Terminal 1 (venv on, repo root): `python backend\run.py`  
Terminal 2: `cd frontend` → `npm run dev`  
UI: http://localhost:3000 — API http://localhost:8000 (`/docs` for OpenAPI).

---

## Live trading

- Switch **PAPER → LIVE** in the header after PEM + API key are correct.
- **`GET /portfolio` (live)** accounts for resting Kalshi orders where applicable.
- **Unrealized P&amp;L** in the API uses **Est. Value** / intrinsic marks when Kalshi supplies them; otherwise bid-style marks while the contract is still tradeable.
- **`POST /trade` (live):** `limit_price` caps **IOC limit** buys; orders are placed by **contract count** (Kalshi V2).

---

## Troubleshooting

| Problem | What to try |
|--------|-------------|
| `ModuleNotFoundError` | Activate venv |
| Empty UI / no analyses | Backend up? Keys? Bot on **Play**? |
| xAI failures | `XAI_API_KEY`, billing, model id |
| SQLAlchemy / httpx errors on new Python | `pip install -r backend/requirements.txt` |
| Port 3000 busy | Vite tries 3001, 3002, … |
| Port 8000 busy | Set `PORT` in `.env` |
| DB schema errors | Delete `backend/trading_bot.db` (recreates) |
| Everything SKIP | Lower `MIN_EDGE_TO_BUY_PCT` or min xAI win % in Settings, widen vetting (`BOT_MAX_HOURS`, `BOT_MIN_VOLUME`, `BOT_MAX_SPREAD`, `LOCAL_MIN_RESIDUAL_PAYOFF`), or ensure markets pass local checks |
| Exit IOC canceled | Ticker mismatch; restart backend and compare to Kalshi |
| No bids on exit | Wait for liquidity or settlement |

---

## Project layout (backend)

| Path | Role |
|------|------|
| `backend/run.py` | Entry |
| `src/main.py` | FastAPI app |
| `src/api/` | REST + WebSocket |
| `src/bot/loop.py` | Scan → vetting → xAI → trade → position monitor |
| `src/bot/event_batch_partition.py` | Event-batch grouping + line-ladder xAI shortlist |
| `src/bot/scan_eligibility.py` | When market scan + xAI runs (balance / prepaid / deployable gates) |
| `src/decision_engine/analyzer.py` | xAI wrapper + strategy payload |
| `src/decision_engine/strategy_math.py` | Edge, full Kelly order sizing (cash-capped; single-contract fallback) |
| `src/clients/` | Kalshi, xAI |
| `src/database/models.py` | ORM + migrations |
| `src/api/tuning.py` | Tuning REST + WebSocket **`tuning_update`** payloads |

---

## Database (`backend/trading_bot.db`)

| Table | Contents |
|-------|----------|
| `positions` | Open/closed, P&amp;L, `cash_basis` (API), stop-loss snapshot, exit reason; `entry_decision_log_id` links the opening gate’s `decision_logs` row (per-leg AI in the UI) |
| `trades` | Ledger |
| `decision_logs` | Analyses |
| `bot_state` | Play / Pause / Stop |
| `tuning_state` | Min edge, min xAI win % on buy side, max open positions, stop-loss drawdown, stop-loss master switch |
| `vault_state` | Per-mode reserved cash (Vault tile) |

Delete the DB file to reset (recreates on start).

---

## Stack

**Backend:** FastAPI · Uvicorn · SQLite · SQLAlchemy · httpx · Pydantic · cryptography (RSA-PSS)  
**Frontend:** React 18 · TypeScript · Vite · Tailwind · Recharts  
**AI:** xAI Grok  
**Exchange:** Kalshi REST v2 (sign path **without** query string)

---

## Architecture

```
start.bat
  ├── backend/run.py     FastAPI + bot loop
  └── frontend/          Vite (~3000) — Dashboard, AI Analysis, Performance & History, Settings
```

---

## ☕ Support the Vibe

I poured a lot of evenings, weekends, and **way too many AI tokens** into building this Kalshi Vibe Bot (the trial-and-error tax is real 😂).

I am not a dev. I'm just a random tech nerd trying to get rich by having our robot overlords make decisions for me.

If you're getting good signals from it and want to say thanks, every coffee / vibe tip helps keep me motivated to improve it:

- **[Sponsor on GitHub](https://github.com/sponsors/K-Jeez)**
- **[Venmo](https://venmo.com/u/kevingarn)**

No pressure at all — just happy to share the bot with the community!
