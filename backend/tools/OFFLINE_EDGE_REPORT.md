# Does this bot have a trading edge?

Charts and the summary table for this write-up are in `offline_edge_outputs/` beside this file. Open the SVG files in Cursor’s preview.

**Short answer:** This repository cannot show that it does. The bot’s real forecasts and results were cleared and are not in git. A seeded synthetic stress test of the corrected buy rules is not a substitute for those records.

Reviewed code: safety commit `c2994d3144da55e985505274711d5d98bd191d50`. The strategy files were not changed for this study. No Kalshi or xAI call was made. Passing unit tests are not performance evidence.

## There is no historical scorecard

Tracked files were searched for timestamped forecasts, executable quotes, and resolved outcomes. The only data files in git are frontend package and TypeScript config. Decision logs, fills, and the SQLite database are gitignored, were wiped, and were not opened. Example strings inside unit tests are not trading history.

So these quantities are **unknown**, not zero and not estimated:

- live prediction accuracy
- live win rate
- live profit after fees
- whether past losses came from fees, bad probabilities, sizing, or bad fills

## What the synthetic study is

The script `offline_edge_eval.py` imports the **corrected** edge, Kelly, and autonomous-gate functions and refuses to run if the small-account stake cap still expands to full cash.

Each scenario uses:

- seed `20261108`
- 200 independent replications
- 10 waves of 12 contracts (24,000 contracts per scenario)
- true probabilities drawn from Uniform(0.15, 0.85)
- default gates: 5 point minimum edge, 60% minimum win probability on the bought side, 22 point edge cap, 90% probability cap, 26 cent minimum price, and the mid-price high-confidence block
- hold to settlement (stop-loss selling is off in the product, so it is not simulated)
- the 30-position cap, which **does not bind** here because a wave only has 12 contracts
- buy at the scanned ask, all-or-nothing, if a later ask is not worse

Forecasts are stored before outcomes are drawn. The outcome is not an input to the forecast function.

**Illustrative costs, not Kalshi’s schedule.** The live fee schedule was not verified. The study uses a flat charge of **$0, $0.01, or $0.02 per contract**, and a half-spread of **2 cents** (or zero in one row). A “2 cent worse ask” is an assumed quote move. None of these numbers are a quote from Kalshi.

**Return** is `(cash after the last settlement − starting cash) / starting cash`, one number per replication. **Max drawdown** is the worst peak-to-trough drop on wealth after each wave. **return_mean_over_std** is the mean terminal return across the 200 replications divided by the population standard deviation of those returns. It is not annualized. **Brier** and **log loss** use every contract, traded or not. Log loss clips probabilities to the range 0.001–0.999. **Accuracy** is the share of forecasts strictly on the correct side of 50%. An exact 50% call is not counted as correct. **Side win rate** is the share of fills whose bought side won. It is left blank when there are no fills.

In this particular book, the ask plus the illustrative fee stayed below $1, so a winning contract was always cash-positive. Side win rate and “profitable fill rate” match for that reason. A real fee schedule could separate them. A high win rate is still not a positive expected value, as the rows below show.

## Results that do not require a gifted forecast

| Scenario | Fills | Side win rate | Mean terminal return | 5th–95th percentile | Mean max drawdown | Brier, forecast vs market |
| --- | ---: | ---: | ---: | --- | ---: | --- |
| No-trade cash | 0 | blank | 0 | 0 to 0 | 0 | no forecast |
| Market probability, efficient book, $500 | 0 | blank | 0 | 0 to 0 | 0 | 0.209 vs 0.209 |
| Always 50%, $500 | 0 | blank | 0 | 0 to 0 | 0 | 0.250 vs 0.209 |
| Noise forecast, $500, fee $0 | 3,279 | 60.4% | **−1.3%** | −22.9% to +26.2% | 14.2% | 0.320 vs 0.209 |
| Noise forecast, $500, fee $0.02 | 3,279 | 60.4% | **−3.9%** | −24.9% to +24.1% | 15.3% | same worse Brier |
| Noise forecast, $10, fee $0 | 615 | 45.5% | **−0.25%** | −12.6% to +14.8% | 6.0% | same worse Brier |
| Overconfident forecast, $500, fee $0 | 9,478 | **66.7%** | **−4.6%** | −33.3% to +29.8% | 20.1% | 0.225 vs 0.209 |
| Overconfident forecast, $500, fee $0.02 | 9,478 | 66.7% | **−10.3%** | −37.4% to +22.0% | 22.4% | same |
| Overconfident forecast, $10 | 0 | blank | 0 | 0 to 0 | 0 | same worse Brier |
| Forecast equals truth, market also equals truth, 2 cent half-spread | 0 | blank | 0 | 0 to 0 | 0 | tied with the market |
| Same, spread set to $0 | 0 | blank | 0 | 0 to 0 | 0 | tied with the market |

Reading that table:

