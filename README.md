# Kalshi Vibe Bot

Live helper for binary [Kalshi](https://kalshi.com/) markets. It scans markets that can actually be bought, asks xAI (Grok) for the chance the contract settles YES, and buys when that chance clears your edge and confidence rules. Positions stay open until Kalshi settles them.

App version **2.1.0**. The model default is **grok-4.3**.

## What it does

1. Local checks drop markets that are closed, thin, too far away, or have no side priced between 50¢ and 85¢.
2. Grok returns a calibrated chance the **contract** settles YES. A spread, total, or player prop is not “who wins the game.”
3. The server picks YES or NO by comparing that chance with the ask. It buys only when the edge and the model’s confidence both clear your settings, and the edge is not above the cap (`MAX_EDGE_TO_BUY_PCT`, 22 by default; 0 turns the cap off).
4. Size is the edge, in points, as a percent of available cash. A 10-point edge uses 10% of cash. The cost includes the estimated fee and is rounded down to whole contracts. If that cannot buy one contract, the bot skips. It never rounds up to one contract. Set `FLAT_STAKE_DOLLARS` above 0 to use a fixed dollar amount instead.
5. The bot does not sell early. Kalshi settlement closes the position.

## Round 6, for the record

124 settled positions, 70% wins, average entry 70.6¢. The model claimed 81.1% on the bought side, about 11 points high. Result: **−$27.39** on $497 staked. Edges of 15 points or more went 57% and lost $16.50. Buys from 70¢ to 80¢ lost $36.25. That is why size is now a plain percent of cash, the price window is 50–85¢, and there is no early sell.

## Windows setup

From the project folder:

- `start.bat` starts the backend and the dashboard.
- `stop.bat` stops them.

Or use `python backend/tools/dev_control.py`. The dashboard listens on port 3000 and the API on port 8000. Both bind to the LAN (`0.0.0.0`). Changing state (play, pause, settings) is limited to this computer and private LAN addresses. A public address needs `LOCAL_API_TOKEN`.

Copy `backend/.env.example` to `backend/.env` and fill in the Kalshi and xAI keys. Restart after key or model changes. The Settings page stores the minimum edge and the model confidence in the database. Restore configuration defaults copies those two back from `.env`. Other keys are read from `.env` at startup.

On a phone on the same Wi-Fi, open `http://<this-pc-lan-ip>:3000`. The menu button in the header switches pages.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `KALSHI_API_KEY` | empty | Kalshi key id |
| `KALSHI_PRIVATE_KEY_PATH` | `./kalshi_private_key.pem` | RSA private key |
| `KALSHI_BASE_URL` | `https://external-api.kalshi.com` | Trade API host |
| `XAI_API_KEY` | empty | xAI inference key |
| `XAI_MODEL` | `grok-4.3` | Chat model |
| `XAI_TEAM_ID` | empty | Optional, for the prepaid balance tile |
| `XAI_MANAGEMENT_API_KEY` | empty | Optional, paired with the team id |
| `AI_TEMPERATURE` | `0.1` | Sampling temperature |
| `MIN_EDGE_TO_BUY_PCT` | `4` | Minimum edge in points. Saved in Settings. |
| `MAX_EDGE_TO_BUY_PCT` | `22` | Skip larger edges. `0` turns this off. |
| `MIN_AI_WIN_PROB_BUY_SIDE_PCT` | `70` | Minimum model chance on the bought side. Saved in Settings. |
| `MIN_ENTRY_PRICE_CENTS` | `50` | Lowest ask the bot will buy |
| `MAX_ENTRY_PRICE_CENTS` | `85` | Highest ask the bot will buy. This is the cap on the market price, not on the model’s probability. |
| `FLAT_STAKE_DOLLARS` | `0` | `0` uses edge-as-percent-of-cash. Above 0, every buy uses that many dollars. |
| `DAILY_XAI_BUDGET_USD` | `0` | Denver-day cap on recorded model spend. `0` means no cap. |
| `BOT_SCAN_INTERVAL` | `10` | Seconds between scans |
| `BOT_MAX_SCAN_QUEUE_UNITS_PER_SWEEP` | `50` | Highest-volume markets analyzed per sweep. `0` means no cap. |
| `BOT_MIN_VOLUME` | `1500` | Minimum 24-hour contract volume. Sports use at least 2000. |
| `BOT_MAX_HOURS` | `4` | Only markets that end within this many hours |
| `BOT_MARKETS_FETCH_MAX_CLOSE_HOURS` | `720` | How far ahead to ask Kalshi for listings |
| `BOT_MAX_SPREAD` | `0.15` | Maximum bid-ask spread |
| `BOT_MIN_TOP_SIZE` | `1` | Minimum contracts at the ask |
| `REENTRY_COOLDOWN_MINUTES` | `120` | Wait after a losing close before buying that ticker again. `0` is off. |
| `LOCAL_MIN_RESIDUAL_PAYOFF` | `0.10` | Minimum profit per contract if the side wins. `0` is off. |
| `VIBE_LIVE_FACTS_ENABLED` | `true` | Use free score and price feeds before the model |
| `VIBE_PREFILTER_SERIES_PREFIXES` | esports and challenger prefixes | Skip these series before the model |
| `VIBE_NUMERIC_PREFILTER_SERIES_PREFIXES` | metals and index prefixes | Skip these series before the model |
| `VIBE_LIVE_SEARCH_ENABLED` | `false` | Paid web and X lookup. Off means zero search calls. |
| `VIBE_LIVE_SEARCH_MAX_TOOL_CALLS` | `1` | Tool-call cap when search is on |
| `VIBE_LIVE_SEARCH_WITHIN_SEC` | `10800` | Treat a market as in-play inside this many seconds |
| `VIBE_LIVE_SEARCH_CACHE_SEC` | `300` | Share one lookup across props on the same event |
| `VIBE_LIVE_SEARCH_MAX_PER_SWEEP` | `8` | Paid lookups per sweep. `0` means no cap. |
| `CLOSED_RESOLUTION_REFRESH_INTERVAL_SEC` | `450` | How often to ask Kalshi for missing results |
| `CLOSED_RESOLUTION_REFRESH_BATCH` | `25` | Rows checked each time |
| `PORT` | `8000` | API port |
| `HOST` | `0.0.0.0` | Listen on the LAN |
| `LOCAL_API_TOKEN` | empty | Required only for state changes from a public address |
| `ENABLE_DEBUG_RAW_KALSHI` | `false` | Leave false |

An old `.env` may still contain `STOP_LOSS_DRAWDOWN_PCT`, `STOP_LOSS_SELLING_ENABLED`, `EXIT_GRACE_MINUTES`, `VIBE_MODEL_BLEND_WEIGHT`, or `VIBE_BLEND_EV_GATE`. Those keys are ignored. The bot does not sell before settlement.

## LAN

Allow inbound TCP 3000 and 8000 on the private Windows firewall if a phone cannot connect:

```powershell
New-NetFirewallRule -DisplayName "Kalshi Vibe Bot LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3000,8000 -Profile Private
```
