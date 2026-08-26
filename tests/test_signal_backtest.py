import unittest
from datetime import date

import pandas as pd

from ark_history_backfill import add_flow_adjustment, aggregate_ark_signals
from signal_backtest import evaluate_signal


class HistoricalSignalTests(unittest.TestCase):
    def test_persistence_uses_trading_session_window_not_last_signal_count(self):
        rows = []
        calendar = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 12)]
        for d in calendar:
            rows.append({"date": d, "fund": "ARKK", "ticker": "ZZZ", "company": "ZZZ", "shares": 1000.0, "deltaShares": 0.0, "weight": 1.0, "previous_shares": 1000.0,
                         "flow_factor": 0.0, "flow_confidence": "high", "flow_cluster_count": 10, "expected_flow_shares": 0.0,
                         "flow_adjusted_manager_delta": 0.0, "relative_manager_delta": 0.0})
        # Directional buys only on session 1 and session 7; they must not count as 2 buys in a 5-session window.
        rows[0]["flow_adjusted_manager_delta"] = 10.0
        rows[0]["relative_manager_delta"] = 0.01
        rows[-1]["flow_adjusted_manager_delta"] = 10.0
        rows[-1]["relative_manager_delta"] = 0.01
        signals = aggregate_ark_signals(pd.DataFrame(rows))
        latest = signals.iloc[-1]
        self.assertEqual(int(latest["buy_days_5d"]), 1)
        self.assertEqual(int(latest["consecutive_buy_days"]), 1)

    def test_flow_adjustment_removes_proportional_scaling(self):
        rows = []
        for i in range(12):
            prev = 1000.0 + i * 10
            curr = prev * 1.05
            rows.append({
                "date": date(2026, 1, 5), "fund": "ARKK", "ticker": f"T{i}", "company": f"T{i}",
                "shares": curr, "deltaShares": curr - prev, "weight": 1.0, "previous_shares": prev,
            })
        adjusted = add_flow_adjustment(pd.DataFrame(rows))
        self.assertAlmostEqual(float(adjusted["flow_factor"].iloc[0]), 0.05, places=3)
        self.assertLess(float(adjusted["flow_adjusted_manager_delta"].abs().median()), 0.01)


class EventStudyTests(unittest.TestCase):
    def test_entry_is_next_session_open_not_same_day_close(self):
        row = pd.Series({
            "signal_date": date(2026, 1, 5), "ticker": "ABC", "company": "ABC", "direction": "Buy",
            "signal_label": "ARK Single-Fund Buy", "buy_fund_count": 1, "sell_fund_count": 0,
            "buy_days_5d": 1, "buy_days_20d": 1, "sell_days_5d": 0, "sell_days_20d": 0,
            "min_flow_confidence": "medium_or_high", "source_kind": "TEST",
        })
        prices = pd.DataFrame([
            {"date": date(2026, 1, 5), "adj_open": 90.0, "adj_high": 110.0, "adj_low": 89.0, "adj_close": 100.0},
            {"date": date(2026, 1, 6), "adj_open": 105.0, "adj_high": 112.0, "adj_low": 104.0, "adj_close": 110.0},
        ])
        spy = pd.DataFrame([
            {"date": date(2026, 1, 5), "adj_open": 500.0, "adj_high": 501.0, "adj_low": 499.0, "adj_close": 500.0},
            {"date": date(2026, 1, 6), "adj_open": 500.0, "adj_high": 506.0, "adj_low": 499.0, "adj_close": 505.0},
        ])
        result = evaluate_signal(row, prices, spy, date(2026, 1, 6))[0]
        self.assertEqual(result["entry_date"], "2026-01-06")
        self.assertAlmostEqual(result["entry_adj_open"], 105.0)
        self.assertAlmostEqual(result["stock_return"], 110.0 / 105.0 - 1.0)


if __name__ == "__main__":
    unittest.main()
