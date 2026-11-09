# Dev process control

The dashboard Play, Pause, and Stop buttons are unchanged. These commands are another way to start the app and to set the same trading state.

There are two different things:

- **Process** means the backend and frontend programs on this computer.
- **Trading state** means Play, Pause, or Stop inside the running backend.

`launch`, `shutdown`, and `restart` change processes. They do not press Play. `bot play`, `bot pause`, and `bot stop` change trading state only. They do not start or kill the programs.

From the repo folder (the path can contain spaces):

```
cd /d "C:\Users\KJeez\Documents\VibeBot Project\Kalshi Vibe Bot"
vibe.bat status
vibe.bat launch
vibe.bat shutdown
vibe.bat restart
vibe.bat bot stop
vibe.bat bot pause
vibe.bat bot play
```

`start.bat` is `launch` and then opens the dashboard. `stop.bat` is `shutdown`. A bare `vibe.bat stop` is rejected so it cannot be confused with either command.

## What each command does

`launch` starts the backend with the same `backend/.env` as the UI, then starts the frontend on port 3000. The app lifespan sets the trading state to Stop. Launch checks that state and does not press Play. Position monitoring runs on its own timer, so a long market scan does not delay stop-loss checks. Stop still turns automatic exits off. Pause still allows them. Launch does not press Play. If the backend is not Stop, launch sends Stop once. If that still is not Stop, launch stops the processes it just started. Running launch again while the owned processes are healthy does not change Play, Pause, or Stop.

`shutdown` stops only the backend and frontend processes recorded for this repo. It checks the process id and the command line before it stops anything. It does not stop every Python or Node program, and it does not stop an unrelated program that happens to be using port 8000 or 3000. A stale or unreadable pid file does not get a process killed. Running shutdown again is safe.

`restart` is shutdown, then launch. It does not resume Play. The new process comes up in Stop.

`status` prints whether the owned processes are running and what trading state the local backend reports. It does not change either one.

`bot play`, `bot pause`, and `bot stop` call `POST /bot/state` on `127.0.0.1`, the same route the dashboard uses. If `LOCAL_API_TOKEN` is set, the command sends it and does not print it. These commands do not place orders, cancel orders, or change strategy settings.

## Risks

Pause stops new buys. If stop-loss selling is enabled in Settings, Pause can still sell.

Stop stops new buys and turns off automatic exits. Stop does not cancel resting orders at Kalshi.

Closing the backend process does not cancel resting orders at Kalshi. The next launch comes up in Stop and does not press Play.

## First check on the Windows machine

The processes already open from the old `start.bat` are not in the new pid file. The new shutdown will not close them, and it will not kill whatever else is on the port. Close those two windows yourself (the old titles were KalshiBot-Backend and KalshiBot-Frontend). Then:

1. `vibe.bat status` while the old app is still up, before you close it. This is read-only. It should report the trading state and say the process is not one this control started.
2. Close the old backend and frontend windows by hand.
3. `vibe.bat launch`
4. `vibe.bat status` and confirm the trading state is stop.
5. `vibe.bat bot stop` to confirm the state route. This does not cancel resting orders.
6. `vibe.bat shutdown` when you want the new processes stopped.

Do not run `vibe.bat bot pause` or `vibe.bat bot play` in that first check. Play can place real orders. Pause can sell when stop-loss selling is enabled.

## Checkout, migration, and read-only audit

Do not start the app for this step. The commands below update the checkout, back up the database, and print a ledger audit. They do not press Play.

From the repo folder:

The Windows machine already has `venv\Scripts\python.exe` at the repo root. Do not create another virtualenv. The app is stopped. Do not start it.

```
cd /d "C:\Users\KJeez\Documents\VibeBot Project\Kalshi Vibe Bot"
git fetch origin cursor/live-run-repairs-6f97
git checkout cursor/live-run-repairs-6f97
venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend
..\venv\Scripts\python.exe tools\migrate_approved_bundle.py trading_bot.db
..\venv\Scripts\python.exe tools\reconcile_export.py trading_bot.db
```

`pip install` only updates packages that the requirements file asks for. The migration uses the SQLite backup API, so a WAL left by the stopped process is included. It writes a new `trading_bot.db.bak-YYYYMMDDTHHMMSSZ` file every run and does not overwrite `trading_bot.db.pre-bundle.bak` if that older file is already there. It adds missing `activity_events` columns, including `exchange_order_id`, `fee_kind`, and `fill_count_fp`. Once, even if an older cap-only patch is already recorded, it sets the open cap to 12, the minimum edge to 5, the buy-side win floor to 60, and the stop to 0.80. A later edit in the database is not rewritten the next time the command runs. Other tuning fields and history stay. It does not start the server.

The cash comparison is read-only and comes after that command. Pass statement figures when you have them:

```
..\venv\Scripts\python.exe tools\reconcile_export.py trading_bot.db --cash-start 100 --cash-end 98.83
```

A gap stays unresolved. It is not an 18-cent correction. Historical NO-side fees that were never stored are not reconstructed. The shard error root is not proven.

Rollback of this checkout, without deleting the database, secrets, or a backup, is:

```
git checkout 3c5289997a6412a68cc5373e691bf2d950404d5a
```

That returns the code to the prior revision. It does not restore a database backup. Use the timestamped backup file if the database itself has to be put back, and do that only while the app is stopped.
