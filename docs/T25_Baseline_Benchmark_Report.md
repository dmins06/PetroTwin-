# T25 — Baseline Validation & Benchmark Report

**Project:** PetroTwin
**Scope:** Simulated baseline validation and benchmarking
**Status:** SIMULATED — NOT AN OFFICIAL T25 BACKTEST
**Evaluation period:** 2024-01-01 to 2025-12-31
**Source:** Outputs from `python -m petrotwin.evaluation.baseline_benchmark` and `metrics_mean_std.csv`, supplied by the project team.

## 1. Objective

Evaluate two price-persistence baseline models under the time-series validation splits defined in T21, establish reproducible error benchmarks, and identify limitations that must be resolved before official comparison with M4 models (e.g., SARIMAX and XGBoost).

This report documents **simulation results**, not a point-in-time validated operational forecast evaluation.

## 2. Data and validation design

The input is the processed fuel-market panel (`fuel_market_panel.csv`), with domestic fuel-price observations indexed by `product_id` and `effective_at_local` (Asia/Ho_Chi_Minh). The reported validation folds cover **516 target observations** in total.

### 2.1 Expanding-window validation

| Fold | Train observations | Validation period | Validation observations | Count check |
|---|---:|---|---:|---|
| 1 | 632 | 2024-01-01 to 2024-06-30 | 105 | PASS |
| 2 | 737 | 2024-07-01 to 2024-12-31 | 118 | PASS |
| 3 | 855 | 2025-01-01 to 2025-06-30 | 130 | PASS |
| 4 | 985 | 2025-07-01 to 2025-12-31 | 163 | PASS |

### 2.2 Rolling-window validation

T21 specifies a 730-day lookback. The current simulation uses a **moving 730-day history window at each forecast origin**, updated as earlier observed adjustment events become available. The configured fold-start counts are checked separately. Whether this intra-validation update policy is the intended official T21 implementation requires confirmation.

| Fold | Configured training cutoff start | Fold-start train observations | Validation observations | Count check |
|---|---|---:|---:|---|
| 1 | 2022-01-01 | 284 | 105 | PASS |
| 2 | 2022-07-02 | 321 | 118 | PASS |
| 3 | 2023-01-02 | 367 | 130 | PASS |
| 4 | 2023-07-02 | 429 | 163 | PASS |

**Interpretation:** All eight count checks passed. This verifies alignment with the configured fold counts, **not** complete compliance with point-in-time feature availability or official forecast-origin rules.

## 3. Baseline methods and simulation policy

- **Naive Persistence:** predicts the most recent observed price for the same product at or before the origin.
- **Seasonal Naive:** selects an observation near 7 days before the origin (±1 day tolerance), with persistence fallback if no suitable seasonal observation is found.
- **Simulated forecast origin:** `target.effective_at_local - 1 second` for every validation target.
- **Sequential updates:** historical prices from earlier validation events can be used at later origins, provided their effective timestamps precede the origin. This is a sequential simulation, **not** a fixed-model forecast from the start of each fold.
- **Cold start:** a product without any eligible earlier price history receives no prediction and is excluded from error metrics.

**Critical caveat:** The origin is derived from the known target effective time and may not represent a feasible operational prediction time. It should not be treated as an official T20/T21 origin schedule. Historical market-input `available_at` has not been validated; the reported price-only baseline results do not establish Brent/FX point-in-time integrity.

## 4. Results

### 4.1 Per-fold error metrics

MAE and RMSE are in the same price unit as `price_value` (domestic price, conventionally VND/L for applicable liquid fuel products); MAPE is percent. The results below are identical for the Expanding and Rolling simulations.

| Fold | Model | N | MAE | RMSE | MAPE (%) |
|---|---|---:|---:|---:|---:|
| 1 | Naive | 104 | 389.231 | 480.511 | 1.781 |
| 1 | Seasonal Naive | 104 | 389.231 | 480.511 | 1.781 |
| 2 | Naive | 117 | 373.675 | 474.042 | 1.912 |
| 2 | Seasonal Naive | 117 | 373.675 | 474.042 | 1.912 |
| 3 | Naive | 130 | 395.692 | 530.235 | 2.080 |
| 3 | Seasonal Naive | 130 | 395.692 | 530.235 | 2.080 |
| 4 | Naive | 162 | 323.395 | 425.714 | 1.686 |
| 4 | Seasonal Naive | 162 | 334.753 | 457.067 | 1.746 |

### 4.2 Pooled metrics

Pooled metrics are computed over **513 predicted target observations** for each model and each window type.

| Model | N | MAE | RMSE | MAPE (%) |
|---|---:|---:|---:|---:|
| **Naive** | 513 | **366.53** | **475.96** | **1.86** |
| Seasonal Naive | 513 | 370.12 | 485.05 | 1.88 |

### 4.3 Mean and standard deviation across folds

Each of the four fold-level metric values has equal weight. Standard deviations are **sample** standard deviations (`ddof=1`). These figures were identical for Expanding and Rolling in the simulation.

| Model | Mean MAE | Std MAE | Mean RMSE | Std RMSE | Mean MAPE (%) | Std MAPE (percentage points) |
|---|---:|---:|---:|---:|---:|---:|
| **Naive** | **370.498** | 32.734 | **477.626** | 42.754 | **1.865** | 0.171 |
| Seasonal Naive | 373.338 | **27.333** | 485.464 | **31.442** | 1.880 | **0.152** |

