#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

STANDARD_DATA_ROOT = Path("data")
ARK_DATA_ROOT = Path("data/ark")
OUTPUT_ROOT = Path("output")

FUND_PROFILES = {
    "ARKK": {"manager": "ARK Invest", "style": "innovation_growth", "group": "ARK"},
    "ARKQ": {"manager": "ARK Invest", "style": "innovation_growth", "group": "ARK"},
    "ARKW": {"manager": "ARK Invest", "style": "innovation_growth", "group": "ARK"},
    "ARKG": {"manager": "ARK Invest", "style": "innovation_growth", "group": "ARK"},
    "ARKF": {"manager": "ARK Invest", "style": "innovation_growth", "group": "ARK"},
    "ARKX": {"manager": "ARK Invest", "style": "innovation_growth", "group": "ARK"},
    "JEPI": {"manager": "J.P. Morgan Asset Management", "style": "equity_income_low_vol", "group": "JPM"},
    "DFAC": {"manager": "Dimensional Fund Advisors", "style": "systematic_core", "group": "DIMENSIONAL"},
    "CGGR": {"manager": "Capital Group", "style": "active_growth", "group": "CAPITAL_GROUP"},
    "AVUV": {"manager": "Avantis Investors", "style": "small_value", "group": "AVANTIS"},
    "BLOK": {"manager": "Amplify ETFs", "style": "blockchain_thematic", "group": "AMPLIFY"},
}

MATERIALITY_ORDER = {"Small": 0, "Moderate": 1, "Meaningful": 2, "High": 3}
SIGNAL_THRESHOLD_REL = 0.005
MIN_FLOW_COMMON = 10
MIN_FLOW_CLUSTER_WEIGHT_SHARE = 0.35


@dataclass(frozen=True)
class Snapshot:
    fund: str
    reported_date: date
    trade_date: date
    path: Path
    is_ark: bool


def log(message: str) -> None:
    print(f"[consensus] {message}", flush=True)


def parse_date(value: str) -> date:
    return pd.Timestamp(value).date()


def previous_weekday(d: date) -> date:
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def find_standard_market_dates() -> list[date]:
    dates: set[date] = set()
    if not STANDARD_DATA_ROOT.exists():
        return []
    for day_dir in STANDARD_DATA_ROOT.iterdir():
        if not day_dir.is_dir() or day_dir.name == "ark":
            continue
        try:
            d = parse_date(day_dir.name)
        except Exception:
            continue
        if any((day_dir / fund / "normalized.csv").exists() for fund in ("JEPI", "DFAC", "CGGR", "AVUV", "BLOK")):
            dates.add(d)
    return sorted(dates)


def ark_trade_date(reported_date: date, standard_market_dates: list[date]) -> tuple[date, str]:
    prior = [d for d in standard_market_dates if d < reported_date]
    if prior:
        return max(prior), "inferred_from_standard_holdings_calendar"
    return previous_weekday(reported_date), "weekday_fallback"


