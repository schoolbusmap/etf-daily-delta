#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

ARK_BASE = "https://www.ark-funds.com"
ASSET_BASE = "https://assets.ark-funds.com"
FUNDS = {
    "ARKK": (f"{ARK_BASE}/funds/arkk", f"{ASSET_BASE}/fund-documents/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv"),
    "ARKQ": (f"{ARK_BASE}/funds/arkq", f"{ASSET_BASE}/fund-documents/funds-etf-csv/ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv"),
    "ARKW": (f"{ARK_BASE}/funds/arkw", f"{ASSET_BASE}/fund-documents/funds-etf-csv/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv"),
    "ARKG": (f"{ARK_BASE}/funds/arkg", f"{ASSET_BASE}/fund-documents/funds-etf-csv/ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv"),
    "ARKF": (f"{ARK_BASE}/funds/arkf", f"{ASSET_BASE}/fund-documents/funds-etf-csv/ARK_BLOCKCHAIN_&_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv"),
    "ARKX": (f"{ARK_BASE}/funds/arkx", f"{ASSET_BASE}/fund-documents/funds-etf-csv/ARK_SPACE_&_DEFENSE_INNOVATION_ETF_ARKX_HOLDINGS.csv"),
}
DATA_ROOT = Path("data/ark")
OUTPUT_ROOT = Path("output")
STATUS_PATH = OUTPUT_ROOT / "ark_holdings_status.json"
DELTA_CSV = OUTPUT_ROOT / "ark_holdings_delta.csv"
DELTA_JSON = OUTPUT_ROOT / "ark_holdings_delta.json"
CSV_RE = re.compile(r'(?P<url>(?:https://assets\.ark-funds\.com)?/fund-documents/funds-etf-csv/[^\"\'<>]+?\.csv)', re.I)
DATE_RE = re.compile(r"\b(?:as\s+of\s+)?(?P<date>\d{1,2}/\d{1,2}/\d{4})\b", re.I)


def log(msg: str) -> None:
    print(f"[ark] {msg}", flush=True)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        "Accept": "text/csv,text/plain,application/octet-stream,*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


def discover_csv(s: requests.Session, fund: str) -> str:
    page, fallback = FUNDS[fund]
    try:
        r = s.get(page, timeout=30)
        r.raise_for_status()
        found = []
        for m in CSV_RE.finditer(r.text):
            u = urljoin(ARK_BASE, m.group("url").replace("&amp;", "&"))
            if fund.lower() in u.lower() or f"_{fund}_" in u.upper():
                found.append(u)
        if found:
            found.sort(key=lambda u: (not u.startswith(ASSET_BASE), len(u)))
            return found[0]
    except Exception as exc:
        log(f"{fund}: discovery failed ({exc}); using fallback")
    return fallback


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise RuntimeError("cannot decode holdings CSV")


def hnorm(x: str) -> str:
    x = x.strip().lower().replace("$", " dollar ").replace("%", " percent ")
    return re.sub(r"[^a-z0-9]+", "_", x).strip("_")


