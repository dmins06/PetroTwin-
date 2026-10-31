# M2 data and database workflow

This guide connects the work M1–M2 have done: source files, the accepted v3 pipeline, quality checks, MySQL publication, and the dataset M3–M5 consume. The accepted CSV contract remains the shared handoff; MySQL provides queryable, versioned storage.

## End-to-end flow

```mermaid
flowchart LR
  A[Official/source snapshots<br/>data/raw + reviewed annotations] --> B[Existing cleaner<br/>src/petrotwin/data/pipeline.py]
  B --> C[Immutable run folder<br/>manifest + quality report + CSVs]
  C --> D[current.json selects accepted run]
  C --> E[Read-only audit and EDA]
  D --> F[mysql_loader preflight<br/>hashes, counts, keys, dates]
  F --> G[One MySQL transaction<br/>provenance + canonical rows + panel]
  G --> H[Reconcile counts and quality]
  H --> I[dataset_current_pointer]
  I --> J[v_current_* views]
  J --> K[M3–M5 analysis and models]
  C --> K
```

**Why two storage forms?** CSV v3 is portable and easy for the whole team to review or load in Python. MySQL adds foreign keys, indexed queries, run history, and safe current-run selection. Neither one should silently replace the other; both refer to the same `run_id` and source hashes.

## Table map and grain

| Table | One row means | Key idea |
|---|---|---|
| `dataset_run` | One published processing run | `run_id` identifies the exact build and status. |
| `source_snapshot` | One source file version | `snapshot_id` is its SHA-256; keep attribution and retrieval times. |
| `dataset_run_source` | One source used by one run | Links a run to each exact source snapshot. |
| `fuel_product`, `market_series` | One controlled product/series definition | Reference IDs prevent spelling variations. |
| `fuel_price_observation` | One product price event | Product + effective local time + source snapshot. |
| `market_observation` | One Brent or FX series/date/source row | Missing observations are explicit status + SQL `NULL`. |
| `fuel_market_panel` | One valid fuel event with prior market context | Retains target and feature provenance/date/age. |
| `fund_annotation` | One verified product/date fund annotation | A blank/unverified annotation is not inserted as zero. |
| `quarantine_record` | One rejected source row | Auditable, never a target or feature. |
| `quality_report` | One machine-readable report per run | Counts, checks, and publication status. |
| `dataset_current_pointer` | Which validated run consumers see by default | Singleton row; changed only after reconciliation. |

## Keys and relationships

```mermaid
erDiagram
  DATASET_RUN ||--o{ DATASET_RUN_SOURCE : uses
  SOURCE_SNAPSHOT ||--o{ DATASET_RUN_SOURCE : belongs_to
  DATASET_RUN ||--o{ FUEL_PRICE_OBSERVATION : contains
  DATASET_RUN ||--o{ MARKET_OBSERVATION : contains
  DATASET_RUN ||--o{ FUEL_MARKET_PANEL : publishes
  DATASET_RUN ||--o{ QUARANTINE_RECORD : records
  DATASET_RUN ||--|| QUALITY_REPORT : summarizes
  DATASET_RUN ||--o| DATASET_CURRENT_POINTER : selected_by
  FUEL_PRODUCT ||--o{ FUEL_PRICE_OBSERVATION : identifies
  FUEL_PRODUCT ||--o{ FUEL_MARKET_PANEL : identifies
  MARKET_SERIES ||--o{ MARKET_OBSERVATION : classifies
```

`run_id` makes observations from different processed versions coexist. Foreign keys from observation rows to `(run_id, snapshot_id)` ensure a row cannot claim a source snapshot that was not linked to that run. The current views filter by the singleton pointer. For reproducible work, query by a pinned `run_id` instead of relying on “current” after the fact.

## Safe procedure for a source refresh

