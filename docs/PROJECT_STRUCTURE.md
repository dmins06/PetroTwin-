# Project structure and team workflow

## Responsibilities by area

| Area | Main work | Suggested lead |
|---|---|---|
| `.devcontainer/` | Reproducible Codespaces workspace and private MySQL practice service | M2 |
| `data/`, `src/petrotwin/data/` | Source inventory, ingestion, cleaning, validation, and database-ready schemas | M1, M2 |
| `notebooks/01_eda/` | Data quality review, trends, distributions, and adjustment-period analysis | M2 |
| `database/mysql/` | MySQL/InnoDB migrations, reference IDs, and later CSV-to-database reconciliation | M2 |
| `src/petrotwin/features/`, `notebooks/02_feature_engineering/` | Target definition, temporal alignment, lags, and leakage checks | M3 |
| `src/petrotwin/models/`, `notebooks/03_model_experiments/` | Naive baseline, SARIMAX, XGBoost, and explainability experiments | M3, M4 |
| `src/petrotwin/evaluation/` | Chronological backtests, common test windows, MAE/RMSE, and error analysis | M5 with model owners |
| `src/petrotwin/monitoring/` | Forecast registry, model/data version metadata, actual-price matching, and drift/error summaries | M5 |
| `backend/app/`, `dashboard/` | FastAPI endpoints and Streamlit presentation | M5, with team integration |
| `tests/`, `docs/`, `reports/` | Regression checks, decisions, technical documentation, and final report | All members |

These are starting ownership suggestions, not exclusive boundaries. Agree on interfaces before parallel implementation.

## Reproducible workflow

1. Preserve source snapshots under `data/raw/` and record their provenance.
2. Run deterministic cleaning and schema validation from code in `src/petrotwin/data/`.
3. Save rebuildable outputs under `data/interim/` and validated tables under `data/processed/`.
4. Create features using only information published by each forecast origin.
5. Evaluate baseline, SARIMAX, and XGBoost with the same expanding-window or rolling-origin splits.
6. Log forecasts with timestamps, target dates, model versions, and input cutoffs; attach actual prices when published.
7. Keep notebooks for exploration and move stable logic into the package so tests and services can reuse it.

## Naming and collaboration

- Use descriptive module and notebook names with numeric prefixes for notebook order.
- Keep configuration that is safe to share in `configs/`; use `.env` for local credentials and never commit it.
- Do not commit raw/processed data, forecast logs, model binaries, or generated plots. Store small synthetic fixtures in tests when needed.
- Use a pull request for shared changes. State the data assumptions, time split, and validation evidence for model changes.

M2's ordered tasks, dependencies, deliverables, and acceptance gates are tracked in [M2_WORKPLAN.md](M2_WORKPLAN.md).
