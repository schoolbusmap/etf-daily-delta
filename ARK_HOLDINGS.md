# ARK Official Holdings Pipeline

This module archives the official **Full Holdings CSV** for ARK's six actively managed equity ETFs:

- ARKK
- ARKQ
- ARKW
- ARKG
- ARKF
- ARKX

## Source discipline

The fetcher discovers the current CSV link from each official `ark-funds.com/funds/<ticker>` page and falls back only to the corresponding `assets.ark-funds.com/fund-documents/funds-etf-csv/` URL.

The raw official bytes are preserved at:

```text
data/ark/<reported_date>/<fund>/raw.csv
```

A normalized copy and metadata are stored beside it. `reported_date` is the date printed by ARK in the holdings file; ARK states that the displayed holdings date is the **next trading day**, so reconciliation with a Daily Trade Notification must align the holdings snapshot to the prior trading session rather than treating the label as the execution date.

## Derived output

After two distinct official snapshots exist, the job writes:

- `output/ark_holdings_delta.csv`
- `output/ark_holdings_delta.json`
- `output/ark_holdings_status.json`

The delta contains raw shares/weight changes plus an estimated fund-level creation/redemption scaling factor. `flow_adjusted_manager_delta` removes that proportional factor before estimating whether the remaining change resembles manager buying or selling.

This estimate is **not** an execution blotter. Final reconciliation should still compare it with ARK Trading Desk's Daily Trade Notification, which explicitly excludes ETF creation/redemption unit activity, and account for corporate actions.

## GitHub Actions

`.github/workflows/ark_holdings.yml` runs on weekdays after the ARK trade-email window. It retries the official CSVs for up to roughly 40 minutes, archives new snapshots, computes deltas, and commits only changed `data/ark` / `output/ark_holdings_*` files.

Manual test:

```bash
python ark_holdings.py --funds ARKK ARKW --strict
```

Scheduled production run:

```bash
python ark_holdings.py --wait-for-new --retries 5 --retry-seconds 600 --strict
```
