# MySQL storage design

This initial schema implements the M2 T12 logical model for **MySQL 26.7 / InnoDB**. It mirrors `petrotwin-data-v3` while adding `run_id` to database keys so accepted outputs from different pipeline runs can coexist without overwriting one another.

## Grain and table responsibilities

| Table | Grain / purpose |
|---|---|
| `dataset_run` | One immutable pipeline publication attempt, including schema/pipeline versions and status. |
| `source_snapshot` | One source file/version, identified by its SHA-256; stores source URL, attribution, terms, every observed retrieval time, and schema fingerprint. |
| `dataset_run_source` | Links a processed run to the source snapshots used in that run. |
| `fuel_product`, `market_series` | Controlled product and market-series IDs, source labels, and units. |
| `market_observation` | One market series/date/source snapshot within one processed run. Missing Brent values remain NULL and retain status `missing`. |
| `fuel_price_observation` | One fuel product/effective instant/source snapshot within one processed run. Keep local event time and `Asia/Ho_Chi_Minh` explicit. |
| `fund_annotation` | One verified product/source-record-date annotation per run and manual-source snapshot, with amount, unit, and HTTPS evidence URL. |
| `fuel_market_panel` | One fuel event joined to its latest strictly prior Brent and FX observations; carries both feature dates, ages, units, and snapshot IDs. |
| `quarantine_record` | One rejected source row and reason per run; do not expose it as a model feature or target. |
| `quality_report` | The run's machine-readable quality report. |
| `dataset_current_pointer` | Singleton pointer to the latest successfully validated run. |

Raw source files remain immutable files under `data/raw`; the database stores their checksums and provenance rather than duplicating raw files in tables. Canonical tables and panel rows are immutable within a `run_id`. Composite foreign keys require each observation snapshot to be linked to that same run in `dataset_run_source`.

## Safe publication flow

1. Create a `dataset_run` with status `building`; record its exact `source_snapshot` rows in `dataset_run_source`.
2. Load and validate into staging tables. Check required columns, types, units, keys, time ordering, join cardinality, strict-prior feature dates, and quarantine counts against the v3 contract.
3. In one transaction, insert the validated canonical rows, panel, quarantine rows, and quality report for the new `run_id`; set the run status to `validated` or `validated_with_quarantine`.
4. Update the singleton `dataset_current_pointer` only after all gates pass, then commit. On batch-level failure, roll back publication and leave the previous pointer unchanged.
5. Consumers query `v_current_fuel_market_panel` (or the other `v_current_*` views) for the active data. Reproducible experiments should query by pinned `run_id` and record it with the model output.

The v3 CSV interface remains the handoff contract for M3/M4/M5. SQL `DATETIME` has no timezone behavior: persist `effective_at_local` as Vietnam wall time with `local_timezone='Asia/Ho_Chi_Minh'`; persist `*_utc` values as UTC by application contract. Never rely on the MySQL session timezone to reinterpret these fields.

## Initial migration

On MySQL 26.7, apply `001_initial_schema.sql` once to create the database, tables, constraints, and current-view definitions. Then apply `002_seed_reference_data.sql` to insert the controlled fuel-product and market-series IDs. The seed migration is safe to reapply. These migrations do not load observations; the T13 loader and reconciliation remain separate work. Confirm the actual server version with `SELECT VERSION()` before applying it; the local machine currently has MySQL 8.0 client binaries and no server listening on `127.0.0.1:3306`.

Before connecting a deployed service, supply credentials through environment variables/secret management and grant only the required schema privileges. Do not put credentials in this migration or commit them in `.env`.

