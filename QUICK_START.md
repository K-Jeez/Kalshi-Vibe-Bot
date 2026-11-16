# Quick start

Get the bot running with **live Kalshi trading** (real orders against Kalshi’s API). Have API credentials and a funded account before you turn the bot to **Play**.

---

## What you need

- **Python 3.12+** — **3.12** is the most predictable; **3.13/3.14** work if you reinstall deps after cloning (`pip install -r backend/requirements.txt`) so SQLAlchemy and httpx match current pins.
- **Node.js 20+** (22 LTS recommended)

---

## One-time setup

Do these steps from the **Kalshi Vibe Bot** folder (the repo root—the same folder that contains `start.bat`).

### 1. Python environment

**Windows (PowerShell or Command Prompt):**

```text
cd "Kalshi Vibe Bot"
python -m venv venv
venv\Scripts\activate
pip install -r backend\requirements.txt
```

**macOS / Linux:**

```text
cd "Kalshi Vibe Bot"
python3.12 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
```

### 2. API keys and Kalshi key file

1. Copy the environment template to a real config file:
   - **Windows:** `copy backend\.env.template backend\.env`
   - **Mac/Linux:** `cp backend/.env.template backend/.env`

2. Open **`backend/.env`** in a text editor and fill in at least:
   - **`KALSHI_API_KEY`** — from [Kalshi API settings](https://kalshi.com/account/api)
   - **`KALSHI_BASE_URL`** — leave the template default `https://external-api.kalshi.com` unless Kalshi directs otherwise
   - **`XAI_API_KEY`** — from [xAI Console](https://console.x.ai/) (required for market analysis)
   - **`XAI_MODEL`** — optional Grok model id (default `grok-4.3` for scan cost; `grok-4.7` for flagship quality)

3. Kalshi also gives you a **private key file** when you create API access. Save it as **`backend/kalshi_private_key.pem`** (or another path and set **`KALSHI_PRIVATE_KEY_PATH`** in `.env` to match).

4. **Optional — verify Kalshi auth and parsing** (requires the key file above). From **`backend/`** with your venv activated:

   ```text
   python scripts/verify_kalshi_parsing.py
   ```

   Expect a short log: portfolio positions, one page of orders, an orderbook sample, then `OK: Kalshi live reads...`. It does not place trades.

   **Repair scripts:** if quantities or open P&L drift vs Kalshi after trading outside the bot, from **`backend/`** run `python scripts/refresh_open_live_positions_from_kalshi.py` (open legs) and/or `python scripts/refinalize_live_closed_pnl.py` (closed legs).

### 3. Frontend dependencies

```text
cd frontend
npm install
cd ..
```

The dashboard listens on the LAN at port 3000 and calls the API on the same host, port 8000. From a phone on the home network, open `http://<PC LAN IP>:3000`. Set `VITE_API_BASE_URL` only if the API is not on port 8000 of that same host. Keep **`ENABLE_DEBUG_RAW_KALSHI=false`** unless you need the `GET /debug/raw` Kalshi debug route.

`HOST` defaults to **`0.0.0.0`**. This computer and private LAN addresses can press Play. A public address still needs `LOCAL_API_TOKEN`. Allow the Windows private firewall profile for ports 3000 and 8000:

```powershell
New-NetFirewallRule -DisplayName "Kalshi Vibe Bot LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3000,8000 -Profile Private
```

---

## Run the app

From the repo root:

1. Double‑click **`start.bat`** (Windows), **or** run it from a terminal in this folder.

That starts the backend and frontend and should open the dashboard in your browser. The trading state stays **Stop** until you press Play.

- If it doesn’t open automatically, go to **http://localhost:3000**
- The API lives at **http://localhost:8000**

To stop the processes **this control started**, run **`stop.bat`** or **`vibe.bat shutdown`**. That does not cancel resting Kalshi orders. Command-line Play, Pause, and Stop, and the difference between closing the app and changing trading state, are in [DEV_CONTROL.md](DEV_CONTROL.md).

---

## First-time checklist in the UI

1. Confirm Kalshi keys and balance look right on the dashboard before **Play**.
2. Set the bot to **Play** when you want it to scan and trade automatically; **Pause** or **Stop** when you want it idle.

All trading is **real money** on Kalshi (live-only): fill-or-kill **buys** at the price where the whole size is resting; **exits** use **stop-loss only** from the executable bid, with no grace period. Strategy defaults come from **`backend/.env`** and **`config.py`** (min edge **4**, min AI win **55%**, stop-loss **80%** drawdown, gross upside **0.10**, buy window **`BOT_MAX_HOURS` 4**). The buy window is not one of the Settings rows stored in **`tuning_state`**. **Settings** adjusts those knobs in **`tuning_state`** (applied immediately) and shows the running **`XAI_MODEL`**. Market analysis is **xAI Grok** only (`XAI_API_KEY` / `XAI_MODEL` in `.env`; default **`grok-4.3`**). Additional caps (22-point edge cap, 26¢ minimum price, Kelly limit, sports volume) are automatic. There is no cap on the model’s probability. See [README.md](README.md).

3. If you trade outside the bot or restart after downtime, open **Settings** and use **Reconcile with Kalshi** so open quantities, invested/fees basis, buy-order entry economics, unrealized marks, and closed P&L stay aligned with Kalshi (see README ``POST /portfolio/live/reconcile``). For a CLI-only open-leg refresh, use ``python scripts/refresh_open_live_positions_from_kalshi.py`` from ``backend/``.

---

## Need more detail?

See **[README.md](README.md)** for configuration options, troubleshooting, and how the bot behaves under the hood.
