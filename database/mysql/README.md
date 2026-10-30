# MySQL storage design

This initial schema implements the M2 T12 logical model for **MySQL 26.10 / InnoDB**. It mirrors `petrotwin-data-v3` while adding `run_id` to database keys so accepted outputs from different pipeline runs can coexist without overwriting one another.

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

On MySQL 26.10, apply `001_initial_schema.sql` once to create the database, tables, constraints, and current-view definitions. Then apply `002_seed_reference_data.sql` to insert the controlled fuel-product and market-series IDs. The seed migration is safe to reapply. These migrations do not load observations; use the separate T13 loader below after confirming the actual server version with `SELECT VERSION()`.

## T13 accepted-run loader

`src/petrotwin/data/mysql_loader.py` reads the run selected by `data/processed/current.json`. It verifies source and output SHA-256 values (allowing Git's Windows newline conversion), manifest row counts, source schema fingerprints, primary-key uniqueness, strict-prior Brent/FX features, manual annotation keys, and quarantine counts. It uses the existing processed v3 outputs; it does not clean or edit CSVs. Blank numeric cells stay SQL `NULL`.

From the repository root in the VS Code PowerShell terminal, first run the read-only preflight:

```powershell
$env:PYTHONUTF8 = '1'
python -m pip install -e '.[mysql]'
$env:PYTHONPATH = 'src'
python -m petrotwin.data.mysql_loader
```

Publishing requires the explicit `--load` flag and the `PETROTWIN_DB_HOST`, `PETROTWIN_DB_PORT`, `PETROTWIN_DB_NAME`, `PETROTWIN_DB_USER`, and `PETROTWIN_DB_PASSWORD` environment variables. Set them in the current terminal/session or a secret manager; `.env.example` lists names only and is not read automatically by this command. Do not put the password in a command argument or tracked file. Example with a hidden Windows credential prompt:

```powershell
$dbCredential = Get-Credential -UserName root -Message 'Local PetroTwin MySQL password'
$env:PETROTWIN_DB_PASSWORD = $dbCredential.GetNetworkCredential().Password
python -m petrotwin.data.mysql_loader --load
Remove-Item Env:PETROTWIN_DB_PASSWORD
```

The load inserts the dataset run, its source provenance, canonical observations, annotations, panel, quarantine, and quality report in one transaction. It performs count reconciliation before changing the current pointer; any error rolls the transaction back. A run ID that already exists is checked and never auto-repaired or overwritten. The loader refuses to move the pointer backwards to an older run. Use a development database; do not run against shared or production data without the team's approval. After loading, run the read-only `003_reconcile_run.sql` query and record its output in `reports/mysql-reconciliation.md`.

The source CSV retains its displayed values exactly. MySQL stores `source_value` as `DECIMAL`, so the loader removes valid thousands-grouping commas (for example, `22,674` becomes numeric `22674`) when inserting that audit value; the raw source text remains available in the immutable CSV snapshot.

## GitHub Codespaces practice environment

The repository dev container starts a private MySQL 26.10 service. Before the containers start, Codespaces creates a random local development password in `.devcontainer/.mysql.env`. Git ignores that file, so it is not pushed to GitHub. The workspace uses `mycli` with PyMySQL's RSA support so the client can authenticate with MySQL's `caching_sha2_password` method; the MariaDB client bundled by Debian may not support it.

```bash
mycli --defaults-file .devcontainer/.mysql-client.cnf
```

At the `mycli` prompt, select the project database and verify the server:

```bash
USE petrotwin;
SELECT VERSION();
```

Apply each migration once:

```sql
SOURCE database/mysql/001_initial_schema.sql;
SOURCE database/mysql/002_seed_reference_data.sql;
SHOW FULL TABLES IN petrotwin;
```

The service is for development practice only. Keep credentials out of chat, SQL files, and Git. The `mysql-data-v2` volume provides a clean initialization for this credential setup; previous development volumes are left untouched. On a fresh Codespace, re-check `SELECT VERSION()` before using the database.

Before connecting a deployed service, supply credentials through environment variables/secret management and grant only the required schema privileges. Do not put credentials in this migration or commit them in `.env`.
