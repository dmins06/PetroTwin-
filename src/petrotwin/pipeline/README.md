# Data pipeline

Run from the repository root after placing the three source snapshots in `data/raw/`:

```bash
PYTHONPATH=src python -m petrotwin.data.pipeline
```

Optional arguments: `--raw-dir`, `--processed-dir`, `--log-dir`, and `--config`. The default runner reads `configs/default.yaml` and requires:

- `brent_crude_prices.csv`
- `usd_vnd_rates.csv`
- `data_price_petro.csv`

Each source is parsed with a strict, versioned schema. The pipeline hashes each raw file, config, schema version, pipeline version, and pipeline code to create an idempotent run ID. It writes the canonical market and fuel tables, an event-aligned `fuel_market_panel.csv`, a manifest, a quality report, and `quarantine.csv` under `data/processed/runs/<run_id>/`. The manifest records SHA-256 hashes and row counts for every output table; rerunning an existing run checks those hashes before reusing the immutable snapshot. CSV line endings are fixed so output hashes do not depend on the host platform.

The panel joins market observations strictly before each local fuel effective date and records each feature's observation date and age. `data/processed/current.json` is atomically replaced only after validation succeeds. Earlier runs remain available for reproducibility. Append-only execution logs are stored under `data/interim/logs/pipeline.log`; batch-level failures leave the current pointer unchanged.

The manual MOIT fund annotations are validated before joining: keys must be unique, products and dates must be recognized, amounts must be non-negative integers, units must match the product, and every annotation must match exactly one valid fuel event. An amount of zero means verified no-spend; a positive amount means verified spend; no matching annotation remains NULL/unverified. The integrated panel is checked for one-to-one fuel-event coverage and strictly prior market features before publication.

Exact repeated rows are collapsed and counted. Conflicting keys and schema changes fail the batch. Invalid individual values are recorded with a reason and original row in quarantine. The present source snapshot has 64 zero-price FO rows; those are quarantined. Brent's 62 explicitly missing observations remain in the canonical market table with a NULL value and `missing` status. The quality report distinguishes 161 raw `24:00` timestamps from the 131 valid canonical rows normalized to next-day midnight. No interpolation or forward-fill is performed.

The runner uses the source snapshots already collected; it does not fetch new data from the internet. Source acquisition, licenses, and the semantics of zero-price FO rows still require confirmation with publishers. See [the canonical schema and cleaning contract](../../../docs/DATA_DICTIONARY.md) before changing parsers or mappings.
