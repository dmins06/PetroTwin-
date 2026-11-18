# T21 — Time-Series Validation Strategy

**Project:** PetroTwin  
**Workstream:** M3 — Time Series and Feature Engineering  
**Reference branch:** `feature/comeco-collector`  
**Depends on:** `docs/T20_Time_Series_Problem_Specification.md`  
**Data contract:** `petrotwin-data-v3`  
**Status:** Validation design. The procedures below describe how experiments must be evaluated; they are not claims that a forecasting model or validation runner is already implemented.

## 1. Objective

T21 defines a reproducible, time-aware validation procedure for the next-event domestic fuel-price forecasting problem in T20.

The validation must:
1. preserve the irregular event-based structure of domestic fuel prices;
2. prevent future information from entering training or features at an earlier forecast origin;
3. compare every candidate against the last-observed-price baseline on the same eligible cases;
4. keep model selection separate from final chronological testing;
5. evaluate each product without silently mixing fuel grades;
6. treat BOG labels and scenario outputs according to their actual evidence and limitations.

T21 is a modeling/evaluation specification. It does not implement the data collectors, the canonical data pipeline, model training, a forecast registry, or the production monitoring service.

## 2. Data and validation unit

### 2.1 Canonical input

At the start of each experiment:
1. read `data/processed/current.json`;
2. resolve the referenced immutable run directory;
3. load `fuel_market_panel.csv` for event-level modeling;
4. record `run_id`, `schema_version`, manifest/checksums and experiment configuration;
5. inspect `quality_report.json` and the product-level coverage before constructing folds.

Do not hard-code the current run ID. Do not edit published run files. Quarantined rows are audit-only and must not enter training or evaluation.

Use `fuel_price_observation.csv` and `market_observation.csv` when source-level provenance or a separate point-in-time reconstruction is needed.

### 2.2 Event-based sample

The domestic target sample is one valid price observation per product and effective adjustment event. The interval between events is irregular.

The validation sample must not be expanded to daily frequency. Do not manufacture target rows for days between adjustments.

A fold boundary is defined by event time, not by row position across a mixed-product table alone. All product rows belonging to the same official adjustment event date/instant must be assigned consistently to the same chronological partition. This prevents the same market adjustment from appearing in both train and validation merely because products are stored as separate rows.

### 2.3 Product-specific series

Build folds and report metrics separately by `product_id`. Do not substitute another grade's history.

The live COMECO collector covers four whitelisted products:
- `gasoline_e10_ron95_iii`;
- `gasoline_e5_ron92_ii`;
- `diesel_0001s_v`;
- `diesel_005s_ii`.

Historical records in `data_price_petro.csv` may extend coverage for other products. Product eligibility must be decided from the current canonical snapshot, not inferred from the collector whitelist alone.

Products with sparse event histories must be marked exploratory or excluded from standalone performance claims. Do not silently pool sparse products into a single score.

## 3. Temporal ordering and leakage controls

### 3.1 Chronological order

- Sort events by `effective_at_local` and preserve the timezone semantics.
- Never use random train/test shuffling.
- For a prediction at origin t, training labels must belong strictly to events before the target event being predicted.
- Validation and test targets must not be used to fit preprocessing, feature selection, model parameters or thresholds.

### 3.2 Point-in-time feature rule

For every forecast origin:

`feature_available_at <= forecast_origin`

Use only information actually available by that origin. The current event panel applies the conservative historical rule that Brent and FX observation dates must be strictly earlier than the domestic event's local date. This rule is appropriate for the canonical event panel but does not reconstruct exact historical intraday publication times.

For a realistic live-style backtest, reconstruct the information set from the immutable raw/processed snapshots and their retrieval/availability timestamps. Do not use a later snapshot to revise an earlier simulated forecast.

If exact historical availability cannot be established, state the limitation and use the conservative prior-date rule. Do not fabricate source publication timestamps.

### 3.3 Fold-local preprocessing

Within every fold:
- fit imputers, scalers, encoders, feature selectors and other learned transformations on that fold's training subset only;
- apply the fitted transformations to validation/test data without refitting;
- perform hyperparameter selection using training/validation folds only;
- do not calculate rolling statistics across the train/validation boundary using future observations;
- calculate lags independently within each product series.

Missing values remain missing unless a transformation is explicitly fitted within the training fold and its use is documented. Never replace NULL with zero simply to make a model run.

### 3.4 Same-event grouping