def num(x):
    if x is None:
        return None
    s = str(x).strip()
    if not s or s.lower() in {"nan", "n/a", "na", "-", "--"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace("%", "").replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(x):
    if not x:
        return None
    s = str(x).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = DATE_RE.search(s)
    return datetime.strptime(m.group("date"), "%m/%d/%Y").date().isoformat() if m else None


def parse_holdings(raw: bytes, fund: str) -> tuple[pd.DataFrame, str]:
    text = decode(raw)
    rows = list(csv.reader(io.StringIO(text)))
    header_i = None
    for i, row in enumerate(rows[:80]):
        hs = {hnorm(c) for c in row}
        if any(x in hs for x in {"company", "company_name"}) and any(x in hs for x in {"ticker", "symbol"}) and any("share" in x for x in hs) and any("weight" in x for x in hs):
            header_i = i
            break
    if header_i is None:
        raise RuntimeError("could not locate holdings header")

    headers = [hnorm(x) for x in rows[header_i]]
    aliases = {
        "date": {"date", "as_of", "as_of_date"},
        "fund": {"fund", "fund_ticker", "etf"},
        "company": {"company", "company_name"},
        "ticker": {"ticker", "symbol"},
        "cusip": {"cusip"},
        "shares": {"shares", "shares_held", "share_count"},
        "market_value": {"market_value", "market_value_dollar", "market_value_dollars", "market_value_usd"},
        "weight": {"weight", "weight_percent", "weight_percentage"},
    }
    cmap = {}
    for key, names in aliases.items():
        for i, h in enumerate(headers):
            if h in names or (key == "market_value" and h.startswith("market_value")) or (key == "weight" and h.startswith("weight")):
                cmap[key] = i
                break
    for key in ("company", "ticker", "shares", "weight"):
        if key not in cmap:
            raise RuntimeError(f"missing required column {key}")

    out, dates = [], []
    width = len(headers)
    for row in rows[header_i + 1:]:
        if not row or not any(c.strip() for c in row):
            continue
        row = row + [""] * max(0, width - len(row))
        ticker = row[cmap["ticker"]].strip().upper()
        shares = num(row[cmap["shares"]])
        weight = num(row[cmap["weight"]])
        if not ticker or len(ticker) > 24 or shares is None or weight is None:
            continue
        row_fund = row[cmap["fund"]].strip().upper() if "fund" in cmap else fund
        if row_fund and row_fund != fund:
            continue
        d = parse_date(row[cmap["date"]]) if "date" in cmap else None
        if d:
            dates.append(d)
        out.append({
            "reported_date": d,
            "fund": fund,
            "company": row[cmap["company"]].strip(),
            "ticker": ticker,
            "cusip": row[cmap["cusip"]].strip() if "cusip" in cmap else "",
            "shares": int(round(shares)),
            "market_value": num(row[cmap["market_value"]]) if "market_value" in cmap else None,
            "weight": float(weight),
        })
    if not out:
        raise RuntimeError("parsed zero holdings rows")
    if dates:
        reported = pd.Series(dates).mode().iloc[0]
    else:
        prefix = "\n".join(",".join(r) for r in rows[:header_i])
        reported = parse_date(prefix)
        if not reported:
            raise RuntimeError("could not identify official holdings date")
    df = pd.DataFrame(out)
    df["reported_date"] = df["reported_date"].fillna(reported)
    df = df.groupby("ticker", as_index=False).agg({
        "reported_date": "first", "fund": "first", "company": "first", "cusip": "first",
        "shares": "sum", "market_value": "sum", "weight": "sum"
    }).sort_values(["weight", "ticker"], ascending=[False, True]).reset_index(drop=True)
    return df[["reported_date", "fund", "company", "ticker", "cusip", "shares", "market_value", "weight"]], reported


def latest_date(fund: str):
    dates = []
    for p in DATA_ROOT.glob(f"*/{fund}/normalized.csv") if DATA_ROOT.exists() else []:
        d = p.parents[1].name
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            dates.append(d)
    return max(dates) if dates else None


def snapshot(fund: str, d: str) -> pd.DataFrame:
    return pd.read_csv(DATA_ROOT / d / fund / "normalized.csv", dtype={"ticker": str, "cusip": str})


def weighted_median(values, weights):
    pairs = sorted((float(v), max(float(w), 0.0)) for v, w in zip(values, weights) if math.isfinite(float(v)) and math.isfinite(float(w)))
    if not pairs:
        return 0.0
    total = sum(w for _, w in pairs)
    if total <= 0:
        return float(pd.Series([v for v, _ in pairs]).median())
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def flow_factor(prev: pd.DataFrame, cur: pd.DataFrame):
    m = prev.merge(cur, on="ticker", suffixes=("_prev", "_cur"))
    m = m[(m.shares_prev > 0) & (m.shares_cur > 0)].copy()
    if len(m) < 8:
        return 0.0, 0.0, len(m)
    m["ratio"] = m.shares_cur / m.shares_prev - 1.0
    m = m[m.ratio.between(-0.35, 0.35)]
    w = m.market_value_prev.fillna(0).abs()
    if float(w.sum()) <= 0:
        w = m.shares_prev.abs()
    center = weighted_median(m.ratio, w)
    dev = (m.ratio - center).abs()
    mad = float(dev.median())
    tol = max(0.0015, min(0.02, 4 * mad if mad > 0 else 0.0015))
    stable = m[dev <= tol]
    if len(stable) >= 5:
        sw = stable.market_value_prev.fillna(0).abs()
        if float(sw.sum()) <= 0:
            sw = stable.shares_prev.abs()
        center = weighted_median(stable.ratio, sw)
    confidence = float(((m.ratio - center).abs() <= max(0.0025, tol)).mean()) if len(m) else 0.0
    return float(center), confidence, len(m)


def make_delta(fund: str, prev_date: str, cur_date: str):
    prev, cur = snapshot(fund, prev_date), snapshot(fund, cur_date)
    factor, confidence, n = flow_factor(prev, cur)
    m = prev.merge(cur, on="ticker", how="outer", suffixes=("_prev", "_cur"), indicator=True)
    for c in ("shares_prev", "shares_cur", "weight_prev", "weight_cur", "market_value_prev", "market_value_cur"):
        m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0.0)
    m["fund"] = fund
    m["previous_reported_date"], m["current_reported_date"] = prev_date, cur_date
    m["previous_shares"], m["current_shares"] = m.shares_prev.round().astype("int64"), m.shares_cur.round().astype("int64")
    m["raw_shares_delta"] = m.current_shares - m.previous_shares
    m["previous_weight"], m["current_weight"] = m.weight_prev, m.weight_cur
    m["weight_delta"] = m.current_weight - m.previous_weight
    m["estimated_creation_redemption_factor"] = factor
    m["flow_factor_confidence"], m["flow_factor_sample_size"] = confidence, n
    m["flow_adjusted_manager_delta"] = m.current_shares - m.previous_shares * (1 + factor)
    prev_company = m.company_prev.fillna("") if "company_prev" in m else ""
    cur_company = m.company_cur.fillna("") if "company_cur" in m else ""
    m["company"] = cur_company.where(cur_company.astype(str).str.len() > 0, prev_company)
    def direction(r):
        if r._merge == "right_only": return "New Position"
        if r._merge == "left_only": return "Closed Position"
        threshold = max(5.0, 0.0005 * max(abs(r.previous_shares), 1.0))
        if r.flow_adjusted_manager_delta > threshold: return "Buy Estimate"
        if r.flow_adjusted_manager_delta < -threshold: return "Sell Estimate"
        return "Flow/Noise"
    m["flow_adjusted_direction"] = m.apply(direction, axis=1)
    cols = ["fund", "ticker", "company", "previous_reported_date", "current_reported_date", "previous_shares", "current_shares", "raw_shares_delta", "previous_weight", "current_weight", "weight_delta", "estimated_creation_redemption_factor", "flow_factor_confidence", "flow_factor_sample_size", "flow_adjusted_manager_delta", "flow_adjusted_direction"]
    return m[cols].sort_values(["flow_adjusted_direction", "flow_adjusted_manager_delta", "ticker"], ascending=[True, False, True]).reset_index(drop=True), factor, confidence


