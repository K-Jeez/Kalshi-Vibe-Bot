# Offline tools

These commands do not place orders and do not start the app.

## Read-only ledger audit

From the `backend` directory:

```
python tools/reconcile_export.py trading_bot.db
python tools/reconcile_export.py trading_bot.db --cash-start 100 --cash-end 98.83
```

The report lists closed entry cost, fees stored on those rows, realized P&L, open cost, and activity run ids. It counts an order as current only when its latest status is still pending, ambiguous, or open at the exchange. Older pending rows stay in the history count. A cash start and end are only compared with the stored realized P&L. A difference is marked unresolved. The tool does not invent a fee adjustment.

## Research ledger

See `RESEARCH_LEDGER.md`. It records forecasts and scores them later. It does not trade.
