#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

OUTPUT_ROOT = Path("output")
ARK_SIGNAL_PATH = OUTPUT_ROOT / "backtest_ark_historical_signals.csv"
BENCHMARK = "SPY"
HORIZONS = (1, 5, 20, 60)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TICKER_RE = re.compile(r"^[A-Z0-9.\-]+$")


def completed_market_cutoff(now: datetime | None = None) -> date:
    now = now or datetime.now(ZoneInfo("America/New_York"))
    d = now.date()
    if now.weekday() < 5 and (now.hour, now.minute) >= (16, 15):
        return d
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-")


def fetch_yahoo_history(session: requests.Session, ticker: str, start: date, end: date, retries: int = 3) -> pd.DataFrame:
    symbol = yahoo_symbol(ticker)
    period1 = int(datetime.combine(start, datetime.min.time(), tzinfo=ZoneInfo("UTC")).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo("UTC")).timestamp())
    url = YAHOO_CHART.format(symbol=quote(symbol, safe="-^="))
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code in (429, 502, 503):
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            payload = r.json()
            result = payload.get("chart", {}).get("result")
            if not result:
                raise ValueError(payload.get("chart", {}).get("error") or "empty Yahoo chart result")
            obj = result[0]
            timestamps = obj.get("timestamp") or []
            quote_data = ((obj.get("indicators") or {}).get("quote") or [{}])[0]
            adj_data = ((obj.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
            rows = []
            for i, ts in enumerate(timestamps):
                close = (quote_data.get("close") or [None] * len(timestamps))[i]
                open_ = (quote_data.get("open") or [None] * len(timestamps))[i]
                high = (quote_data.get("high") or [None] * len(timestamps))[i]
                low = (quote_data.get("low") or [None] * len(timestamps))[i]
                adjclose = adj_data[i] if i < len(adj_data) else close
                if close is None or open_ is None or adjclose is None or close == 0:
                    continue
                factor = float(adjclose) / float(close)
                rows.append({
                    "date": datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York")).date(),
                    "open": float(open_),
                    "high": float(high) if high is not None else float(open_),
                    "low": float(low) if low is not None else float(open_),
                    "close": float(close),
                    "adj_close": float(adjclose),
                    "adj_open": float(open_) * factor,
                    "adj_high": (float(high) if high is not None else float(open_)) * factor,
                    "adj_low": (float(low) if low is not None else float(open_)) * factor,
                })
            return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        except Exception as exc:
            last_error = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"{ticker}: price fetch failed: {last_error}")


def load_signals(start: date | None, end: date | None) -> pd.DataFrame:
    if not ARK_SIGNAL_PATH.exists():
        raise FileNotFoundError(f"Missing {ARK_SIGNAL_PATH}; run ark_history_backfill.py first")
    df = pd.read_csv(ARK_SIGNAL_PATH)
    df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce").dt.date
    df = df[df["signal_date"].notna()].copy()
    if start:
        df = df[df["signal_date"] >= start]
    if end:
        df = df[df["signal_date"] <= end]
    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df = df[df["ticker"].map(lambda x: bool(TICKER_RE.fullmatch(x)))]
    return df.sort_values(["signal_date", "ticker"]).reset_index(drop=True)


def persistence_bucket(row: pd.Series) -> str:
    if row["direction"] == "Buy":
        d5, d20 = int(row.get("buy_days_5d", 0)), int(row.get("buy_days_20d", 0))
    elif row["direction"] == "Sell":
        d5, d20 = int(row.get("sell_days_5d", 0)), int(row.get("sell_days_20d", 0))
    else:
        return "MIXED"
    if d5 >= 5:
        return "5D_5"
    if d5 >= 3:
        return "5D_3_4"
    if d20 >= 5:
        return "20D_5PLUS"
    if d20 <= 1:
        return "ISOLATED"
    return "REPEATED"


def evaluate_signal(row: pd.Series, prices: pd.DataFrame, spy: pd.DataFrame, cutoff: date) -> list[dict]:
    if prices.empty or spy.empty:
        return []
    signal_date = row["signal_date"]
    eligible = prices[(prices["date"] > signal_date) & (prices["date"] <= cutoff)].reset_index(drop=True)
    if eligible.empty:
        return []
    entry = eligible.iloc[0]
    entry_date = entry["date"]
    spy_entry_rows = spy[spy["date"] == entry_date]
    if spy_entry_rows.empty:
        return []
    spy_entry = spy_entry_rows.iloc[0]
    entry_price = float(entry["adj_open"])
    spy_entry_price = float(spy_entry["adj_open"])
    if entry_price <= 0 or spy_entry_price <= 0:
        return []

    results: list[dict] = []
    for horizon in HORIZONS:
        exit_index = horizon - 1
        if exit_index >= len(eligible):
            continue
        exit_row = eligible.iloc[exit_index]
        exit_date = exit_row["date"]
        if exit_date > cutoff:
            continue
        spy_exit_rows = spy[spy["date"] == exit_date]
        if spy_exit_rows.empty:
            continue
        spy_exit = spy_exit_rows.iloc[0]
        stock_return = float(exit_row["adj_close"] / entry_price - 1.0)
        spy_return = float(spy_exit["adj_close"] / spy_entry_price - 1.0)
        excess = stock_return - spy_return
        direction = row["direction"]
        sign = 1.0 if direction == "Buy" else -1.0 if direction == "Sell" else math.nan
        signed_return = stock_return * sign if np.isfinite(sign) else math.nan
        signed_excess = excess * sign if np.isfinite(sign) else math.nan

        path = eligible.iloc[:horizon]
        raw_up = float(path["adj_high"].max() / entry_price - 1.0)
        raw_down = float(path["adj_low"].min() / entry_price - 1.0)
        if direction == "Buy":
            mfe, mae = raw_up, raw_down
        elif direction == "Sell":
            mfe, mae = -raw_down, -raw_up
        else:
            mfe = mae = math.nan

        results.append({
            "signal_date": signal_date.isoformat(),
            "entry_date": entry_date.isoformat(),
            "exit_date": exit_date.isoformat(),
            "ticker": row["ticker"],
            "company": row.get("company", ""),
            "direction": direction,
            "signal_label": row["signal_label"],
            "persistence_bucket": persistence_bucket(row),
            "buy_fund_count": int(row.get("buy_fund_count", 0)),
            "sell_fund_count": int(row.get("sell_fund_count", 0)),
            "buy_days_5d": int(row.get("buy_days_5d", 0)),
            "buy_days_20d": int(row.get("buy_days_20d", 0)),
            "sell_days_5d": int(row.get("sell_days_5d", 0)),
            "sell_days_20d": int(row.get("sell_days_20d", 0)),
            "flow_quality": row.get("min_flow_confidence", ""),
            "horizon": horizon,
            "entry_adj_open": entry_price,
            "exit_adj_close": float(exit_row["adj_close"]),
            "stock_return": stock_return,
            "spy_return": spy_return,
            "excess_return_vs_spy": excess,
            "signal_signed_return": signed_return,
            "signal_signed_excess_vs_spy": signed_excess,
            "outperformed_in_signal_direction": bool(signed_excess > 0) if np.isfinite(signed_excess) else None,
            "mfe": mfe,
            "mae": mae,
            "source_kind": row.get("source_kind", ""),
        })
    return results


def summarize(perf: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if perf.empty:
        return pd.DataFrame()
    rows: list[dict] = []
    for key, g in perf.groupby(group_cols + ["horizon"], dropna=False, sort=True):
        if not isinstance(key, tuple):
            key = (key,)
        record = {col: value for col, value in zip(group_cols + ["horizon"], key)}
        signed = pd.to_numeric(g["signal_signed_excess_vs_spy"], errors="coerce").dropna()
        record.update({
            "sample_size": int(len(g)),
            "median_stock_return": float(g["stock_return"].median()),
            "median_excess_vs_spy": float(g["excess_return_vs_spy"].median()),
            "median_signal_signed_excess_vs_spy": float(signed.median()) if len(signed) else math.nan,
            "mean_signal_signed_excess_vs_spy": float(signed.mean()) if len(signed) else math.nan,
            "signal_direction_outperformance_rate": float((signed > 0).mean()) if len(signed) else math.nan,
            "median_mfe": float(pd.to_numeric(g["mfe"], errors="coerce").median()),
            "median_mae": float(pd.to_numeric(g["mae"], errors="coerce").median()),
        })
        rows.append(record)
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Point-in-time event study for ARK / institutional signals.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-signals", type=int, default=0, help="0 means no limit; useful for smoke tests")
    args = parser.parse_args()

    start = pd.Timestamp(args.start).date() if args.start else None
    end = pd.Timestamp(args.end).date() if args.end else None
    signals = load_signals(start, end)
    if args.max_signals > 0:
        signals = signals.tail(args.max_signals).copy()
    if signals.empty:
        raise SystemExit("No signals available for requested period")

    cutoff = completed_market_cutoff()
    earliest = min(signals["signal_date"]) - timedelta(days=7)
    latest_needed = cutoff
    tickers = sorted(set(signals["ticker"]))
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 ARK-Signal-Backtest/1.0"})

    price_map: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    for i, ticker in enumerate(tickers, start=1):
        try:
            price_map[ticker] = fetch_yahoo_history(session, ticker, earliest, latest_needed)
        except Exception as exc:
            failures[ticker] = str(exc)
        if i % 25 == 0:
            print(f"[backtest] prices {i}/{len(tickers)} failures={len(failures)}", flush=True)

    spy = fetch_yahoo_history(session, BENCHMARK, earliest, latest_needed)
    results: list[dict] = []
    for _, row in signals.iterrows():
        prices = price_map.get(row["ticker"])
        if prices is None:
            continue
        results.extend(evaluate_signal(row, prices, spy, cutoff))

    perf = pd.DataFrame(results)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    perf.to_csv(OUTPUT_ROOT / "signal_performance.csv", index=False)
    by_signal = summarize(perf, ["signal_label"])
    by_persistence = summarize(perf, ["direction", "persistence_bucket"])
    by_signal.to_csv(OUTPUT_ROOT / "backtest_summary_by_signal.csv", index=False)
    by_persistence.to_csv(OUTPUT_ROOT / "backtest_summary_by_persistence.csv", index=False)

    eligible_signal_count = int(len(signals))
    tested_unique = int(perf[["signal_date", "ticker"]].drop_duplicates().shape[0]) if not perf.empty else 0
    status = {
        "mode": "EVENT_STUDY_V1",
        "predictive_scoring_used": False,
        "signal_rows_requested": eligible_signal_count,
        "signals_with_at_least_one_completed_horizon": tested_unique,
        "ticker_count": len(tickers),
        "price_fetch_failures": len(failures),
        "failed_tickers": failures,
        "benchmark": BENCHMARK,
        "horizons_trading_sessions": list(HORIZONS),
        "entry_rule": "next available market session adjusted open after signal date",
        "same_day_close_entry_allowed": False,
        "completed_market_cutoff": cutoff.isoformat(),
        "return_adjustment": "Yahoo adjusted-close factor applied to open/high/low to reduce split/dividend distortion",
        "primary_validation_metric": "signal-signed excess return versus SPY",
        "limitations": [
            "Historical ARK signal source is a secondary archive of ARK-published holdings, not the current Gmail Daily Trade Notification feed.",
            "Sector-adjusted returns are not yet implemented in v1.",
            "Corporate-action exclusions and point-in-time ticker mapping are not yet complete.",
            "Missing/delisted Yahoo symbols can create coverage bias and are reported explicitly.",
            "No transaction costs, slippage, position sizing, or portfolio overlap rules are included; this is an event study, not a trading-strategy backtest.",
        ],
    }
    (OUTPUT_ROOT / "backtest_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
