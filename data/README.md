# Data conventions

- `raw/` contains source snapshots exactly as received. Keep source files immutable; document source URL, retrieval date, units, and license in a small manifest or accompanying note.
- `interim/` contains temporary outputs from cleaning and alignment. It must be possible to rebuild these files from raw inputs and versioned code/configuration.
- `processed/` contains validated tables used by analysis and modeling. Record schema, units, timezone, coverage, and the transformation version.
- Do not commit datasets, credentials, or personally identifying information. The directories are ignored by Git; keep only these instructions under version control.
- Use descriptive lowercase names with dates or source versions, for example `brent_daily_2026-10-01.csv` and `fuel_prices_v1.parquet`.
- Use ISO 8601 dates. Keep source values and units explicit; do not mix VND/liter with other units in the same column.
- Treat each fuel grade as a distinct series. Do not infer E10 history from RON95 or other grades.
- Cleaning must be deterministic and documented. Validation should check required columns, parseable dates, duplicate keys, units, missing targets, and chronological order before saving processed data.
- Split time series chronologically. Fit imputers, scalers, and feature transforms on each training window only.
- Build the current canonical snapshot from the repository root with `PYTHONPATH=src python -m petrotwin.data.pipeline`. Outputs are versioned under `processed/runs/<run_id>/`; `processed/current.json` points to the last successfully validated snapshot. See `src/petrotwin/pipeline/README.md` for pipeline behavior and options.
