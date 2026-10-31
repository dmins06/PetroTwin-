# MySQL reconciliation — M2 T13

## Offline preflight

**Run:** `28b22922e761cb08a8d8`
**Snapshot:** `petrotwin-data-v3`
**Preflight:** passed locally on 2026-10-30 using `python -m petrotwin.data.mysql_loader`, including decimal validation for formatted source display values such as `22,674`.

| Handoff table | Manifest rows | Preflight result |
|---|---:|---|
| `fuel_price_observation.csv` | 1,390 | SHA-256, manifest count, key, date/time and snapshot checks passed |
| `market_observation.csv` | 4,528 | SHA-256, manifest count, key, status and missing-value checks passed |
| `fuel_market_panel.csv` | 1,390 | SHA-256, manifest count, key, one-to-one event count and strict-prior Brent/FX checks passed |
| `quarantine.csv` | 64 | SHA-256, manifest count, JSON payload and source mapping checks passed |
| Manual fund annotations | 565 | Source hash/schema/count, unique product/date keys, non-orphan keys, units and HTTPS evidence checks passed |

Four source snapshots passed hash, row-count, and schema-fingerprint checks. Windows CRLF checkout conversion is handled by accepting the manifest hash of either the exact bytes or LF-normalized text. Empty numeric CSV cells remain SQL `NULL`; quarantined rows remain separate from valid observations.

## Database status

The accepted run was loaded and reconciled in local MySQL Community Server **26.10.0** on 2026-10-30. Run status is `validated_with_quarantine`.

| MySQL table | Expected | Observed | Result |
|---|---:|---:|---|
| `fuel_price_observation` | 1,390 | 1,390 | PASS |
| `market_observation` | 4,528 | 4,528 | PASS |
| `fuel_market_panel` | 1,390 | 1,390 | PASS |
| `fund_annotation` | 565 | 565 | PASS |
| `quarantine_record` | 64 | 64 | PASS |

Additional checks passed: all 62 `missing` market observations have SQL NULL values; all 4,466 `observed` rows have non-NULL values; quarantine reason `domestic_zero_price_pending_source_confirmation` has 64 rows; singleton current pointer points to `28b22922e761cb08a8d8`; panel rows with same-day/future Brent or FX features = **0**. The server/schema and 12/12 InnoDB checks are recorded in `reports/mysql-schema-acceptance.md`.

The first load attempt surfaced a schema conversion issue: formatted FX `source_value` text such as `22,674` cannot be passed directly to a `DECIMAL` column. The loader now validates and removes valid thousands separators for the numeric MySQL representation. The CSV and raw snapshot remain unchanged. That attempt was rolled back; the corrected load completed and passed the reconciliation above.

The loader publishes all run records in one transaction, checks inserted counts, and only then updates the singleton current pointer. Any exception rolls back the transaction. Existing run IDs are checked and never auto-repaired. A pointer to a newer run cannot be moved backwards.