Where multiple product rows share an official effective event, keep the entire event group in one partition. If a fold uses a time boundary, assign rows using the event timestamp boundary, not independent random row assignment.

## 4. Validation design

### 4.1 Primary method: expanding-window / rolling-origin

Use expanding-window evaluation as the default:

1. Select an initial chronological training period.
2. Fit the baseline and candidate model using only that training period.
3. Predict the next eligible event or next predefined block of events.
4. Record the forecast origin, target event, product, data run, feature snapshot and model version.
5. Advance the origin forward in time.
6. Expand the training history only with events that would have become available by the next origin.
7. Repeat until the validation period is exhausted.

A rolling fixed-length training window may be evaluated as a separate experiment if there is a justified reason to test adaptation to changing market regimes. It must not replace the expanding-window baseline without reporting the difference.

### 4.2 Choosing fold sizes

Do not impose a generic 70/15/15 random split. Do not copy fixed cutoff dates from an older dataset snapshot.

Choose the initial training window and validation block size only after inspecting the current run's per-product event counts and date coverage. Record the chosen dates, number of events and reason for every fold.

If a product cannot provide enough observations for a meaningful initial training period and multiple validation targets:
- do not fabricate more rows;
- do not borrow another product's target series;
- report that the product is insufficient for a standalone model comparison;
- consider a pooled/global model only as a separately specified experiment that explicitly includes product identity and evaluates products without leakage.

### 4.3 Final chronological holdout

If the dataset has enough events after model-selection folds, reserve the latest contiguous period as a final holdout.

The final holdout must:
- remain untouched during feature selection, tuning and model selection;
- be evaluated only after the candidate model and all preprocessing decisions are frozen;
- use the same event eligibility and baseline comparison rules;
- be reported separately from cross-validation results.

If the available data do not support a reliable final holdout, state that limitation instead of presenting a tiny holdout as conclusive evidence.

### 4.4 Continuous forecast-vintage backtest

Because T20 requires forecasts to refresh as new market information arrives, a second evaluation view should simulate multiple forecast vintages within an adjustment cycle:

- create a simulated origin after each newly available valid input snapshot;
- construct features using only data available at that origin;
- retain every forecast version;
- when the next official domestic adjustment is observed, link the forecasts in that cycle to the actual event;
- report earlier-cycle forecasts separately from the final pre-announcement forecast.

This evaluates the operational update behavior. It must not be confused with retraining: the model version remains fixed within an experiment unless a separate controlled retraining experiment is being tested.

## 5. Required baseline and candidate comparison

### 5.1 Last-observed-price baseline

For each product, predict the next event price using the latest valid observed price for that product available at the forecast origin:

\[
\hat{y}_{p,t+1}^{\,baseline}=y_{p,t}
\]

This baseline is mandatory. It must use the same training cutoff, forecast origin, target event and eligible evaluation rows as each candidate model.

### 5.2 Candidate models

Candidate algorithms may include statistical and machine-learning regressors (for example, SARIMAX or gradient-boosted trees) when the product sample size and feature availability justify them.

T21 does not select a winner in advance. A more complex model is not accepted merely because it has a lower training error. It must improve on the baseline on chronological validation cases and remain defensible on the final holdout.

### 5.3 Fair comparison

For each product and fold:
- compare models only on the intersection of eligible target events for which every compared method produced a valid prediction;
- report any excluded cases and reasons;
- do not compare model A on one set of events with model B on another and present the scores as directly comparable;
- preserve the baseline result even if it performs better than all candidates.

## 6. Evaluation metrics

### 6.1 Primary price metrics

Report at least:

- **MAE** in VND/liter;
- **RMSE** in VND/liter;
- number of evaluated forecasts;
- product ID;
- fold and evaluation date range;
- baseline and candidate scores on the same cases.

Definitions:

\[
MAE=\frac{1}{n}\sum_{i=1}^{n}|y_i-\hat{y}_i|
\]

\[
RMSE=\sqrt{\frac{1}{n}\sum_{i=1}^{n}(y_i-\hat{y}_i)^2}
\]

MAPE may be reported as a supplementary metric only when its denominator is valid and its interpretation is appropriate. It must not replace MAE/RMSE.

### 6.2 Error analysis

Where sample size permits, inspect:
- error by product;
- error by fold/time period;
- over- versus under-prediction;
- error by position of the forecast vintage within an adjustment cycle;
- error relative to the last-observed-price baseline.

Do not over-interpret small fold differences when the number of evaluated events is low.

