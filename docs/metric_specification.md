# PetroTwin – Metric Specification (T24)

## 1. Objective and Scope

This document defines evaluation metrics and reporting rules for the PetroTwin M3 forecasting system.

The specification covers:

- Regression metrics for fuel price forecasting.
- Classification metrics for BOG prediction.
- Edge-case handling.
- Evaluation aggregation and reporting.

All evaluation must follow the chronological validation strategy defined in `configs/validation.yaml` (T21).

## 2. Regression Metrics

### 2.1 Mean Absolute Error (MAE)

MAE is the primary metric for model selection.

\[
MAE = \frac{1}{n}\sum_{i=1}^{n}|y_i-\hat{y}_i|
\]

Where:
- \(y_i\): Actual fuel price.
- \(\hat{y}_i\): Predicted fuel price.
- \(n\): Number of valid observations.

Lower MAE indicates better prediction accuracy.

Unit: VND per corresponding fuel-price unit.

### 2.2 Root Mean Squared Error (RMSE)

RMSE is a secondary metric that penalizes large prediction errors.

\[
RMSE = \sqrt{\frac{1}{n}\sum_{i=1}^{n}(y_i-\hat{y}_i)^2}
\]

Lower RMSE indicates better performance.

### 2.3 Mean Absolute Percentage Error (MAPE)

\[
MAPE = \frac{100}{n}\sum_{i=1}^{n}
\left|\frac{y_i-\hat{y}_i}{y_i}\right|
\]

Zero-value policy:
- Exclude observations where actual price equals zero.
- Do not replace zero denominators with epsilon.
- Report the number of excluded observations.
- Return null if no valid observations remain.

### 2.4 Directional Accuracy

Directional Accuracy measures whether the model predicts the correct direction of price movement.

For each forecast origin:

- Actual direction: compare the target actual price with the latest known price at the forecast origin.
- Predicted direction: compare the predicted target price with that same known price.

Direction labels:
- Increase: positive change.
- Decrease: negative change.
- No change: zero change.

\[
DA = \frac{\text{Number of correct directions}}
{\text{Number of valid predictions}}\times100
\]

The comparison must use the same reference price for actual and predicted direction.

## 3. BOG Classification Metrics

BOG classes:

| Class ID | Class name |
|---|---|
| 0 | NO_ACTION |
| 1 | SPEND |
| 2 | CONTRIBUTE |

### 3.1 Classification Metrics

- Accuracy
- Precision Macro
- Recall Macro
- F1 Macro
- Confusion Matrix

All three classes must remain in the class schema and confusion matrix, including classes with zero support.

### 3.2 Calibration Metrics

**Brier Score**

Measures the squared difference between predicted class probabilities and one-hot encoded actual labels.

For multiclass classification:

\[
BS = \frac{1}{N}\sum_{i=1}^{N}
\sum_{k=1}^{K}(p_{ik}-y_{ik})^2
\]

**Log-loss**

Measures the negative log-likelihood of the true class probability.

\[
LogLoss = -\frac{1}{N}\sum_{i=1}^{N}\log(p_{i,y_i})
\]

Brier Score and Log-loss require probability predictions (either 3-class probability matrix `(N, 3)` summing to `1.0` or binary `P(SPEND)` probabilities in `[0, 1]`).

Probability validation & clipping rules (implemented in `src/petrotwin/evaluation/metrics.py::compute_bog_metrics`):
- Reject NaN/Inf probabilities, values outside `[0, 1]`, or multiclass rows not summing to `1.0` (`atol = 1e-5`).
- Clip probabilities to `[1e-15, 1 - 1e-15]` (`PROB_CLIP_EPS = 1e-15`) before computing `Log-loss` so exact `0.0` or `1.0` probabilities never produce `inf` or `NaN`.
- Binary F1 for `positive_class = 1` (`SPEND`) uses explicit classification `threshold = 0.5` (configurable), clearly separated from probability calibration metrics (`Brier Score`, `Log-loss`).
- Report `class_counts` and `imbalance_ratio` to expose label imbalance explicitly.
- Aggregation (`aggregate_regression_metrics`) refuses to pool mixed `unit_code` values (e.g., `VND/liter` vs `VND/kg`) unless `unit_code` is included in `group_cols`.

## 4. Edge Cases and Missing Data

### 4.1 Missing BOG Labels

- Preserve null labels.
- Never convert null into NO_ACTION.
- Exclude unverified labels from BOG metric calculations.

### 4.2 Zero Valid Labels

If a fold contains zero valid BOG labels:
- Set status to `not_evaluable`.
- Retain the fold in the evaluation manifest.
- Exclude it from BOG metric aggregation.

### 4.3 Missing Classes

- Keep all three classes in the confusion matrix.
- Report support = 0 for absent classes.
- Undefined class-level precision, recall, and F1 must remain null.
- Mark three-class macro F1 as `undefined_single_class` when fewer than two classes appear.

### 4.4 Undefined Metrics

Undefined metrics must be reported as null or an explicit status, not silently converted to zero.

## 5. Aggregation and Reporting Rules

All models must use identical:
- Validation folds.
- Forecast origins.
- Comparable observation sets.

Primary validation:
- Expanding window.
- Four chronological validation folds.

Supplementary validation:
- Rolling window.
- Fixed 730-day training lookback.

Required reports:

1. Per-fold metrics.
2. Mean and standard deviation across evaluable folds.
3. Pooled validation metrics computed from concatenated valid predictions and targets.
4. Per-product metric breakdown.

Validation and final test results must be reported separately.

The final test remains locked and must not be used for model selection or hyperparameter tuning.

## 6. Report Templates

### 6.1 Regression Report

| Model | Product | Fold | MAE | RMSE | MAPE (%) | Directional Accuracy (%) |
|---|---|---|---:|---:|---:|---:|
| Naive | Product A | Fold 1 | — | — | — | — |
| Seasonal Naive | Product A | Fold 1 | — | — | — | — |
| SARIMAX | Product A | Fold 1 | — | — | — | — |
| XGBoost | Product A | Fold 1 | — | — | — | — |

### 6.2 BOG Report

| Fold | Valid Labels | Accuracy | Precision Macro | Recall Macro | F1 Macro | Brier | Log-loss | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Expanding Fold 1 | 0 | — | — | — | — | — | — | not_evaluable |
| Expanding Fold 2 | 5 | — | — | — | — | — | — | provisional |
| Expanding Fold 3 | 130 | — | — | — | — | — | — | provisional |
| Expanding Fold 4 | 140 | — | — | — | — | — | — | provisional |

The table is a reporting template. Dashes indicate metrics not yet calculated.

## 7. Limitations and Evaluation Status

- BOG annotations contain unverified and missing labels.
- CONTRIBUTE has no verified annotations in the current validation and test scopes.
- BOG evaluation remains provisional.
- Brier Score and Log-loss cannot be computed until probability outputs are available.
- Regression evaluation must respect point-in-time availability and leakage-prevention rules from T21.
- Reference implementation: `src/petrotwin/evaluation/metrics.py` (`compute_regression_metrics`, `compute_bog_metrics`, `aggregate_regression_metrics`, `evaluate_baselines_on_t21`), `src/petrotwin/evaluation/splits.py`, and unit tests in `tests/unit/test_metrics_t24.py`.

## 8. References

- `configs/validation.yaml`
- `docs/T21_Time_Series_Validation_Strategy.md`
- `docs/feature_dictionary.md`
- `docs/baseline_specification.md`