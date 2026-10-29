# PetroTwin EDA summary — M2 T10/T11

**Snapshot:** `28b22922e761cb08a8d8`  
**Schema:** `petrotwin-data-v3`  
**Notebook:** [`notebooks/01_eda/01_fuel_market_eda.ipynb`](../notebooks/01_eda/01_fuel_market_eda.ipynb)

This is descriptive analysis of the accepted event-level panel. It does not define the final forecast target, horizon, or production model. Those decisions belong in the Phase 2 handoff with M3.

## T10 — coverage and behavior

The panel contains **1,390 valid fuel-price events** on **310 distinct adjustment dates**, from **2018-08-22 through 2026-10-22**. Fuel prices are irregular events; they have not been expanded to daily frequency or imputed.

| Product | Events | Coverage |
|---|---:|---|
| Diesel 0.05S-II | 310 | 2018-08-22 to 2026-10-22 |
| E5 RON 92-II | 310 | 2018-08-22 to 2026-10-22 |
| RON 95-III | 287 | 2018-08-22 to 2026-05-28 |
| Kerosene | 269 | 2018-08-22 to 2026-03-07 |
| Diesel 0.001S-V | 119 | 2024-09-26 to 2026-10-22 |
| E10 RON 95-III | 74 | 2025-07-31 to 2026-10-22 |
| Diesel 0.005S-IV | 10 | 2018-08-22 to 2019-01-01 |
| RON 95-IV | 10 | 2018-08-22 to 2019-01-01 |
| FO 3.5%S | 1 | 2024-05-30 only |

Across the 1,384 non-null reported price changes, **666 increased**, **653 decreased**, and **65 were unchanged**; 6 reported changes are NULL. The main continuously observed products have a median interval of about 7–9 days between events. Low-count grades should be treated as descriptive only.

The market tables contain 2,128 Brent rows (62 explicitly missing) and 2,400 USD/VND rows. Six earliest fuel events have no strictly prior Brent or FX observation, so both features remain NULL. The month-of-year comparison on complete years 2019–2025 averages **2.7–3.1 distinct adjustment dates per month**; this is fairly even and does not establish a strong seasonal pattern. Counts per year rise from 24–37 dates in 2020–2023 to 52–54 in 2024–2025, so cadence/regime changes matter more than a simple seasonal decomposition.

Fund annotations are verified for **565/1,390 events** (130 positive spending, 435 confirmed no spending); the other **825 remain unverified**. Missing annotations must not be interpreted as zero.

## T11 — exploratory associations

For products with at least 50 events, the notebook computes within-product Spearman correlation between the source-reported price change and the change in the prior Brent/FX feature attached to successive adjustment events.

| Product | Paired events | Price change vs Brent change | Price change vs FX change |
|---|---:|---:|---:|
| Diesel 0.001S-V | 118 | 0.346 | 0.108 |
| Diesel 0.05S-II | 308 | 0.486 | 0.062 |
| E10 RON 95-III | 73 | 0.296 | 0.158 |
| E5 RON 92-II | 308 | 0.494 | 0.069 |
| RON 95-III | 285 | 0.507 | 0.054 |
| Kerosene | 267 | 0.537 | 0.040 |

The exploratory association with Brent changes is positive and moderate for the longer series; the association with FX changes is weaker in this sample. These are screening correlations, not causal estimates or evidence that a feature improves forecasts. Event gaps differ, domestic price controls and taxes can intervene, and a pooled time trend can inflate level correlations.

## Handoff decisions and limits

1. M3 and the group should define the target (price level vs reported price change), forecast horizon, and forecast-origin/as-of cutoff before feature selection. The panel's prior-calendar-date join is useful, but publication-time availability is not proven by that rule alone.
2. E10 has only 74 events and recent coverage; FO has only one valid event because 64 zero-price rows remain quarantined. Avoid claiming model readiness for these grades.
3. Keep NULLs as NULL, preserve product-specific histories, and keep the event grain. Do not infer E10 from RON95, turn events into daily observations, or label unverified fund data as zero.
4. The source audit still records secondary attribution for FX, unverified source-reuse terms, and unresolved PVOIL zero-price FO semantics. See [`data-foundation-acceptance.md`](data-foundation-acceptance.md).

Reproduce the EDA from a checkout with `data/processed/current.json` by opening and running the notebook. It resolves the current snapshot dynamically and records its `run_id` in the output.
