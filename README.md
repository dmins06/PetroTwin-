# PetroTwin

Forecasting Vietnamese Domestic Fuel Prices with Energy Market Data

PetroTwin is a Data Science project that aims to forecast Vietnamese domestic fuel prices using international energy market data, foreign exchange rates, historical domestic fuel prices, and relevant domestic price-regulation factors.

The project focuses on building a reproducible forecasting pipeline, evaluating model performance against baseline methods, and monitoring predictions against officially published prices.

> Project status: In Progress

## 1. Problem Statement

Vietnamese domestic fuel prices are influenced by multiple factors, including international crude oil prices, exchange rates, domestic pricing mechanisms, and government policy adjustments.

PetroTwin investigates whether these factors can improve the accuracy of domestic fuel price forecasts and how forecasting performance changes across different adjustment periods.

The project also addresses the challenge of limited historical data for E10 gasoline by carefully evaluating data availability and distinguishing E10 observations from other fuel types.

## 2. Project Objectives

- Collect and standardize international and domestic fuel price data.
- Analyze relationships between crude oil prices, exchange rates, and domestic fuel prices.
- Develop time-series and machine learning forecasting models.
- Compare forecasting models against a naive baseline.
- Evaluate models using time-aware validation and metrics such as MAE and RMSE.
- Explain model predictions and investigate alternative market scenarios.
- Build a dashboard to visualize historical prices, forecasts, actual prices, and prediction errors.
- Track predictions over time to assess real-world forecasting performance.

## 3. Data Sources

The initial dataset includes:

| Dataset | Description |
|---|---|
| Brent crude oil prices | International crude oil price history |
| USD/VND exchange rates | Historical exchange rates |
| Vietnamese domestic fuel prices | Historical domestic fuel prices, including available E10 and RON95 observations |
| Domestic policy and pricing information | Relevant price-regulation events, subject to source availability |

Each dataset will be inspected for its source, time coverage, units, missing values, duplicates, and update frequency.

Data sources and collection methods will be documented as the project develops.

## 4. Methodology

### Data Engineering
- Data quality assessment
- Data cleaning and standardization
- Temporal alignment
- Data validation and database ingestion

### Exploratory Data Analysis
- Price trend analysis
- Distribution and outlier analysis
- Correlation analysis
- Analysis of price changes across adjustment periods

### Forecasting
Candidate approaches include:
- Naive forecasting baseline
- SARIMAX
- XGBoost

Additional models will be considered only when justified by data availability and evaluation results.

### Model Evaluation
- Walk-forward or time-aware validation
- Mean Absolute Error (MAE)
- Root Mean Squared Error (RMSE)
- Comparison against the baseline
- Error analysis across forecasting periods

### Explainability and Scenario Analysis
- SHAP-based feature contribution analysis where applicable
- Conditional scenario simulations for changes in Brent prices and exchange rates
- Evaluation of whether additional features improve out-of-sample performance

### Forecast Monitoring
- Save forecasts with timestamps and model versions
- Match predictions with the corresponding actual price adjustment
- Track prediction errors after official prices become available

## 5. System Architecture

The planned workflow is:

Raw Data → Data Cleaning → Data Validation → Database → Feature Engineering → Forecasting Models → Evaluation → API → Dashboard

The system is designed to separate data ingestion, model development, evaluation, and presentation so that individual components can be developed and tested independently.

## 6. Technology Stack

Planned technologies:

- Language: Python
- Data processing: Pandas, NumPy
- Machine learning: scikit-learn, XGBoost, statsmodels
- Explainability: SHAP
- Database: MySQL
- Backend API: FastAPI
- Visualization: Streamlit
- Version control: Git and GitHub

The final stack will reflect the technologies actually adopted by the team.

## 7. Team Contributions

The project is developed by a five-member team.