def fetch_one(s: requests.Session, fund: str):
    prev = latest_date(fund)
    url = discover_csv(s, fund)
    log(f"{fund}: GET {url}")
    r = s.get(url, timeout=45, allow_redirects=True)
    r.raise_for_status()
    if len(r.content) < 100:
        raise RuntimeError(f"response too small: {len(r.content)} bytes")
    df, reported = parse_holdings(r.content, fund)
    folder = DATA_ROOT / reported / fund
    raw_path, norm_path = folder / "raw.csv", folder / "normalized.csv"
    saved = not (raw_path.exists() and norm_path.exists())
    if saved:
        folder.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(r.content)
        df.to_csv(norm_path, index=False)
        meta = {
            "fund": fund, "reported_date": reported, "source_url": url,
            "source_tier": "ARK official Full Holdings CSV", "content_type": r.headers.get("content-type", ""),
            "sha256": hashlib.sha256(r.content).hexdigest(), "row_count": int(len(df)),
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "date_alignment_note": "ARK states the holdings document date is the next trading day; align to the prior trading session when reconciling with Daily Trade Notifications."
        }
        (folder / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log(f"{fund}: reported_date={reported}, rows={len(df)}, saved={saved}")
    return {"fund": fund, "source_url": url, "reported_date": reported, "previous_reported_date": prev, "row_count": int(len(df)), "saved": saved, "sha256": hashlib.sha256(r.content).hexdigest(), "content_type": r.headers.get("content-type", ""), "fetched_at_utc": datetime.now(timezone.utc).isoformat()}


def write_outputs(results):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    deltas = []
    for x in results:
        prev, cur = x["previous_reported_date"], x["reported_date"]
        if prev and cur > prev and (DATA_ROOT / prev / x["fund"] / "normalized.csv").exists():
            d, factor, confidence = make_delta(x["fund"], prev, cur)
            x["flow_factor"], x["flow_confidence"] = factor, confidence
            deltas.append(d)
    if deltas:
        combined = pd.concat(deltas, ignore_index=True)
        combined.to_csv(DELTA_CSV, index=False)
        DELTA_JSON.write_text(combined.to_json(orient="records", indent=2), encoding="utf-8")
    status = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "ARK official Full Holdings CSV",
        "funds": {x["fund"]: x for x in results},
        "notes": [
            "reported_date is the date printed in the official holdings file.",
            "ARK states that displayed holdings date is the next trading day; reconcile against the prior trading session.",
            "flow_adjusted_manager_delta estimates manager activity after removing robust proportional fund-flow scaling.",
            "Use the Daily Trade Notification to confirm manager trades and avoid misclassifying creations/redemptions or corporate actions."
        ]
    }
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")


