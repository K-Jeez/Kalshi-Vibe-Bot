# Offline edge evaluation

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