### 6.3 Aggregation

The primary results are product-level. Do not average raw errors across products with different price scales without defining the aggregation method and explaining its limitations.

If an aggregate score is required, report the product-level scores alongside it and specify whether the aggregate is macro-averaged, weighted by event count, or otherwise calculated.

## 7. BOG validation

### 7.1 Label integrity

Use only verified annotations for supervised BOG evaluation:
- `1`: confirmed BOG spending;
- `0`: confirmed no spending;
- `NULL`: unknown/unverified.

Do not convert NULL to zero. Exclude unknown labels from classification metrics and report how many observations were excluded.

The current binary label semantics do not establish a three-class `NO_ACTION / SPEND / CONTRIBUTE` target. A three-class evaluation is out of scope until the data foundation contains verified labels for all required classes and the label definition is approved.

### 7.2 Metrics and chronological evaluation

If the verified binary sample is sufficient, use chronological folds and report:
- class counts per fold;
- precision, recall and F1 by class;
- macro-F1;
- balanced accuracy;
- confusion matrix;
- probability-based metrics such as log loss or Brier score only when calibrated probabilities are available and the evaluation sample supports them.

Do not report accuracy alone where classes are imbalanced. Do not tune a decision threshold on the final holdout.

If a fold contains only one class or too few examples, mark the corresponding metrics as unavailable rather than manufacturing a score.

### 7.3 BOG scenario validation

Do not score a with-BOG/no-BOG price scenario as if it were an observed counterfactual. The actual retail price is the realized regulated price, not an observed pair of prices under both policy states.

A scenario delta may be quantitatively evaluated only after the team verifies a defensible base-price formula, amount units, sign convention and relevant policy interactions. Until then, scenario outputs are experimental assumptions, not ground-truth forecast targets.

## 8. Reproducibility and experiment records

Every validation run should persist:

| Item | Required content |
|---|---|
| Data | `run_id`, `schema_version`, manifest/checksums |
| Target | `product_id`, target field, target unit and event definition |
| Fold | train start/end, validation start/end, event counts |
| Origin | forecast-origin timestamp or simulated origin rule |
| Features | feature list, source dates, snapshot IDs, missing/stale handling |
| Preprocessing | transformations and fold-local fit details |
| Model | algorithm, hyperparameters, random seed where applicable, artifact/version |
| Metrics | baseline and candidate metrics on the same eligible cases |
| Exceptions | skipped products/folds/targets and explicit reasons |

The experiment must be reproducible from the pinned data run, code revision and configuration. Do not overwrite earlier experiment outputs.

## 9. Acceptance criteria for model selection

A candidate may be recommended for the next development stage only when:
1. its data and feature construction pass the point-in-time leakage checks;
2. its validation predictions cover the same eligible targets as the baseline;
3. it is evaluated across multiple chronological folds where the sample supports this;
4. its MAE/RMSE and error behavior are compared with the last-observed-price baseline;
5. its final holdout result, if a holdout is feasible, is reported separately and was not used for tuning;
6. product limitations, missingness and fold sizes are disclosed;
7. the result is reproducible from a pinned data run and code/config version.

A candidate that does not outperform the baseline should not be described as a successful improvement. The baseline may remain the preferred model.

## 10. Known limitations and items requiring team confirmation

The following must be checked against the current `feature/comeco-collector` snapshot before fixing concrete fold cutoffs or reporting model scores:

- actual current per-product event counts and date ranges;
- how the pipeline reconciles COMECO current events with historical `data_price_petro.csv` records;
- whether the current raw snapshots contain enough historical COMECO events for a standalone live-source backtest;
- whether exact feature availability timestamps can be reconstructed for historical snapshots;
- the amount of verified BOG labels available for each product and event period;
- whether any product has enough observations for a final chronological holdout.

The COMECO collector's strict four-product whitelist and fail-closed behavior must be reflected in data availability assumptions. A missing product on the source page can cause collection failure; a failed collection must not be interpreted as a zero-price event or as evidence that the product has no price.

## 11. T21 deliverables

The M3 validation handoff should contain:
1. a documented fold-generation procedure;
2. a leakage-check checklist and test cases;
3. baseline and candidate predictions keyed by product, origin and target event;
4. fold-level and product-level metrics;
5. a final holdout report if data volume permits;
6. an experiment manifest sufficient to reproduce the result.

T21 is a validation design, not proof that these deliverables have already been generated. Implementation and execution status must be reported separately.
