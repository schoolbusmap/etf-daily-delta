# Institutional Consensus Layer v1

## Goal

Use daily holdings changes from ARK and five independent actively managed ETF families to reduce the retail-investor research universe. The system is a research-priority engine, not an automatic buy system.

## Development order

1. **Data correctness** — normalize dates, shares, weights, fund flows, manager identity.
2. **Signal definition** — define Buy/Sell, persistence, cross-manager confirmation and divergence.
3. **Scoring** — only after the first two layers are stable and reviewed.
4. **Predictive validation** — test 1D/5D/20D/60D excess returns and drawdowns before changing score weights.

v1 intentionally implements stages 1 and 2 only. `score_status=NOT_SCORED_V1` is deliberate.

## Independent manager identity

ARKK / ARKQ / ARKW / ARKG / ARKF / ARKX are six funds but **one independent manager: ARK Invest**. Their synchronized trades are recorded as ARK internal breadth, not six independent institutional votes.

Current independent manager groups:

- ARK Invest — ARKK, ARKQ, ARKW, ARKG, ARKF, ARKX
- J.P. Morgan Asset Management — JEPI
- Dimensional Fund Advisors — DFAC
- Capital Group — CGGR
- Avantis Investors — AVUV
- Amplify ETFs — BLOK

The style labels in code are an analyst taxonomy for cross-style research; they are not a claim that each provider describes itself using those exact labels.

## Date normalization

Standard ETF snapshots use their published holdings `Date` as the holdings/trade session date.

ARK official Full Holdings files use a next-trading-day displayed date. For ARK, v1 maps the reported date to the latest observed standard-fund market date strictly before it. This handles weekends/holidays better than subtracting one calendar day. A weekday fallback is used only when no standard market date exists.

## Creation/redemption normalization

Raw holdings delta is not automatically a manager trade.

For each fund and snapshot pair:

1. Match securities present in both snapshots.
2. Compute `current_shares / previous_shares - 1`.
3. Use prior portfolio weight as the robust-median weight.
4. Estimate a central proportional scaling cluster.
5. Require at least 10 common positions and at least 35% of candidate portfolio weight in the central cluster.
6. If the cluster is credible, compute:

`flow_adjusted_manager_delta = raw_shares_delta - previous_shares * estimated_flow_factor`

If the flow factor is not credible, the record remains available but `flow_confidence` shows that normalization is unavailable/weak.

This is an estimate, not an audited execution blotter. Corporate actions can still create false deltas and should be added as a later exclusion layer.

## Initial signal definition

For an existing position, v1 calls a manager-level fund change a directional Buy/Sell when the absolute flow-adjusted share change is at least **0.5% of previous shares**. New positions and exits are directional automatically.

This 0.5% threshold is a starting research heuristic, not a validated optimal threshold.

Materiality labels:

- Small
- Moderate
- Meaningful
- High

They use relative share change and portfolio-weight change as descriptive features; they are not yet a score.

## Manager aggregation

Multiple ARK funds trading the same ticker are aggregated to one ARK manager row while preserving:

- `buy_fund_count`
- `sell_fund_count`
- `ark_internal_breadth`
- fund names
- maximum materiality

If one manager has funds buying and selling the same ticker on the same date, manager direction is `Mixed`.

## Cross-manager statuses

- `Strong Buy Confirmation` — 3+ independent managers Buy, no independent manager Sell.
- `Moderate Buy Confirmation` — 2 independent managers Buy, no Sell.
- `Single Manager Buy` — 1 manager Buy, no Sell.
- `Institutional Divergence` — at least one independent manager Buy and at least one Sell.
- Symmetric sell statuses are defined the same way.

`cross_style_confirmation` requires at least two independent buying managers from at least two configured style buckets.

## Eligibility denominator is intentionally pending

v1 does **not** claim that all six independent managers are eligible to own every ticker. A thematic blockchain fund not owning a biotech stock is not negative evidence.

Therefore v1 exposes `observed_manager_count` and sets:

`eligibility_model_status=PENDING_MANDATE_VALIDATION`

A future mandate/eligibility layer should be built from documented fund mandates and observed investable universes before using ratios such as `2 of 3 eligible managers buying`.

## Outputs

- `output/institutional_fund_flows.csv` — fund-level raw and flow-adjusted changes.
- `output/institutional_manager_signals.csv` — one row per date × independent manager × ticker.
- `output/institutional_consensus.csv` — one row per date × ticker.
- `output/institutional_consensus.json` — machine-readable consensus rows.
- `output/institutional_consensus_status.json` — data coverage, date mapping and methodology state.

## Next validation gates

Before enabling a 0–100 Institutional Consensus Score:

1. Review false Buy/Sell classifications caused by ETF flow.
2. Add corporate-action exclusions.
3. Validate each fund's mandate/eligibility model.
4. Accumulate enough daily history to measure persistence.
5. Add market-price history and benchmark-relative forward returns.
6. Compare ARK-only vs ARK+1 manager vs ARK+2+ manager cohorts.
7. Only then activate scoring and optimize weights out-of-sample.
