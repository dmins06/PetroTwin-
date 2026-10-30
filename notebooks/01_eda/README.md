# Exploratory analysis

Use notebooks here for descriptive analysis and plots. Keep reusable data transformations in `src/petrotwin/`, capture the data snapshot and date range used, and avoid committing generated datasets or secrets.

Before starting the EDA notebook, run and review `../00_data_preparation/01_preprocessing_quality_audit.ipynb`. Resolve every FAIL check and document the accepted WARN items. The quality audit does not analyze target-feature relationships; that belongs here after the data handoff gate passes.

## Fuel-market EDA

`01_fuel_market_eda.ipynb` covers the run snapshot, product-level price/cadence/coverage metrics, source-reported changes, calendar pattern, feature missingness, Brent/FX history, and exploratory within-product associations. It has a compact KPI/product table and seven diagnostic figures. The notebook reads the latest accepted run through `data/processed/current.json`, saves shareable figures to `reports/figures/eda/`, and does not modify source or processed data. The short interpretation and caveats are in `reports/eda-summary.md`.

Install the optional notebook dependencies with `python -m pip install -e "../..[eda]"` from this directory, or `python -m pip install -e ".[eda]"` from the repository root. Run the notebook from within the repository checkout so it can resolve the data pointer.