def load_normalized(path: Path, fund: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.rename(columns={
        "Stock_Ticker": "ticker",
        "Company_Name": "company",
        "Shares": "shares",
        "Weight": "weight",
    })
    required = {"ticker", "company", "shares", "weight"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    out = df[["ticker", "company", "shares", "weight"]].copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out = out[~out["ticker"].isin({"", "NAN", "NONE"})]
    out["company"] = out["company"].fillna("").astype(str).str.strip()
    out["shares"] = pd.to_numeric(out["shares"], errors="coerce").fillna(0.0)
    out["weight"] = pd.to_numeric(out["weight"], errors="coerce").fillna(0.0)
    out = out.groupby("ticker", as_index=False).agg(company=("company", "first"), shares=("shares", "sum"), weight=("weight", "sum"))
    out["fund"] = fund
    return out


def discover_snapshots() -> tuple[dict[str, list[Snapshot]], dict[str, str]]:
    standard_dates = find_standard_market_dates()
    snapshots: dict[str, list[Snapshot]] = {fund: [] for fund in FUND_PROFILES}
    ark_date_methods: dict[str, str] = {}
    for d in standard_dates:
        day_dir = STANDARD_DATA_ROOT / d.isoformat()
        for fund in ("JEPI", "DFAC", "CGGR", "AVUV", "BLOK"):
            path = day_dir / fund / "normalized.csv"
            if path.exists():
                snapshots[fund].append(Snapshot(fund, d, d, path, False))
    if ARK_DATA_ROOT.exists():
        for day_dir in ARK_DATA_ROOT.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                reported = parse_date(day_dir.name)
            except Exception:
                continue
            trade_d, method = ark_trade_date(reported, standard_dates)
            ark_date_methods[reported.isoformat()] = method
            for fund in ("ARKK", "ARKQ", "ARKW", "ARKG", "ARKF", "ARKX"):
                path = day_dir / fund / "normalized.csv"
                if path.exists():
                    snapshots[fund].append(Snapshot(fund, reported, trade_d, path, True))
    for fund in snapshots:
        snapshots[fund].sort(key=lambda s: s.reported_date)
    return snapshots, ark_date_methods


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


def estimate_flow_factor(prev: pd.DataFrame, curr: pd.DataFrame) -> dict:
    merged = prev[["ticker", "shares", "weight"]].merge(curr[["ticker", "shares", "weight"]], on="ticker", suffixes=("_prev", "_curr"))
    candidates = merged[(merged["shares_prev"] > 0) & (merged["shares_curr"] > 0)].copy()
    candidates["ratio"] = candidates["shares_curr"] / candidates["shares_prev"] - 1.0
    candidates = candidates[np.isfinite(candidates["ratio"]) & (candidates["ratio"].abs() <= 0.35)]
    if len(candidates) < MIN_FLOW_COMMON:
        return {"flow_factor": float("nan"), "flow_confidence": "unavailable", "common_count": int(len(candidates)), "cluster_count": 0, "cluster_weight_share": 0.0}
    weights = candidates["weight_prev"].to_numpy(dtype=float)
    weights = np.where(weights > 0, weights, 0.01)
    ratios = candidates["ratio"].to_numpy(dtype=float)
    initial = weighted_median(ratios, weights)
    abs_dev = np.abs(ratios - initial)
    mad = weighted_median(abs_dev, weights)
    tolerance = max(0.0015, min(0.03, 4.0 * mad if np.isfinite(mad) else 0.01))
    inlier = abs_dev <= tolerance
    cluster_weight = float(weights[inlier].sum())
    total_weight = float(weights.sum())
    cluster_share = cluster_weight / total_weight if total_weight else 0.0
    cluster_count = int(inlier.sum())
    if cluster_count < max(5, MIN_FLOW_COMMON // 2) or cluster_share < MIN_FLOW_CLUSTER_WEIGHT_SHARE:
        return {"flow_factor": float("nan"), "flow_confidence": "low_cluster", "common_count": int(len(candidates)), "cluster_count": cluster_count, "cluster_weight_share": cluster_share}
    factor = weighted_median(ratios[inlier], weights[inlier])
    dispersion = weighted_median(np.abs(ratios[inlier] - factor), weights[inlier])
    confidence = "high" if cluster_share >= 0.60 and dispersion <= 0.003 else "medium"
    return {"flow_factor": factor, "flow_confidence": confidence, "common_count": int(len(candidates)), "cluster_count": cluster_count, "cluster_weight_share": cluster_share}


def classify_materiality(relative_delta: float, weight_delta: float) -> str:
    r = abs(relative_delta) if np.isfinite(relative_delta) else 0.0
    w = abs(weight_delta) if np.isfinite(weight_delta) else 0.0
    if r >= 0.10 or w >= 0.25:
        return "High"
    if r >= 0.02 or w >= 0.05:
        return "Meaningful"
    if r >= 0.005 or w >= 0.02:
        return "Moderate"
    return "Small"


def classify_direction(prev_shares: float, curr_shares: float, adjusted_delta: float) -> str:
    if prev_shares <= 0 < curr_shares:
        return "Buy"
    if prev_shares > 0 >= curr_shares:
        return "Sell"
    if prev_shares <= 0:
        return "Neutral"
    rel = adjusted_delta / prev_shares
    if rel >= SIGNAL_THRESHOLD_REL:
        return "Buy"
    if rel <= -SIGNAL_THRESHOLD_REL:
        return "Sell"
    return "Neutral"


def compare_pair(prev_snap: Snapshot, curr_snap: Snapshot) -> pd.DataFrame:
    prev = load_normalized(prev_snap.path, prev_snap.fund)
    curr = load_normalized(curr_snap.path, curr_snap.fund)
    flow = estimate_flow_factor(prev, curr)
    merged = prev.merge(curr, on="ticker", how="outer", suffixes=("_prev", "_curr"))
    merged["company"] = merged["company_curr"].fillna(merged["company_prev"]).fillna("")
    for col in ("shares_prev", "shares_curr", "weight_prev", "weight_curr"):
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)
    flow_factor = flow["flow_factor"]
    merged["raw_shares_delta"] = merged["shares_curr"] - merged["shares_prev"]
    merged["expected_flow_shares"] = np.where((merged["shares_prev"] > 0) & np.isfinite(flow_factor), merged["shares_prev"] * flow_factor, 0.0)
    merged["flow_adjusted_manager_delta"] = np.where(
        (merged["shares_prev"] > 0) & (merged["shares_curr"] > 0) & np.isfinite(flow_factor),
        merged["raw_shares_delta"] - merged["expected_flow_shares"],
        merged["raw_shares_delta"],
    )
    merged["relative_manager_delta"] = np.where(merged["shares_prev"] > 0, merged["flow_adjusted_manager_delta"] / merged["shares_prev"], np.nan)
    merged["weight_delta"] = merged["weight_curr"] - merged["weight_prev"]
    merged["direction"] = [classify_direction(p, c, a) for p, c, a in zip(merged["shares_prev"], merged["shares_curr"], merged["flow_adjusted_manager_delta"])]
    merged["materiality"] = [classify_materiality(r, w) for r, w in zip(merged["relative_manager_delta"], merged["weight_delta"])]
    merged["position_change"] = np.select(
        [(merged["shares_prev"] <= 0) & (merged["shares_curr"] > 0), (merged["shares_prev"] > 0) & (merged["shares_curr"] <= 0)],
        ["New Position", "Exit"], default="Existing")
    profile = FUND_PROFILES[curr_snap.fund]
    merged["trade_date"] = curr_snap.trade_date.isoformat()
    merged["reported_date"] = curr_snap.reported_date.isoformat()
    merged["previous_reported_date"] = prev_snap.reported_date.isoformat()
    merged["fund"] = curr_snap.fund
    merged["manager"] = profile["manager"]
    merged["manager_group"] = profile["group"]
    merged["style"] = profile["style"]
    merged["is_ark"] = curr_snap.is_ark
    merged["flow_factor"] = flow_factor
    merged["flow_confidence"] = flow["flow_confidence"]
    merged["flow_common_count"] = flow["common_count"]
    merged["flow_cluster_count"] = flow["cluster_count"]
    merged["flow_cluster_weight_share"] = flow["cluster_weight_share"]
    cols = [
        "trade_date", "reported_date", "previous_reported_date", "fund", "manager", "manager_group", "style", "is_ark",
        "ticker", "company", "shares_prev", "shares_curr", "weight_prev", "weight_curr", "raw_shares_delta",
        "flow_factor", "flow_confidence", "flow_common_count", "flow_cluster_count", "flow_cluster_weight_share",
        "expected_flow_shares", "flow_adjusted_manager_delta", "relative_manager_delta", "weight_delta",
        "direction", "materiality", "position_change",
    ]
    return merged[cols]


def build_fund_flows(snapshots: dict[str, list[Snapshot]]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for fund, snaps in snapshots.items():
        for prev_snap, curr_snap in zip(snaps, snaps[1:]):
            try:
                frames.append(compare_pair(prev_snap, curr_snap))
            except Exception as exc:
                log(f"{fund} {prev_snap.reported_date}->{curr_snap.reported_date} failed: {exc}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["trade_date", "fund", "ticker"]).reset_index(drop=True)


def max_materiality(values: Iterable[str]) -> str:
    vals = list(values)
    return max(vals, key=lambda x: MATERIALITY_ORDER.get(x, -1)) if vals else "Small"


def aggregate_manager_signals(fund_flows: pd.DataFrame) -> pd.DataFrame:
    if fund_flows.empty:
        return pd.DataFrame()
    rows = []
    keys = ["trade_date", "manager", "manager_group", "style", "ticker"]
    for key, g in fund_flows.groupby(keys, sort=True):
        trade_date, manager, manager_group, style, ticker = key
        buys = sorted(g.loc[g["direction"] == "Buy", "fund"].unique())
        sells = sorted(g.loc[g["direction"] == "Sell", "fund"].unique())
        neutral = sorted(g.loc[g["direction"] == "Neutral", "fund"].unique())
        direction = "Mixed" if buys and sells else "Buy" if buys else "Sell" if sells else "Neutral"
        rows.append({
            "trade_date": trade_date, "manager": manager, "manager_group": manager_group, "style": style, "ticker": ticker,
            "company": next((x for x in g["company"] if isinstance(x, str) and x), ""),
            "manager_direction": direction, "funds_observed": "|".join(sorted(g["fund"].unique())),
            "fund_count_observed": int(g["fund"].nunique()), "buy_fund_count": len(buys), "sell_fund_count": len(sells),
            "neutral_fund_count": len(neutral), "buy_funds": "|".join(buys), "sell_funds": "|".join(sells),
            "max_materiality": max_materiality(g["materiality"]),
            "sum_flow_adjusted_shares": float(g["flow_adjusted_manager_delta"].sum()),
            "max_current_weight": float(g["weight_curr"].max()), "max_previous_weight": float(g["weight_prev"].max()),
            "flow_confirmed_fund_count": int(g["flow_factor"].notna().sum()),
            "ark_internal_breadth": len(buys) if manager_group == "ARK" else 0,
        })
    out = pd.DataFrame(rows).sort_values(["trade_date", "manager", "ticker"]).reset_index(drop=True)
    return add_persistence(out)


def add_persistence(manager_signals: pd.DataFrame) -> pd.DataFrame:
    if manager_signals.empty:
        return manager_signals
    out = manager_signals.copy()
    all_dates = sorted(out["trade_date"].unique())
    date_index = {d: i for i, d in enumerate(all_dates)}
    lookup = {(r.manager, r.ticker, r.trade_date): r.manager_direction for r in out.itertuples(index=False)}
    fields = {k: [] for k in ("buy_days_5d", "sell_days_5d", "buy_days_20d", "sell_days_20d", "consecutive_buy_days", "consecutive_sell_days")}
    for r in out.itertuples(index=False):
        idx = date_index[r.trade_date]
        last5 = all_dates[max(0, idx - 4): idx + 1]
        last20 = all_dates[max(0, idx - 19): idx + 1]
        dirs5 = [lookup.get((r.manager, r.ticker, d), "Neutral") for d in last5]
        dirs20 = [lookup.get((r.manager, r.ticker, d), "Neutral") for d in last20]
        fields["buy_days_5d"].append(sum(x == "Buy" for x in dirs5))
        fields["sell_days_5d"].append(sum(x == "Sell" for x in dirs5))
        fields["buy_days_20d"].append(sum(x == "Buy" for x in dirs20))
        fields["sell_days_20d"].append(sum(x == "Sell" for x in dirs20))
        bs = ss = 0
        for d in reversed(all_dates[:idx + 1]):
            val = lookup.get((r.manager, r.ticker, d), "Neutral")
            if val == "Buy" and ss == 0:
                bs += 1
            elif val == "Sell" and bs == 0:
                ss += 1
            else:
                break
        fields["consecutive_buy_days"].append(bs)
        fields["consecutive_sell_days"].append(ss)
    for name, values in fields.items():
        out[name] = values
    return out


def consensus_status(buyers: int, sellers: int) -> str:
    if buyers and sellers:
        return "Institutional Divergence"
    if buyers >= 3:
        return "Strong Buy Confirmation"
    if buyers == 2:
        return "Moderate Buy Confirmation"
    if buyers == 1:
        return "Single Manager Buy"
    if sellers >= 3:
        return "Strong Sell Confirmation"
    if sellers == 2:
        return "Moderate Sell Confirmation"
    if sellers == 1:
        return "Single Manager Sell"
    return "Neutral"


def build_consensus(manager_signals: pd.DataFrame) -> pd.DataFrame:
    if manager_signals.empty:
        return pd.DataFrame()
    rows = []
    for (trade_date, ticker), g in manager_signals.groupby(["trade_date", "ticker"], sort=True):
        buyers = sorted(g.loc[g["manager_direction"] == "Buy", "manager"].unique())
        sellers = sorted(g.loc[g["manager_direction"] == "Sell", "manager"].unique())
        mixed = sorted(g.loc[g["manager_direction"] == "Mixed", "manager"].unique())
        buyer_styles = sorted(g.loc[g["manager_direction"] == "Buy", "style"].unique())
        ark = g[g["manager_group"] == "ARK"]
        rows.append({
            "trade_date": trade_date, "ticker": ticker,
            "company": next((x for x in g["company"] if isinstance(x, str) and x), ""),
            "observed_manager_count": int(g["manager"].nunique()), "buying_manager_count": len(buyers),
            "selling_manager_count": len(sellers), "mixed_manager_count": len(mixed),
            "buying_managers": "|".join(buyers), "selling_managers": "|".join(sellers), "mixed_managers": "|".join(mixed),
            "buying_style_count": len(buyer_styles), "buying_styles": "|".join(buyer_styles),
            "cross_style_confirmation": bool(len(buyers) >= 2 and len(buyer_styles) >= 2),
            "consensus_status": consensus_status(len(buyers), len(sellers)),
            "ark_direction": ark["manager_direction"].iloc[0] if not ark.empty else "Not Observed",
            "ark_buying_fund_count": int(ark["buy_fund_count"].iloc[0]) if not ark.empty else 0,
            "ark_selling_fund_count": int(ark["sell_fund_count"].iloc[0]) if not ark.empty else 0,
            "ark_buy_days_5d": int(ark["buy_days_5d"].iloc[0]) if not ark.empty else 0,
            "ark_buy_days_20d": int(ark["buy_days_20d"].iloc[0]) if not ark.empty else 0,
            "max_manager_materiality": max_materiality(g["max_materiality"]),
            "eligibility_model_status": "PENDING_MANDATE_VALIDATION",
            "score_status": "NOT_SCORED_V1",
        })
    return pd.DataFrame(rows).sort_values(["trade_date", "buying_manager_count", "selling_manager_count", "ticker"], ascending=[True, False, False, True]).reset_index(drop=True)


def status_payload(snapshots, fund_flows, manager_signals, consensus, ark_date_methods) -> dict:
    latest_by_fund = {}
    for fund, snaps in snapshots.items():
        latest_by_fund[fund] = {
            "manager": FUND_PROFILES[fund]["manager"], "style": FUND_PROFILES[fund]["style"], "snapshot_count": len(snaps),
            "latest_reported_date": snaps[-1].reported_date.isoformat() if snaps else None,
            "latest_trade_date": snaps[-1].trade_date.isoformat() if snaps else None,
        }
    latest_confirmed = None
    if not consensus.empty:
        coverage = consensus.groupby("trade_date")["observed_manager_count"].max()
        eligible = coverage[coverage >= 2]
        if not eligible.empty:
            latest_confirmed = str(eligible.index.max())
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "phase": "V1_DATA_AND_SIGNAL_DEFINITION",
        "scoring_enabled": False, "predictive_validation_enabled": False,
        "signal_threshold_relative_shares": SIGNAL_THRESHOLD_REL, "funds": latest_by_fund,
        "ark_reported_date_mapping": ark_date_methods,
        "rows": {"fund_flows": int(len(fund_flows)), "manager_signals": int(len(manager_signals)), "consensus": int(len(consensus))},
        "latest_confirmed_cross_manager_date": latest_confirmed,
        "data_discipline": {
            "ark_funds_count_as_one_independent_manager": True,
            "creation_redemption_normalization": True,
            "eligibility_denominator": "not yet activated; mandate validation required",
            "score": "deliberately disabled until data/signals are validated",
        },
    }


def write_outputs(fund_flows, manager_signals, consensus, status) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    fund_flows.to_csv(OUTPUT_ROOT / "institutional_fund_flows.csv", index=False)
    manager_signals.to_csv(OUTPUT_ROOT / "institutional_manager_signals.csv", index=False)
    consensus.to_csv(OUTPUT_ROOT / "institutional_consensus.csv", index=False)
    (OUTPUT_ROOT / "institutional_consensus.json").write_text(json.dumps(consensus.to_dict(orient="records"), indent=2, ensure_ascii=False), encoding="utf-8")
    (OUTPUT_ROOT / "institutional_consensus_status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build flow-adjusted cross-manager institutional signals.")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    snapshots, ark_date_methods = discover_snapshots()
    fund_flows = build_fund_flows(snapshots)
    manager_signals = aggregate_manager_signals(fund_flows)
    consensus = build_consensus(manager_signals)
    status = status_payload(snapshots, fund_flows, manager_signals, consensus, ark_date_methods)
    write_outputs(fund_flows, manager_signals, consensus, status)
    active_managers = {FUND_PROFILES[f]["manager"] for f, snaps in snapshots.items() if len(snaps) >= 2}
    log(f"fund_flows={len(fund_flows)} manager_signals={len(manager_signals)} consensus={len(consensus)} active_managers={len(active_managers)}")
    if args.strict and len(active_managers) < 2:
        raise SystemExit("Need at least two independent managers with two snapshots each.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
