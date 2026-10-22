# Data foundation acceptance

**Contract:** `petrotwin-data-v1`  
**Accepted snapshot:** `a9b829f5915084070a99` (published via `data/processed/current.json`, 2026-10-22)
**Decision:** Accept the schema, cleaning pipeline, and team interface for development use, with the source exceptions below kept visible. This is not a claim that every raw value or source license is verified.

This snapshot supersedes `1849dea18e4bf43ee561`. Its four CSVs were rebuilt from the same raw snapshots and compared row-by-row with the earlier accepted outputs. The current manifest records the output row counts and SHA-256 checksums; see `data/processed/runs/a9b829f5915084070a99/manifest.json`.

## Reconciliation results

| Source/output | Raw rows | Published rows | Coverage |
|---|---:|---:|---|
| Brent, `USD/bbl` | 2,126 | 2,126 | 2018-08-22 to 2026-10-14 |
| USD/VND, `VND/USD` | 2,395 | 2,395 | 2018-08-22 to 2026-10-16 |
| Domestic prices, `VND/liter` | 1,450 | 1,386 | 2018-08-22 to 2026-10-16 across product histories |
| Integrated fuel-market panel | — | 1,386 | One row per published fuel event |
| Quarantine | — | 64 | FO zero-price rows, preserved with original row payload |

Market and fuel natural-key checks found no duplicate keys or conflicts. Units match their series/product mappings. The panel key is unique and has one row per valid fuel event. Every non-NULL market feature points to an observation date strictly before the fuel event date, and its recorded age matches the date difference.

## Cleaning exceptions retained in the contract

- 62 Brent observations have a NULL value with source status `missing`; they remain in `market_observation` and are not imputed.
- Eight domestic `price_change_value` entries are NULL; the target price is present and remains usable.
- 64 FO rows have a zero target price and are quarantined: 32 `Dầu FO 3%S`, 32 `Dầu FO 3,5%S`. The latter has one separate positive-price event in the canonical table. Confirm zero semantics with PVOIL before using FO history.
- The raw fuel source contains 161 `24:00` times. The 131 valid published rows carrying that flag are normalized to next-day midnight at `+07:00`; rows rejected for zero price remain only in quarantine.
- Six fuel events have no strictly earlier market observation for each of Brent and FX. Their panel features stay blank; the raw market tables are unchanged.
- Adjustment cadence is event-based, not daily. Brent observations follow business-day spacing (1/3-day calendar gaps); FX contains weekend rows and occasional longer gaps. Do not fill these source series during cleaning.

## Product coverage in the accepted target table

| Product ID | Published rows | Effective-date coverage |
|---|---:|---|
| `diesel_0005s_iv` | 10 | 2018-08-22 to 2019-01-01 |
| `diesel_005s_ii` | 309 | 2018-08-22 to 2026-10-16 |
| `diesel_0001s_v` | 118 | 2024-09-26 to 2026-10-16 |
| `fuel_oil_3s` | 0 | All 32 source rows quarantined, 2018-08-22 to 2019-11-30 |
| `fuel_oil_35s` | 1 | 2024-05-30; 32 earlier/other source rows quarantined |
| `kerosene_ko` | 269 | 2018-08-22 to 2026-03-07 |
| `gasoline_e5_ron92_ii` | 309 | 2018-08-22 to 2026-10-16 |
| `gasoline_e10_ron95_iii` | 73 | 2025-07-31 to 2026-10-16 |
| `gasoline_ron95_iii` | 287 | 2018-08-22 to 2026-05-28 |
| `gasoline_ron95_iv` | 10 | 2018-08-22 to 2019-01-01 |

## Interface decision

Team consumers use [DATA_INTERFACE.md](../docs/DATA_INTERFACE.md): resolve `current.json`, pin its run ID, use the immutable run directory, and consume `fuel_market_panel.csv` for event-level model input. Schema and field semantics are governed by [DATA_DICTIONARY.md](../docs/DATA_DICTIONARY.md). Quarantine rows are audit-only. All v1 header changes require an explicit coordinated schema update.

## Remaining source limits

Only one raw snapshot per source is present, so changes across historical source schemas cannot be assessed. Source reuse/license terms remain unknown; the FX data comes through a secondary aggregator; and PVOIL zero-price semantics remain unconfirmed. The pipeline consumes local snapshots and does not fetch updated source data. These limits do not change the accepted field contract, but they must be addressed before claiming production-grade provenance or automated source refresh.

## Audit performed

The acceptance audit compared raw and canonical row counts, distinct keys, unit domains, date coverage, NULL/status consistency, quarantine reasons, effective-time normalization, panel row cardinality, strict prior-date joins, lag calculations, and quality-report totals. A rebuild with the committed pipeline reproduced the same run ID and output hashes on a second run. All checked invariants passed for the accepted snapshot.