1. **Confirm the source is allowed.** Record publisher, URL, retrieval time, attribution, and reuse terms. Keep terms as unknown until checked; do not automate a download whose terms are unverified.
2. **Preserve inputs.** Put a reviewed new source file in `data/raw/` or update the explicitly reviewed manual annotation file. Never edit a prior raw file or processed run in place.
3. **Run the existing pipeline.** Reuse `src/petrotwin/data/pipeline.py`; do not create a second cleaner. It writes a new immutable run with checksums, manifest, quality report, quarantine, and canonical CSVs.
4. **Review before publication.** Resolve `data/processed/current.json`, inspect the run, run `notebooks/00_data_preparation/01_preprocessing_quality_audit.ipynb`, and review `notebooks/01_eda/01_fuel_market_eda.ipynb` when a source or data distribution changes. Investigate FAILs; record and agree on WARNs.
5. **Run MySQL preflight.** From the repo root in PowerShell:

   ```powershell
   $env:PYTHONUTF8 = '1'
   $env:PYTHONPATH = 'src'
   python -m petrotwin.data.mysql_loader
   ```

6. **Load only an accepted run.** Put the DB password in a hidden local prompt/environment variable; do not put it in SQL, Git, or chat. Run `python -m petrotwin.data.mysql_loader --load` only after preflight succeeds. The loader validates in memory, then publishes its rows in one transaction. It does not use physical staging tables. A failure rolls back and leaves the old pointer in place.
7. **Reconcile.** Run `database/mysql/003_reconcile_run.sql`. Check row counts, explicit missingness, quarantine reasons, current pointer, and strict-prior feature dates. Save the evidence in `reports/mysql-reconciliation.md`.
8. **Hand off the run ID.** Tell M3–M5 which run was accepted. For a reproducible notebook/model, pin its `run_id` and record it with results.

The initial schema migration `001_initial_schema.sql` is for a fresh database only. Do not run it again against the populated project database. `002_seed_reference_data.sql` uses `INSERT IGNORE` and is safe to reapply.

## Safe ways to query

Check the selected run and its source versions:

```sql
SELECT r.run_id, r.schema_version, r.pipeline_version, r.publication_status,
       r.created_at_utc
FROM dataset_current_pointer AS p
JOIN dataset_run AS r ON r.run_id = p.run_id
WHERE p.singleton_id = 1;

SELECT s.source_id, s.raw_filename, s.sha256, s.source_row_count,
       s.retrieved_at_utc_min, s.retrieved_at_utc_max
FROM dataset_current_pointer AS p
JOIN dataset_run_source AS rs ON rs.run_id = p.run_id
JOIN source_snapshot AS s ON s.snapshot_id = rs.snapshot_id
ORDER BY s.source_id;
```

Inspect the event panel through the read-only view:

```sql
SELECT product_id, COUNT(*) AS events,
       MIN(DATE(effective_at_local)) AS first_event,
       MAX(DATE(effective_at_local)) AS last_event,
       SUM(brent_usd_per_barrel IS NULL) AS missing_brent,
       SUM(usd_vnd_rate IS NULL) AS missing_fx
FROM v_current_fuel_market_panel
GROUP BY product_id
ORDER BY events DESC;

SELECT product_id, effective_at_local, price_value, unit_code,
       brent_observation_date, brent_age_days,
       fx_observation_date, fx_age_days
FROM v_current_fuel_market_panel
WHERE product_id = 'gasoline_e5_ron92_ii'
ORDER BY effective_at_local DESC
LIMIT 20;
```

To inspect rejected data, use `quarantine_record` with the current `run_id`; do not join it into the modeling panel. To compare a historical run, replace the view with a direct table query filtered by its pinned `run_id`.

## Practice path

Run these read-only commands in order in Workbench: `SELECT VERSION();`, `SELECT DATABASE();`, `SHOW FULL TABLES;`, inspect `dataset_current_pointer`, query one `v_current_fuel_market_panel` product, then group by product as above. Next, trace one row's `target_snapshot_id` to `dataset_run_source` and `source_snapshot`. This teaches table grain, primary keys, foreign keys, NULL semantics, views, and provenance without changing data.

See [MySQL operations](../database/mysql/README.md), [schema acceptance](../reports/mysql-schema-acceptance.md), and [run reconciliation](../reports/mysql-reconciliation.md) for the exact local setup and evidence.
