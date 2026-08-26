# ARK / Institutional Signal Backtest v1

## Goal

Validate whether observable ARK / active-manager signals have forward predictive value before any 0-100 score is enabled.

The v1 backtest is an **event study**, not a portfolio strategy simulation.

## Historical ARK source

For long-history bootstrapping, `ark_history_backfill.py` reads the public `thisjustinh/ark-invest-history` transaction archive. Its `transactions/master.csv` is documented as daily differences derived from ARK-published holdings beginning in 2020.

This is treated as a **secondary archive of ARK primary data**, not as equivalent to the current Tier-1 Gmail Daily Trade Notification or the current official ARK holdings pipeline.

Current live ARK research continues to prefer:

1. ARK Trading Desk Daily Trade Notification
2. ARK official end-of-day Full Holdings CSV
3. ARK official fund pages/disclosures

## Data discipline

### No same-day-close entry

A signal observed for date T is never allowed to buy at the T close. Earliest modeled retail entry is the next available market session open.

This rule is deliberately conservative and reduces look-ahead bias across historical reporting-regime changes.

### Flow normalization

Historical holdings deltas are adjusted for probable ETF creation/redemption scaling using a robust proportional-share cluster estimate at fund/date level. Low-confidence cases remain labeled and must not be interpreted as precise manager trades.

### Persistence

5D and 20D persistence are counted over observed archive trading sessions, not over the last 5 or 20 signal occurrences. Consecutive streaks require adjacent observed trading sessions.

### Independent manager scoring

No Institutional Consensus score is used in v1. Cross-manager historical validation remains separate until sufficient point-in-time histories exist for JEPI, DFAC, CGGR, AVUV, and BLOK.

## Return definitions

For each signal:

- Entry = T+1 adjusted open
- Horizons = 1 / 5 / 20 / 60 trading sessions
- Stock return = adjusted exit close / adjusted entry open - 1
- Excess return = stock return - SPY return over the same entry/exit dates
- Signal-signed excess = excess return for Buy signals; negative excess return for Sell signals

Primary validation metric: **signal-signed excess return versus SPY**.

The engine also records MFE and MAE over each horizon.

## Signal cohorts

Historical ARK cohorts currently include:

- ARK Single-Fund Buy / Sell
- ARK Cross-Fund Buy / Sell 2
- ARK Cross-Fund Buy / Sell 3+
- ARK Internal Divergence
- Persistence buckets: ISOLATED, REPEATED, 5D_3_4, 5D_5, 20D_5PLUS

## Outputs

- `output/backtest_ark_historical_signals.csv`
- `output/backtest_ark_historical_status.json`
- `output/signal_performance.csv`
- `output/backtest_summary_by_signal.csv`
- `output/backtest_summary_by_persistence.csv`
- `output/backtest_status.json`

## Important limitations

- Historical ARK archive behavior is not identical to the current Daily Trade Notification feed.
- Corporate actions and historical ticker mappings are not yet fully normalized.
- Missing/delisted Yahoo symbols can introduce coverage bias; failures are explicitly reported.
- Sector-adjusted returns are not yet included.
- No commissions, slippage, position sizing, portfolio overlap, stop loss, or take-profit rules are modeled.
- This phase validates signal information content; it does not claim a tradeable strategy.

## Development order

1. Data correctness / historical backfill
2. Point-in-time signal reconstruction
3. Event-study validation
4. Out-of-sample validation
5. Only then: score calibration and retail strategy backtest
