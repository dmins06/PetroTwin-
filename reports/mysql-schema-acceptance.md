# MySQL schema acceptance — M2 T12

**Verified:** 2026-10-30 in MySQL Workbench 8.0 using the local `PetroTwin Local` connection. This records the existing database state; the migration was not rerun during this verification.

## Verification results

- Server: MySQL Community Server **26.10.0** at `localhost:3306`.
- Database: `petrotwin`.
- Base tables: **12**; engine check: **12 InnoDB, 0 other engines**.
- Reference seed rows: **10 fuel products** and **2 market series**.
- The verification queries returned successfully; no password or credential was recorded here.

The observed table set is `dataset_current_pointer`, `dataset_run`, `dataset_run_source`, `fuel_market_panel`, `fuel_price_observation`, `fuel_product`, `fund_annotation`, `market_observation`, `market_series`, `quality_report`, `quarantine_record`, and `source_snapshot`.

## Scope and next work

This verifies the T12 schema and reference seeds on the installed server. T13 is still pending: load the accepted run idempotently, reconcile row counts and keys to its manifest, retain NULL and quarantine semantics, and ensure a failed load cannot advance the current-run pointer. Use a development database and keep credentials outside SQL files and Git.