| Member | Primary Responsibilities |
|---|---|
| M1 — Project Leader | Data collection, data cleaning, pipeline development, integration, and quality control |
| M2 — Data Analysis & Database | Exploratory data analysis, database design, data ingestion, and automation |
| M3 — Time Series & Feature Engineering | Forecast target definition, feature engineering, baseline forecasting, and validation design |
| M4 — Machine Learning & Explainability | Model experiments, model comparison, SHAP analysis, and scenario simulations |
| M5 — Forecast Evaluation & Application | Independent evaluation, prediction-error monitoring, API development, and dashboard implementation |

All members contribute to the Data Science workflow through analysis, experiments, validation, or interpretation of results.

## 8. Project Structure

```text
PetroTwin/
├── configs/                 # Versioned, non-secret project settings
├── data/
│   ├── raw/                 # Original source files; never edit in place
│   ├── interim/             # Intermediate outputs; rebuildable
│   ├── processed/           # Validated, analysis-ready datasets
│   └── README.md            # Naming, schema, and provenance conventions
├── notebooks/
│   ├── 01_eda/
│   ├── 02_feature_engineering/
│   └── 03_model_experiments/
├── src/petrotwin/
│   ├── data/                # Cleaning and validation
│   ├── features/            # Point-in-time-safe feature creation
│   ├── models/              # Baseline, SARIMAX, and XGBoost
│   ├── evaluation/          # Walk-forward evaluation and metrics
│   ├── monitoring/          # Forecast records and error tracking
│   └── pipeline/            # Reproducible workflow orchestration
├── backend/app/             # FastAPI service
├── dashboard/               # Streamlit application
├── tests/unit/              # Small isolated tests
├── tests/integration/       # Pipeline and API integration tests
├── reports/                 # Report outline and generated outputs
├── docs/                    # Data, modeling, API, and team guidance
├── .env.example             # Safe configuration template
├── pyproject.toml           # Package metadata and dependency groups
└── README.md
```

See [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) for module ownership and workflow details. Raw and generated datasets, model artifacts, logs, and local secrets are excluded from Git.

## 9. Reproducibility and Data Leakage Prevention

- Preserve original raw data and document cleaning rules.
- Record data cut-off timestamps for forecasts.
- Use only information available at the time each forecast is generated.
- Evaluate models using chronological splits rather than random shuffling.
- Compare models on consistent evaluation periods.
- Store model versions and forecast timestamps.
- Exclude forecasts without corresponding actual prices from error metrics.
- Distinguish observed historical data from assumptions used in scenario simulations.

## 10. Limitations

- E10 historical observations may be limited compared with other fuel types.
- International prices and exchange rates may not align directly with domestic price adjustment dates.
- Domestic pricing regulations and stabilization mechanisms may affect the relationship between market variables and retail prices.
- Historical relationships do not guarantee future forecasting accuracy.
- SHAP explains model behavior, not causal effects.
- Scenario simulations are conditional estimates, not proof of the impact of a policy decision.

## 11. Getting Started

Requirements: Python 3.11 or newer and Git.

```bash
git clone https://github.com/dmins06/PetroTwin-.git
cd PetroTwin-
python -m venv .venv
```

Activate the environment, then install the project and all planned application/model dependencies:

```bash
# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -e ".[models,api,dashboard,dev]"
```

Copy `.env.example` to `.env` and fill in only the local values you need. Never commit `.env` or real credentials. Dataset preparation and app entry points will be added with their respective implementations; until then, use the README files in each area for the intended conventions.

## 12. Project Roadmap

- [ ] Audit and document data sources.
- [ ] Build reproducible data-cleaning pipelines.
- [ ] Complete exploratory data analysis.
- [ ] Design the database and ingestion process.
- [ ] Build the baseline forecasting model.
- [ ] Implement time-aware model evaluation.
- [ ] Compare candidate forecasting models.
- [ ] Add explainability and scenario analysis.
- [ ] Develop the API and dashboard.
- [ ] Track forecasts against actual domestic prices.
- [ ] Complete integration testing and documentation.

## Disclaimer

PetroTwin is an educational and research project. Forecasts are estimates based on available data and modeling assumptions. They should not be interpreted as official fuel prices or guaranteed predictions.
