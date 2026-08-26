#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

OUTPUT_ROOT = Path("output")
SOURCE_URL = "https://raw.githubusercontent.com/thisjustinh/ark-invest-history/master/transactions/master.csv"
SOURCE_REPO = "thisjustinh/ark-invest-history"
FUNDS = {"ARKK", "ARKQ", "ARKW", "ARKG", "ARKF", "ARKX"}
SIGNAL_THRESHOLD_REL = 0.005
MIN_FLOW_COMMON = 8


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    mask = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not mask.any():
        return float("nan")
    v = values[mask]
    w = weights[mask]
    order = np.argsort(v)
    v = v[order]
    w = w[order]
    cutoff = w.sum() / 2.0
    return float(v[np.searchsorted(np.cumsum(w), cutoff, side="left")])


def estimate_flow_factor(group: pd.DataFrame) -> tuple[float, str, int]:
    candidates = group[(group["previous_shares"] > 0) & (group["shares"] > 0)].copy()
    candidates["ratio"] = candidates["shares"] / candidates["previous_shares"] - 1.0
    candidates = candidates[np.isfinite(candidates["ratio"]) & (candidates["ratio"].abs() <= 0.35)]
    if len(candidates) < MIN_FLOW_COMMON:
        return float("nan"), "unavailable", int(len(candidates))
    weights = pd.to_numeric(candidates["weight"], errors="coerce").fillna(0.01).to_numpy(dtype=float)
    weights = np.where(weights > 0, weights, 0.01)
    ratios = candidates["ratio"].to_numpy(dtype=float)
    initial = weighted_median(ratios, weights)
    mad = weighted_median(np.abs(ratios - initial), weights)
    tolerance = max(0.0015, min(0.03, 4.0 * mad if np.isfinite(mad) else 0.01))
    inlier = np.abs(ratios - initial) <= tolerance
    if int(inlier.sum()) < 5:
        return float("nan"), "low_cluster", int(inlier.sum())
    factor = weighted_median(ratios[inlier], weights[inlier])
    cluster_share = float(weights[inlier].sum() / weights.sum()) if weights.sum() else 0.0
    confidence = "high" if cluster_share >= 0.60 else "medium" if cluster_share >= 0.35 else "low_cluster"
    if confidence == "low_cluster":
        return float("nan"), confidence, int(inlier.sum())
    return factor, confidence, int(inlier.sum())


