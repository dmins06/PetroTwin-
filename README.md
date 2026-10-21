# ⛽ PetroTwin — Digital Twin & Decision Support System for E10 RON95 Fuel Price Forecasting

> **Applied Data Science Project**  
> **University of Transport Ho Chi Minh City (UTH)** — Major: Data Science & Artificial Intelligence

---

## 📌 1. Project Overview

**PetroTwin** is an advanced Decision Support System (DSS) & Digital Twin platform developed in Vietnam that integrates **Data Science, Explainable AI (XAI), and MLOps** to forecast **E10 RON95 Bio-fuel** price adjustments by the Ministry of Industry and Trade – Ministry of Finance.

Anticipating Vietnam's national roadmap towards green energy transition and the complete replacement of traditional mineral RON95 petrol, PetroTwin goes beyond standard time-series forecasting by providing:
1. **Econometric & Macroeconomic Foundations:** Integrating USD/VND exchange rates, Brent crude oil prices, Singapore Mogas95 petroleum benchmark prices, and Ethanol E100/agricultural commodity inputs.
2. **Explainable AI (XAI):** Utilizing **SHAP (SHapley Additive exPlanations)** values to dismantle the "AI black box," providing clear breakdown attribution for price shifts (e.g., impact from international oil markets, exchange rate fluctuations, or Price Stabilization Fund interventions).
3. **What-If Scenario Simulation:** Enabling decision-makers to simulate market shocks (e.g., sudden global oil price surges or policy adjustments in fund allocations).
4. **Automated MLOps Pipeline & Error Tracking:** Automatically logging all forecasts into a MySQL database, performing live backtesting against newly published official prices, and dynamically updating MAE/RMSE trends over time.

---

## 🏗️ 2. System Architecture

PetroTwin follows an industry-standard 4-tier architectural design:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        1. DATA PIPELINE LAYER                          │
│  [Global Financial Data]     [Domestic Market Data]  [News Data]       │
│  • Brent Oil (Yahoo Finance) • PVOIL / E100 Prices   • News RSS Feed   │
│  • USD/VND Exchange Rates    • Stabilization Fund    • Energy Journals │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Automated ETL / Ingestion Pipeline
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      2. FEATURE ENGINEERING LAYER                      │
│  • Time-Series Lags (Lag 1, 3, 7) & Rolling Windows (MA7, MA14)         │
│  • Formula E10 Blending (90% Mogas95 + 10% E100)                       │
│  • Market Sentiment Score (PhoBERT NLP)                                │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Formatted Feature Matrix
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                         3. MODELING & XAI LAYER                        │
│  • Model 1: ARIMAX / VAR (Econometrics Baseline)                       │
│  • Model 2: XGBoost / LightGBM / CatBoost (Tabular ML)                 │
│  • Model 3: LSTM / Temporal Fusion Transformer (Deep Learning)         │
│  • Model 4: PhoBERT Sentiment + SHAP Values & Scenario Engine          │
│  • Model 5: Ensemble / Stacking Meta-Learner Model                     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Model Artifacts & Forecast APIs
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               4. PRESENTATION & MLOPS LOGGING LAYER                    │
│  • FastAPI Backend Endpoints                                           │
│  • Streamlit Web Dashboard (Line Chart, MAE/RMSE Trend, SHAP Plot)    │
│  • MySQL Logging Database (Auto Error Calculation upon Actual Price)  │
└───────────────────────────────────┬────────────────────────────────────┘