def run(funds):
    s, results, failures = session(), [], []
    for fund in funds:
        try:
            results.append(fetch_one(s, fund))
        except Exception as exc:
            failures.append(f"{fund}: {type(exc).__name__}: {exc}")
            log(f"ERROR {failures[-1]}")
    write_outputs(results)
    return results, failures


def main():
    p = argparse.ArgumentParser(description="Archive ARK official Full Holdings CSV files and estimate flow-adjusted daily deltas.")
    p.add_argument("--funds", nargs="*", default=list(FUNDS))
    p.add_argument("--wait-for-new", action="store_true")
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--retry-seconds", type=int, default=600)
    p.add_argument("--strict", action="store_true")
    a = p.parse_args()
    funds = [x.upper() for x in a.funds]
    bad = [x for x in funds if x not in FUNDS]
    if bad:
        raise SystemExit(f"Unknown fund(s): {', '.join(bad)}")
    baseline = {f: latest_date(f) for f in funds}
    log(f"baseline={baseline}")
    failures = []
    for attempt in range(1, max(1, a.retries) + 1):
        log(f"attempt {attempt}/{max(1, a.retries)}")
        results, failures = run(funds)
        if not a.wait_for_new:
            break
        by_fund = {x["fund"]: x for x in results}
        updated = {f for f, x in by_fund.items() if baseline[f] is None or x["reported_date"] > baseline[f]}
        missing = [f for f in funds if f not in updated]
        if not missing:
            log("all selected funds published a newer snapshot")
            break
        if attempt < max(1, a.retries):
            log(f"waiting for {', '.join(missing)}; sleep {a.retry_seconds}s")
            time.sleep(max(1, a.retry_seconds))
    if failures and a.strict:
        log("final failures: " + " | ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