def normalize_archive(df: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "fund", "company", "ticker", "shares", "weight", "deltaShares", "action"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Historical archive is missing required columns: {sorted(missing)}")

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out["fund"] = out["fund"].astype(str).str.upper().str.strip()
    out["ticker"] = out["ticker"].astype(str).str.upper().str.strip()
    out = out[out["fund"].isin(FUNDS) & out["date"].notna() & ~out["ticker"].isin({"", "NAN", "NONE", "CASH"})].copy()
    out["shares"] = pd.to_numeric(out["shares"], errors="coerce").fillna(0.0)
    out["deltaShares"] = pd.to_numeric(out["deltaShares"], errors="coerce").fillna(0.0)
    out["weight"] = pd.to_numeric(out["weight"], errors="coerce").fillna(0.0)
    out["previous_shares"] = out["shares"] - out["deltaShares"]
    out["company"] = out["company"].fillna("").astype(str).str.strip()
    return out


def add_flow_adjustment(df: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for (_d, _fund), g in df.groupby(["date", "fund"], sort=True):
        g = g.copy()
        factor, confidence, cluster_count = estimate_flow_factor(g)
        g["flow_factor"] = factor
        g["flow_confidence"] = confidence
        g["flow_cluster_count"] = cluster_count
        expected = np.where((g["previous_shares"] > 0) & np.isfinite(factor), g["previous_shares"] * factor, 0.0)
        g["expected_flow_shares"] = expected
        g["flow_adjusted_manager_delta"] = np.where(
            (g["previous_shares"] > 0) & (g["shares"] > 0) & np.isfinite(factor),
            g["deltaShares"] - expected,
            g["deltaShares"],
        )
        g["relative_manager_delta"] = np.where(
            g["previous_shares"] > 0,
            g["flow_adjusted_manager_delta"] / g["previous_shares"],
            np.nan,
        )
        frames.append(g)
    return pd.concat(frames, ignore_index=True) if frames else df.copy()


def classify_direction(row: pd.Series) -> str:
    if row["previous_shares"] <= 0 < row["shares"]:
        return "Buy"
    if row["previous_shares"] > 0 >= row["shares"]:
        return "Sell"
    rel = row["relative_manager_delta"]
    if pd.notna(rel) and rel >= SIGNAL_THRESHOLD_REL:
        return "Buy"
    if pd.notna(rel) and rel <= -SIGNAL_THRESHOLD_REL:
        return "Sell"
    return "Neutral"


def aggregate_ark_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["direction"] = df.apply(classify_direction, axis=1)
    calendar = sorted(pd.unique(df["date"]))
    calendar_pos = {d: i for i, d in enumerate(calendar)}
    rows: list[dict] = []

    for (d, ticker), g in df.groupby(["date", "ticker"], sort=True):
        buys = sorted(g.loc[g["direction"] == "Buy", "fund"].unique())
        sells = sorted(g.loc[g["direction"] == "Sell", "fund"].unique())
        if buys and sells:
            direction = "Mixed"
            label = "ARK Internal Divergence"
        elif len(buys) >= 3:
            direction = "Buy"
            label = "ARK Cross-Fund Buy 3+"
        elif len(buys) == 2:
            direction = "Buy"
            label = "ARK Cross-Fund Buy 2"
        elif len(buys) == 1:
            direction = "Buy"
            label = "ARK Single-Fund Buy"
        elif len(sells) >= 3:
            direction = "Sell"
            label = "ARK Cross-Fund Sell 3+"
        elif len(sells) == 2:
            direction = "Sell"
            label = "ARK Cross-Fund Sell 2"
        elif len(sells) == 1:
            direction = "Sell"
            label = "ARK Single-Fund Sell"
        else:
            continue

        max_rel = pd.to_numeric(g["relative_manager_delta"], errors="coerce").abs().max(skipna=True)
        rows.append({
            "signal_date": pd.Timestamp(d).date().isoformat(),
            "calendar_position": calendar_pos[d],
            "ticker": ticker,
            "company": next((x for x in g["company"] if isinstance(x, str) and x), ""),
            "direction": direction,
            "signal_label": label,
            "buy_fund_count": len(buys),
            "sell_fund_count": len(sells),
            "buy_funds": "|".join(buys),
            "sell_funds": "|".join(sells),
            "max_abs_relative_manager_delta": 0.0 if pd.isna(max_rel) else float(max_rel),
            "min_flow_confidence": "low" if (g["flow_confidence"].isin(["unavailable", "low_cluster"]).any()) else "medium_or_high",
            "source_kind": "SECONDARY_ARCHIVE_OF_ARK_OFFICIAL_HOLDINGS",
            "source_repository": SOURCE_REPO,
            "availability_rule": "CONSERVATIVE_NEXT_SESSION_ENTRY",
        })

    signals = pd.DataFrame(rows)
    if signals.empty:
        return signals

    signals = signals.sort_values(["ticker", "calendar_position"]).reset_index(drop=True)
    for col in ("buy_days_5d", "buy_days_20d", "sell_days_5d", "sell_days_20d", "consecutive_buy_days", "consecutive_sell_days"):
        signals[col] = 0

    for _ticker, idxs in signals.groupby("ticker").groups.items():
        history: list[tuple[int, str]] = []
        previous_pos: int | None = None
        previous_direction: str | None = None
        buy_streak = 0
        sell_streak = 0
        for idx in list(idxs):
            pos = int(signals.at[idx, "calendar_position"])
            direction = str(signals.at[idx, "direction"])
            history.append((pos, direction))
            signals.at[idx, "buy_days_5d"] = sum(d == "Buy" and p >= pos - 4 for p, d in history)
            signals.at[idx, "buy_days_20d"] = sum(d == "Buy" and p >= pos - 19 for p, d in history)
            signals.at[idx, "sell_days_5d"] = sum(d == "Sell" and p >= pos - 4 for p, d in history)
            signals.at[idx, "sell_days_20d"] = sum(d == "Sell" and p >= pos - 19 for p, d in history)

            adjacent = previous_pos is not None and pos == previous_pos + 1
            if direction == "Buy":
                buy_streak = buy_streak + 1 if adjacent and previous_direction == "Buy" else 1
                sell_streak = 0
            elif direction == "Sell":
                sell_streak = sell_streak + 1 if adjacent and previous_direction == "Sell" else 1
                buy_streak = 0
            else:
                buy_streak = 0
                sell_streak = 0
            signals.at[idx, "consecutive_buy_days"] = buy_streak
            signals.at[idx, "consecutive_sell_days"] = sell_streak
            previous_pos = pos
            previous_direction = direction

    return signals.drop(columns=["calendar_position"]).sort_values(["signal_date", "ticker"]).reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill historical ARK signals from a public archive of ARK official holdings.")
    parser.add_argument("--start", default="2020-10-20")
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    response = requests.get(SOURCE_URL, timeout=90, headers={"User-Agent": "ARK-Signal-Research/1.0"})
    response.raise_for_status()
    raw = pd.read_csv(io.BytesIO(response.content))
    df = normalize_archive(raw)
    start = pd.Timestamp(args.start).date()
    df = df[df["date"] >= start]
    if args.end:
        end = pd.Timestamp(args.end).date()
        df = df[df["date"] <= end]

    adjusted = add_flow_adjustment(df)
    signals = aggregate_ark_signals(adjusted)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    signals.to_csv(OUTPUT_ROOT / "backtest_ark_historical_signals.csv", index=False)
    status = {
        "source": SOURCE_URL,
        "source_repository": SOURCE_REPO,
        "source_tier": "secondary archive derived from ARK published holdings; not treated as current Tier-1 official confirmation",
        "archive_rows": int(len(df)),
        "signal_rows": int(len(signals)),
        "first_signal_date": None if signals.empty else str(signals["signal_date"].min()),
        "last_signal_date": None if signals.empty else str(signals["signal_date"].max()),
        "flow_normalization": True,
        "persistence_windows": "observed ARK archive trading sessions, not last-N signal occurrences",
        "entry_timing_rule": "signal date is never traded at same-day close; earliest modeled entry is next market session open",
        "warning": "Historical archive methodology and ARK reporting conventions changed over time. Results must be labeled historical-research evidence, not identical to the current Gmail trade-notification feed.",
    }
    (OUTPUT_ROOT / "backtest_ark_historical_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
