# ETF Daily Trade Delta

A free GitHub Actions + Python/Pandas pipeline that downloads official daily holdings for:

- JEPI — J.P. Morgan Asset Management
- DFAC — Dimensional Fund Advisors
- CGGR — Capital Group
- AVUV — Avantis Investors
- BLOK — Amplify ETFs

It stores dated raw/normalized snapshots and calculates the latest holdings delta for each ETF.

## Output

`output/daily_trades.csv` and `output/daily_trades.json` use:

- `Date`
- `ETF_Ticker`
- `Stock_Ticker`
- `Company_Name`
- `Action` (`New Position`, `Closed Position`, `Buy`, `Sell`)
- `Shares_Change`
- `Weight_Change` (percentage-point change)

`output/status.json` records per-provider fetch state, holdings as-of date, source URL and row count.

## Repository layout

```text
.
├── scraper.py
├── requirements.txt
├── data/
│   └── YYYY-MM-DD/
│       └── JEPI/
│           ├── raw.xlsx
│           ├── normalized.csv
│           └── metadata.json
├── output/
│   ├── daily_trades.csv
│   ├── daily_trades.json
│   └── status.json
└── .github/
    └── workflows/
        └── daily_fetch.yml
```

## Local setup

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python scraper.py --strict
```

The first successful run creates the baseline snapshot. A delta appears only after at least two distinct holdings dates exist for an ETF.

## GitHub setup

1. Create a GitHub repository and place these files at the repository root.
2. Commit and push them to the default branch.
3. In **Settings → Actions → General → Workflow permissions**, make sure the workflow can write repository contents. The YAML also declares `contents: write`.
4. Open **Actions → Daily ETF Holdings Delta → Run workflow** once to create the baseline.
5. On subsequent weekdays, the scheduled job runs at 08:07 `America/New_York` and commits changed `data/` and `output/` files.

No paid market-data API key is required. To make GitHub Actions itself strictly zero-cost, use a **public repository** with a standard GitHub-hosted runner. Private repositories consume the account's included Actions minutes and can incur charges after the included quota is exhausted.

GitHub can disable scheduled workflows in a public repository after 60 days without repository activity; if that happens, re-enable the workflow in the Actions tab.

## Why compare the latest two snapshots instead of literal “today vs yesterday” folders?

ETF sites can publish late, weekends/holidays do not have a new holdings date, and some providers label holdings with the prior business day. The scraper extracts the actual `As of` date from the official file/page and compares the latest two distinct dated snapshots. It never substitutes the fetch date when an as-of date cannot be identified, because that could create a false delta.

## Parser strategy

The retrieval order is:

1. Verified official direct download endpoint (when available).
2. Official landing page HTML discovery for current CSV/XLS/XLSX links.
3. Headless Chromium (Playwright) fallback for location gates / JavaScript-rendered downloads.

The parser then:

- scans up to 60 leading rows to identify the real table header;
- supports CSV/TSV/semicolon/pipe-delimited files, XLS/XLSX, HTML tables and JSON API arrays;
- maps provider-specific names into `Ticker`, company, shares and weight concepts;
- aggregates duplicate ticker rows;
- rejects stale/ambiguous documents whose actual holdings `As of` date cannot be determined.

## Important interpretation caveat

This is a **daily holdings delta**, not an audited manager execution blotter. A change in shares can also arise from ETF creations/redemptions, stock splits, mergers or other corporate actions. Therefore `Buy`/`Sell` means “share count increased/decreased between published holdings snapshots.”

## Maintenance

Official websites can change selectors, endpoints or column names. If a provider changes its site:

- inspect `output/status.json` and the GitHub Actions log;
- update that ETF's `landing_url`, `direct_urls`, `browser_download_texts`, or `aliases` in `SOURCES`;
- run `python scraper.py --etfs AVUV --strict` (replace ticker as needed) to test only one source.