The mean across folds differs from the pooled MAE because folds have different numbers of valid predictions. Seasonal Naive has slightly lower cross-fold metric dispersion but slightly higher mean error; with only four folds, dispersion alone should not be interpreted as a strong stability guarantee.

### 4.4 Product-level results

Detailed per-fold, per-product results are stored in `reports/baseline_simulated/metrics_by_product.csv`. As an example, Fold 4 MAE values were:

| Product ID | Naive MAE | Seasonal Naive MAE | Valid predictions |
|---|---:|---:|---:|
| `diesel_0001s_v` | 372.50 | 385.36 | 28 |
| `diesel_005s_ii` | 367.50 | 380.36 | 28 |
| `gasoline_e10_ron95_iii` | 255.45 | 255.45 | 22 |
| `gasoline_e5_ron92_ii` | 287.14 | 300.71 | 28 |
| `gasoline_ron95_iii` | 300.36 | 314.29 | 28 |
| `kerosene_ko` | 342.86 | 355.36 | 28 |

The new E10 product has 23 validation observations, of which one is cold start and 22 are scored. For `fuel_oil_35s` in Fold 1, the only validation observation is cold start, so its per-product metrics are undefined (`NaN`), not zero.

## 5. Cold starts and seasonal behavior

Across 516 validation observations, **three first-observation cold starts** could not be scored:

| Fold | Product ID | First target timestamp (Asia/Ho_Chi_Minh) |
|---|---|---|
| 1 | `fuel_oil_35s` | 2024-05-30 15:00:00+07:00 |
| 2 | `diesel_0001s_v` | 2024-09-26 15:00:00+07:00 |
| 4 | `gasoline_e10_ron95_iii` | 2025-07-31 15:00:00+07:00 |

Although 1, 14, and 23 validation observations respectively belong to products not present in the initial fold training histories, **only the first eligible observation of each new product** lacks any prior product history in this sequential simulation. Later events can use earlier observed prices.

For Expanding, Seasonal Naive used persistence fallback **35 times among 513 scored forecasts**. Of the 513 forecasts, **508** Seasonal Naive predictions equaled Naive predictions. All **five** differences occurred on **2025-07-03** in Fold 4; each differed without fallback, and Naive had lower absolute error in those five cases. This explains the observed Fold 4 metric gap, but it does not prove why all other selected seasonal prices matched persistence without inspecting the selected history timestamps.

## 6. Interpretation

1. **Naive Persistence leads in this simulation:** it has lower pooled MAE, RMSE, and MAPE, and lower mean errors across folds.
2. **Expanding and Rolling results coincide:** this is plausible because both baselines rely on recent price history, which is generally available inside a 730-day window. Exact equivalence of selected history rows has not been independently audited.
3. **The results are not yet suitable for official model selection:** the origin policy is target-derived, and official T20/T21 origin timing, availability constraints, and cross-model comparison sets are not yet established.
4. **Cold starts require an explicit comparison policy:** models must be scored on the same eligible target-origin rows or separately report coverage and cold-start performance.

## 7. Outstanding requirements before official T25 completion

- [ ] Define and freeze a realistic, shared **forecast-origin schedule** aligned with T20 continuous forecasting and official adjustment cycles. Multiple origins may map to one adjustment event.
- [ ] Verify point-in-time availability of every input (`available_at <= forecast_origin`) and publication delays for Brent and USD/VND. `retrieved_at_utc` from retrospective collection does **not** establish historical availability.
- [ ] Agree with M3 on the official Rolling 730-day update semantics and confirm origin/target mapping.
- [ ] Compare Naive, Seasonal Naive, SARIMAX, and XGBoost on **identical folds, origins, horizons, and eligible observation sets**.
- [ ] Preserve prediction metadata, origin timestamps, model versions, and feature snapshots for reproducibility.
- [ ] Add automated tests for no target leakage, origin eligibility, cold-start policy, and metric reproducibility.
- [ ] Keep the locked 2026 final test untouched during development and model selection; evaluate it only according to the T21 final-test policy.

## 8. Reproduction and output artifacts

From the PetroTwin project root with its virtual environment activated:

```powershell
python -m petrotwin.evaluation.baseline_benchmark
```

The existing simulation produces:

```text
reports/baseline_simulated/
├── predictions.csv
├── metrics_by_fold.csv
├── rolling_predictions.csv
├── rolling_metrics_by_fold.csv
├── metrics_by_product.csv
└── metrics_mean_std.csv
```

The `metrics_mean_std.csv` file was produced by a separate aggregation script reading `metrics_by_fold.csv` and `rolling_metrics_by_fold.csv`, not necessarily by the main benchmark module. To keep the report reproducible, the aggregation should eventually be integrated into the evaluation pipeline.

## 9. Conclusion

The **simulated** T25 evaluation pipeline now covers four Expanding folds and four Rolling folds, two baseline methods, per-fold and pooled MAE/RMSE/MAPE, per-product metrics, cross-fold mean/standard deviation, and explicit cold-start reporting. The configured train/validation observation counts all passed.

**Naive Persistence is the lower-error baseline in this simulation.** However, **official T25 sign-off remains pending** until forecast origins, point-in-time data availability, and common M4 comparison rules are finalized and verified.
