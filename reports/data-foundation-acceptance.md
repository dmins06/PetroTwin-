# Data foundation acceptance record

**Contract:** `petrotwin-data-v3`

**Accepted snapshot:** `28b22922e761cb08a8d8` (`data/processed/current.json`)

**Published:** 2026-10-23 06:02:29 UTC

**Decision:** Accept for research and model development with the exceptions below. This approval covers the checked snapshot and interface; it is not approval of automated source refresh, source reuse terms, or unresolved FO zero-price semantics.

## Reconciliation

| Source/table | Raw rows | Canonical rows | Quarantine | Coverage |
|---|---:|---:|---:|---|
| Brent, `USD/bbl` | 2,128 | 2,128 | 0 | 2018-08-22 to 2026-10-20 |
| USD/VND reference, `VND/USD` | 2,400 | 2,400 | 0 | 2018-08-22 to 2026-10-22 |
| PVOIL fuel prices, `VND/liter` | 1,454 | 1,390 | 64 | Effective dates 2018-08-22 to 2026-10-22 15:00 +07:00 |
| `fuel_market_panel.csv` | — | 1,390 | — | One row per canonical fuel event |
| MOIT fund annotations | 565 | 565 applied | 0 | Product/date annotations matched to fuel events |

The canonical `market_observation.csv` contains 4,528 rows: 2,128 Brent and 2,400 USD/VND. The panel has the same 1,390 event keys as `fuel_price_observation.csv`. There are no duplicate or conflicting keys in the three output tables. The current quality report and output checksums are in [the accepted run manifest](../data/processed/runs/28b22922e761cb08a8d8/manifest.json).

## Product coverage

| Product ID | Published events | Effective-date coverage |
|---|---:|---|
| `diesel_0005s_iv` | 10 | 2018-08-22 to 2019-01-01 |
| `diesel_005s_ii` | 310 | 2018-08-22 to 2026-10-22 |
| `diesel_0001s_v` | 119 | 2024-09-26 to 2026-10-22 |
| `fuel_oil_3s` | 0 | All 32 source rows quarantined; 2018-08-22 to 2019-11-30 |
| `fuel_oil_35s` | 1 | One valid event on 2024-05-30; 32 other source rows quarantined |
| `kerosene_ko` | 269 | 2018-08-22 to 2026-03-07 |
| `gasoline_e5_ron92_ii` | 310 | 2018-08-22 to 2026-10-22 |
| `gasoline_e10_ron95_iii` | 74 | 2025-07-31 to 2026-10-22 |
| `gasoline_ron95_iii` | 287 | 2018-08-22 to 2026-05-28 |
| `gasoline_ron95_iv` | 10 | 2018-08-22 to 2019-01-01 |

## Keys, units, and time checks

- Market key: (`series_id`, `observation_date`, `source_id`, `snapshot_id`). Fuel key: (`product_id`, `effective_at_local`, `source_id`, `snapshot_id`). Panel key: (`product_id`, `effective_at_local`, `target_snapshot_id`). Each is unique and complete.
- Units match the schema: Brent `USD/bbl`, FX `VND/USD`, fuel targets `VND/liter`. No unit conversion or aggregation across these series is performed.
- Dates parse as ISO calendar dates. Fuel effective timestamps carry `+07:00`; the source's 24:00 convention is moved to next-day midnight. There are 161 raw `24:00` rows: 131 appear as normalized canonical events and 30 remain in quarantine with their original payload.
- The panel contains 1,390 target events. For every non-NULL Brent/FX feature, its observation date is strictly earlier than the fuel event date, its age equals the calendar-date difference, and its source snapshot is present. Six events have no prior observation in each market series; those features remain NULL.
- Brent has 2,128 distinct dates and 62 explicit missing-value rows. Their status is `missing` and value is NULL. FX has 2,400 distinct dates and no NULL values. Brent's observed intervals are 1 or 3 days except one 5-day gap; FX intervals range from 1 to 10 days. These are reported, not filled.
- Six canonical fuel events have a NULL reported price change. The raw source has eight such rows; two are among zero-price rows in quarantine. NULL is preserved and is never treated as zero.

## Cleaning and exception decisions

- Exact duplicate input rows collapsed: 0. Conflicting natural keys block publication.
- The 64 zero-price FO records are quarantined with reason `domestic_zero_price_pending_source_confirmation`: 32 rows each for `Dầu FO 3%S` and `Dầu FO 3,5%S`. Their original rows remain in `quarantine.csv`; none is used as a target. Confirm their meaning with PVOIL before using FO history.
- No values are interpolated or forward-filled. Domestic price frequency is event-based; market gaps are diagnostics, not synthetic observations.
- The 565 manual fund annotations have unique product/date keys, valid amounts and HTTPS evidence URLs, and each matches one fuel event. They yield 130 confirmed spending labels (`1`), 435 confirmed no-spending labels (`0`), and 825 unverified fuel events (NULL). A missing annotation is not evidence of zero spending.
- The output manifest records source checksums, row counts, retrieval metadata, schema fingerprints, and output checksums. The checked run was produced by pipeline `1.2.0`; the current validation changes were rerun in a temporary directory and reproduced the same output row counts and CSV checksums without changing `current.json`.

## Team interface decision

The accepted handoff is [`DATA_INTERFACE.md`](../docs/DATA_INTERFACE.md), with field types and cleaning semantics governed by [`DATA_DICTIONARY.md`](../docs/DATA_DICTIONARY.md).

1. Resolve `data/processed/current.json` at run time; record both `run_id` and `schema_version` with every experiment.
2. Use `fuel_market_panel.csv` for event-level modeling. Use `fuel_price_observation.csv` and `market_observation.csv` for source-specific analysis and audit.
3. Treat blank CSV cells as NULL. Do not replace missing features or annotations with zero. Do not edit published run files; submit source corrections and rebuild a new immutable run.
4. Keep the product IDs, keys, timestamp offsets, units, and column names fixed within v3. Coordinate and version any contract change before consumers adopt it.
5. Keep quarantine data out of model features and targets. Preserve the run ID and input/output checksums for reproducibility.

## Limits and acceptance scope

This is a local-snapshot pipeline; it does not fetch updates from the internet. Brent is reported as EIA data distributed by FRED. The USD/VND file attributes the SBV reference rate through a secondary aggregator. Reuse/license terms remain unverified in the source manifest. PVOIL zero-price FO semantics also remain unresolved. The pipeline's structural and temporal checks passed, but these source limits prevent a production-grade provenance claim.

The repository does not contain `scripts/test_inspector.py`; the audit was performed directly against the raw CSVs, accepted outputs, quality report, and manifest. A full pipeline reproduction in a temporary directory confirmed 4,528 market rows, 1,390 fuel/panel rows, 565 fund annotations applied, 64 quarantined rows, and matching output checksums. The repository's `current.json` was not changed during that verification.
