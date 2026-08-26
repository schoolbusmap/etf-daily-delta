import unittest
from datetime import date

import pandas as pd

from institutional_consensus import (
    aggregate_manager_signals,
    ark_trade_date,
    estimate_flow_factor,
)


class InstitutionalConsensusTests(unittest.TestCase):
    def test_ark_reported_date_maps_to_prior_observed_market_date(self):
        trade_date, method = ark_trade_date(
            date(2026, 8, 26),
            [date(2026, 8, 24), date(2026, 8, 25)],
        )
        self.assertEqual(trade_date, date(2026, 8, 25))
        self.assertEqual(method, "inferred_from_standard_holdings_calendar")

    def test_flow_factor_detects_proportional_creation_scaling(self):
        tickers = [f"T{i}" for i in range(20)]
        prev = pd.DataFrame({
            "ticker": tickers,
            "shares": [1000 + i * 10 for i in range(20)],
            "weight": [5.0] * 20,
        })
        curr = prev.copy()
        curr["shares"] = curr["shares"] * 1.05
        # One active manager add should not move the robust fund-level estimate.
        curr.loc[0, "shares"] += 300
        result = estimate_flow_factor(prev, curr)
        self.assertAlmostEqual(result["flow_factor"], 0.05, places=6)
        self.assertIn(result["flow_confidence"], {"high", "medium"})

    def test_ark_funds_aggregate_to_one_independent_manager(self):
        rows = [
            {
                "trade_date": "2026-08-25", "manager": "ARK Invest", "manager_group": "ARK",
                "style": "innovation_growth", "ticker": "XYZ", "company": "XYZ INC",
                "fund": "ARKK", "direction": "Buy", "materiality": "Meaningful",
                "flow_adjusted_manager_delta": 100.0, "weight_curr": 1.2, "weight_prev": 1.0,
                "flow_factor": 0.01,
            },
            {
                "trade_date": "2026-08-25", "manager": "ARK Invest", "manager_group": "ARK",
                "style": "innovation_growth", "ticker": "XYZ", "company": "XYZ INC",
                "fund": "ARKW", "direction": "Buy", "materiality": "Meaningful",
                "flow_adjusted_manager_delta": 50.0, "weight_curr": 1.0, "weight_prev": 0.9,
                "flow_factor": 0.01,
            },
        ]
        out = aggregate_manager_signals(pd.DataFrame(rows))
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["manager"], "ARK Invest")
        self.assertEqual(out.iloc[0]["buy_fund_count"], 2)
        self.assertEqual(out.iloc[0]["ark_internal_breadth"], 2)


if __name__ == "__main__":
    unittest.main()
