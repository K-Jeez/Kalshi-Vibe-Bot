# Offline tools

These commands do not place orders and do not start the app.

## Read-only ledger audit

From the `backend` directory:

```
python tools/reconcile_export.py trading_bot.db
python tools/reconcile_export.py trading_bot.db --cash-start 100 --cash-end 98.83
```

The report lists closed entry cost, fees stored on those rows, realized P&L, open cost, and activity run ids. It counts an order as current only when its latest status is still pending, ambiguous, or open at the exchange. Older pending rows stay in the history count. A cash start and end are only compared with the stored realized P&L. A difference is marked unresolved. The tool does not invent an 18-cent fee adjustment. Historical NO-side fees that were never stored stay missing.

## Shadow score

```
python tools/shadow_eval.py rows.json
```

Each row needs `decided_at` and `resolved_at`. A missing time is excluded. An outcome at or before the decision is excluded. The score is not a live-edge result.

## Older offline edge study

The study below used the rules in that commit, including a 5% cash cap and a 30-position ceiling. The live bot now uses quarter Kelly, a 2.5% all-in entry estimate, and a maximum of 12 open positions. That study was not rerun.

This folder does not place orders and does not change the trading rules.

Open `OFFLINE_EDGE_REPORT.md` in Cursor’s preview for the written results. The charts next to it are:

- `offline_edge_outputs/terminal_return_by_scenario.svg`
- `offline_edge_outputs/brier_model_vs_market.svg`
- `offline_edge_outputs/summary.csv`

The 200-replication raw file `replications.csv` is produced by the script. It is not committed; regenerate it with the command below.

From the `backend` directory, with the project dependencies installed:

```
python tools/offline_edge_eval.py --self-check
python tools/offline_edge_eval.py --out /tmp/edge-eval --reps 200
```

The script stops if the small-account stake cap does not return 0. That check keeps the study on the corrected gates.

Unit tests for the study are in `backend/tests/test_offline_edge_eval.py`. A passing test only means the simulator still follows those rules. It is not a profit result.
