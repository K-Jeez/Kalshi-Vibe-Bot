# Research ledger runbook

This tool is preparation only. Installing it does not collect data, start the bot, or approve a study. It never sends or cancels orders, and it never reads account keys.

Open this file in Cursor’s preview. After a score, open `scorecard.md` the same way.

## What you will need later, if you authorize a study

You supply the files yourself. The tool does not download them.

Each forecast snapshot is one JSON object:

| Field | Meaning |
| --- | --- |
| `market_id` | Kalshi market ticker |
| `event_ticker` | Event id used to keep related markets together in the uncertainty estimate |
| `forecast_time` | When the probability was frozen. Timezone required, for example `2026-11-08T13:00:00Z` |
| `quote_time` | When the bid/ask was observed. Must be at or before `forecast_time`, and not older than the experiment’s max age |
| `model_id` | Must match the model id frozen in the experiment file |
| `ai_probability_yes` | Probability that the market resolves YES, from 0 to 1. Not a percent |
| `yes_bid`, `yes_ask`, `no_bid`, `no_ask` | All four prices, each between 0 and 1, bid not above ask |
| `model_version` | Optional short label |

Each official outcome, supplied after the market resolves:

| Field | Meaning |
| --- | --- |
| `market_id` | Same ticker |
| `resolved_at` | Official resolve time. Must be strictly after every stored forecast for that market |
| `result` | `yes` or `no` |

Made-up examples live in `research_ledger_fixtures/`. They are not bot history.

## Freeze the experiment before any rows

From the `backend` directory:

```
python tools/research_ledger.py init --dir /path/you/choose/ledger --config tools/research_ledger_fixtures/experiment.json
```

Use your own config when you actually start. The command writes `experiment.json` and a hash. If that file is edited later, scoring stops. The pre-registered window is every forecast at or after `score_not_before`. Earlier rows are stored as exploratory and are not the result.

`hypothetical.illustrative_fee_per_contract_usd` is an assumption you type. It is not a Kalshi fee quote. The hypothetical trade is one contract when the model’s side is at least `min_edge_points` above that side’s ask. It is not the live Kelly sizer, and it is not an order.

## Record and score offline

```
python tools/research_ledger.py record --dir /path/you/choose/ledger --snapshot snapshot.json
python tools/research_ledger.py resolve --dir /path/you/choose/ledger --outcome outcomes.json
python tools/research_ledger.py score --dir /path/you/choose/ledger --out /path/you/choose/score
```

Then open `scorecard.md` and `scorecard.json` in the output folder.

Repeating an identical snapshot does nothing. The same market, time, and model with a different probability or quote is rejected. A resolve time that is not after the forecast is rejected. A quote older than `max_quote_age_seconds` is rejected. Missing outcomes stay out of the averages. If the pre-registered window is empty, Brier, accuracy, and hypothetical P&L are omitted rather than filled with zeros.

When at least two event groups are in the window, the scorecard resamples those events together so one game does not look like dozens of independent bets.

## What this does not do

It does not start the bot, place orders, cancel orders, or call Kalshi or xAI. The trading activity log on the safety branch is a different file and is not used here. The synthetic edge study is also a different file. This ledger only scores rows you choose to put in it.
