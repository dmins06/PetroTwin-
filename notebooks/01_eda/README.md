# Exploratory analysis

Use notebooks here for descriptive analysis and plots. Keep reusable data transformations in `src/petrotwin/`, capture the data snapshot and date range used, and avoid committing generated datasets or secrets.

## Fuel-market EDA

`01_fuel_market_eda.ipynb` covers product coverage, price changes, event timing, and exploratory associations with the prior Brent and USD/VND observations. It reads the latest accepted run through `data/processed/current.json` and does not modify source or processed files.

Install the optional notebook dependencies with `python -m pip install -e "../..[eda]"` from this directory, or `python -m pip install -e ".[eda]"` from the repository root. Run the notebook from within the repository checkout so it can resolve the data pointer.