- Using the market’s own probability as the “model” never trades. The edge is measured against the **ask**, so a forecast that matches the mid is about 2 points **worse** than the ask when the half-spread is 2 cents, and about 0 when the spread is 0. The 5 point rule skips both. Of 24,000 efficient-market offers, 17,403 failed the edge rule and 6,261 failed the 60% probability rule.
- A forecast that truly equals the probability, while the market mid equals that same probability, also never trades. Matching a well-priced market is not a 5 point edge over the ask.
- A noise forecast is a worse probability than the market (Brier 0.320 vs 0.209) and loses money on average. Its 60% win rate at $500 coexists with about **−$0.40 per fill** before the illustrative fee and **−$1.18 per fill** with a 2 cent fee.
- An overconfident forecast, which stretches the market mid toward 0 or 1 while the mid is already the truth, wins **66.7%** of fills and still loses about **−$0.48 per fill** with no fee. The average account is down 4.6% after 120 offers, and down 10.3% if each contract also costs an illustrative 2 cents. The 90% probability cap blocked 8,085 of those offers; the ones that got through were still a losing book.

## The only profitable rows are labeled assumptions

These rows give the forecast the **true** probability, and they build the market mid as `0.5 + 0.45 × (true probability − 0.5)`, clipped. The market is wrong by construction. A profit here does **not** mean xAI has that skill.

| Scenario | Fills | Side win rate | Mean terminal return | 5th–95th percentile | Mean max drawdown | Brier skill vs that market |
| --- | ---: | ---: | ---: | --- | ---: | --- |
| Assumed skill, $500, fee $0 | 11,548 | 72.8% | +51.3% | +5.8% to +113% | 9.8% | +5.6% relative |
| Same, illustrative fee $0.01 | 11,548 | 72.8% | +45.4% | +1.8% to +104% | 10.5% | same |
| Same, illustrative fee $0.02 | 11,548 | 72.8% | +39.7% | **−2.2%** to +94% | 11.2% | same |
| Same, $10 account | 0 | blank | 0 | 0 to 0 | 0 | same better Brier, no trades |
| Market-mid forecast in that same shrunk world, $500 | 0 | blank | 0 | 0 to 0 | 0 | tied with its own mid |
| Assumed skill, half of IOC orders miss a 2 cent adverse move | 5,757 fills, 5,791 misses | 72.3% | +24.0% | −9.4% to +59% | 9.6% | same |
| Assumed skill, every IOC misses that 2 cent move | 0 | blank | 0 | 0 to 0 | 0 | same; 11,548 missed orders |
| Assumed skill, 12 copies of one shared outcome per wave | 11,964 | 72.6% | +43.8% | **−76.8%** to +277% | **43.8%** | not an independent sample |
| Overconfident, 12 copies of one shared outcome | 9,540 | 66.9% | −4.3% | −79.0% to +123% | 44.5% | worse than the market |

The assumed-skill book can make money inside this generator, including after an illustrative 1 cent fee. At 2 cents, the average replication is still up, but the unlucky 5th percentile is down. That is a statement about this generator, not about Grok.

Two gate facts show up clearly:

- On a **$10** account, 5% of cash is $0.50. Any ask above 50 cents is skipped instead of spending the rest of the cash. In this price mix, the offers that cleared the 5 point rule were mostly above 50 cents, so the assumed-skill world and the overconfident world **placed no trades** at $10. The noise world still placed 615 cheap trades and lost a little. The cap blocks both bad and hypothetically good trades. That is a tradeoff, not a change this study makes.
- Every fill that did happen was tagged **cash capped** (the 5% rule). Uncapped full Kelly and the one-contract bump both occurred **0** times. With a 5 point minimum edge, the Kelly stake is already more than one contract once the account has more than a few dollars. The one-contract bump only matters for a thinner edge than the default gate allows, or for an account of roughly $5 or less. This study did not hit that path.

Correlation is the other practical result. The same assumed skill, pointed at 12 copies of one coin flip, still has a positive average, but a typical replication’s worst drop is about **44%** instead of about **10%**, and about 1 in 20 replications loses more than three quarters of the account. The live bot does not shrink size when several contracts share an outcome. This study does not change that.

## What would count as evidence later

Do not retune the 5 point rule, the 90% cap, or the 5% stake cap from this simulation. Those knobs were not the question, and changing them to harvest the assumed-skill profit would be fitting a story we wrote ourselves.

A real test has to be unfunded:

1. Keep the bot from sending orders. Log the frozen forecast, the yes/no bid and ask, the quote time, and the model id.
2. After Kalshi resolves the market, score every logged forecast. Do not drop the ones the bot would have skipped. Compare Brier and log loss with the market mid, on a later slice that was not used to edit thresholds.
3. Separately, apply today’s gates on paper inside that log. Report fills, win rate, and settlement profit after the **fee written on a real fill**, or leave the fee blank. A win rate with no fee column is not a result.
4. Pre-register the window before looking: at least 150 resolved forecasts and 28 days, then score only the second half. Any threshold edit starts the clock over.
5. If several logged markets share a game or a coin window, report the combined drawdown, not only the average trade.

Until that log exists, the honest conclusion is: **there is no evidence here of a trading edge, and the synthetic wins are not that evidence.**
