# MySQL schema acceptance — M2 T12

**Verified:** 2026-10-30 in MySQL Workbench 8.0 using the local `PetroTwin Local` connection. This records the existing database state; the migration was not rerun during this verification.

## Verification results

- Server: MySQL Community Server **26.10.0** at `localhost:3306`.
- Database: `petrotwin`.
- Base tables: **12**; engine check: **12 InnoDB, 0 other engines**.
- Reference seed rows: **10 fuel products** and **2 market series**.
- The verification queries returned successfully; no password or credential was recorded here.

The observed table set is `dataset_current_pointer`, `dataset_run`, `dataset_run_source`, `fuel_market_panel`, `fuel_price_observation`, `fuel_product`, `fund_annotation`, `market_observation`, `market_series`, `quality_report`, `quarantine_record`, and `source_snapshot`.

## Related T13 evidence

T13 is complete for accepted run `28b22922e761cb08a8d8`. The loader published and reconciled 1,390 fuel-price observations, 4,528 market observations, 1,390 panel rows, 565 fund annotations, and 64 quarantine records. Full reconciliation results and data-quality checks are documented in `reports/mysql-reconciliation.md`.
