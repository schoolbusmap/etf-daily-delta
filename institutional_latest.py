#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

OUTPUT_ROOT = Path("output")
CONSENSUS_PATH = OUTPUT_ROOT / "institutional_consensus.csv"
STATUS_PATH = OUTPUT_ROOT / "institutional_consensus_status.json"

MATERIALITY_RANK = {"Small": 0, "Moderate": 1, "Meaningful": 2, "High": 3}
STATUS_RANK = {
    "Strong Buy Confirmation": 6,
    "Moderate Buy Confirmation": 5,
    "Institutional Divergence": 4,
    "Single Manager Buy": 3,
    "Strong Sell Confirmation": 2,
    "Moderate Sell Confirmation": 1,
    "Single Manager Sell": 0,
}


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    df = pd.read_csv(CONSENSUS_PATH)

    signals = df[df["consensus_status"] != "Neutral"].copy()
    signals["_status_rank"] = signals["consensus_status"].map(STATUS_RANK).fillna(-1)
    signals["_materiality_rank"] = signals["max_manager_materiality"].map(MATERIALITY_RANK).fillna(-1)
    signals = signals.sort_values(
        ["trade_date", "_status_rank", "buying_manager_count", "cross_style_confirmation", "_materiality_rank", "ticker"],
        ascending=[True, False, False, False, False, True],
    )
    clean_cols = [c for c in signals.columns if not c.startswith("_")]
    signals[clean_cols].to_csv(OUTPUT_ROOT / "institutional_consensus_signals.csv", index=False)

    latest_date = status.get("latest_confirmed_cross_manager_date")
    latest = signals[signals["trade_date"] == latest_date].copy() if latest_date else signals.iloc[0:0].copy()
    latest = latest.sort_values(
        ["_status_rank", "buying_manager_count", "cross_style_confirmation", "_materiality_rank", "ticker"],
        ascending=[False, False, False, False, True],
    )
    latest_clean = latest[clean_cols]
    latest_clean.to_csv(OUTPUT_ROOT / "institutional_consensus_latest.csv", index=False)
    (OUTPUT_ROOT / "institutional_consensus_latest.json").write_text(
        json.dumps(latest_clean.to_dict(orient="records"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = {
        "latest_confirmed_cross_manager_date": latest_date,
        "signal_rows_all_history": int(len(signals)),
        "signal_rows_latest_date": int(len(latest_clean)),
        "strong_buy_confirmation_count": int((latest_clean["consensus_status"] == "Strong Buy Confirmation").sum()) if not latest_clean.empty else 0,
        "moderate_buy_confirmation_count": int((latest_clean["consensus_status"] == "Moderate Buy Confirmation").sum()) if not latest_clean.empty else 0,
        "divergence_count": int((latest_clean["consensus_status"] == "Institutional Divergence").sum()) if not latest_clean.empty else 0,
        "ark_plus_external_buy_count": int(((latest_clean["ark_direction"] == "Buy") & (latest_clean["buying_manager_count"] >= 2)).sum()) if not latest_clean.empty else 0,
        "note": "Rows are sorted for research review only. No predictive score is active in v1.",
    }
    (OUTPUT_ROOT / "institutional_consensus_latest_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
