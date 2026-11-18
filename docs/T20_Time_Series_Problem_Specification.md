# T20 — Time-Series Problem Specification

**Project:** PetroTwin  
**Data contract:** `petrotwin-data-v3`  
**Target table:** `fuel_market_panel.csv`  
**Status:** Modeling specification for M3; assumptions requiring team approval are identified explicitly.

## 1. Problem definition

PetroTwin is an event-based forecasting problem. The system estimates the next officially published domestic retail fuel price for a selected fuel product, using the product's historical domestic prices, international Brent observations, USD/VND observations, and verified domestic price-policy information.

A fuel-price observation represents an effective price-adjustment event. It is not a daily target observation. The modeling dataset must therefore preserve the event grain and irregular intervals between adjustment dates.

The system may generate a new forecast whenever a validated new market observation or other relevant input becomes available. Each generated forecast is a new forecast vintage; it does not overwrite an earlier forecast and does not imply that the model is retrained for every update.

## 2. Forecast target

### 2.1 Primary target

- **Target field:** `price_value`
- **Meaning:** Observed domestic retail price for the specified fuel product at an effective price-adjustment event.
- **Unit:** `VND/liter` for the current supported fuel targets.
- **Prediction task:** Estimate `price_value` at the next valid adjustment event for the same `product_id`.

The primary target is the price level, not `price_change_value`. The source-reported change may be used for descriptive analysis or as a lagged feature only when it was already known at the forecast origin. It must not be used as the target or as a feature derived from the future event being predicted.

### 2.2 Product-specific modeling

Each `product_id` is a distinct target series. Do not combine different grades into one target or substitute one grade's history for another.

The accepted snapshot reports:

| Product | Valid events |
|---|---:|
| `diesel_005s_ii` | 310 |
| `gasoline_e5_ron92_ii` | 310 |
| `gasoline_ron95_iii` | 287 |
| `kerosene_ko` | 269 |
| `diesel_0001s_v` | 119 |
| `gasoline_e10_ron95_iii` | 74 |
| `diesel_0005s_iv` | 10 |
| `gasoline_ron95_iv` | 10 |
| `fuel_oil_35s` | 1 |
| `fuel_oil_3s` | 0 |

Initial experiments should prioritize products with sufficient event history, particularly E5 RON 92-II, DO 0.05S-II, RON 95-III and kerosene. E10 may be explored separately with its limited history and an explicit limitation statement. Products with very sparse or no valid observations should not receive a standalone model-performance claim. The final product inclusion threshold must be agreed with the team and justified by validation sample size.

## 3. Forecast origin and information cutoff

A forecast origin is the exact time at which the system creates and records a prediction.

For every forecast vintage:

1. Record the forecast creation timestamp in UTC.
2. Record the latest successfully validated data run and its `run_id` and `schema_version`.
3. Include only input observations that were available to the system by that forecast origin.
4. Preserve source observation dates, source/snapshot identifiers, and feature ages.
5. Exclude the target event's price, price change, BOG annotation, or any other information published only after the forecast origin.

The accepted v3 panel applies a conservative calendar-date join: for a fuel event on local date D, it selects the latest observed Brent and USD/VND observation whose source date is strictly earlier than D. This avoids same-day look-ahead in the event panel. However, source observation dates alone do not prove the exact intraday publication/availability time. For operational, continuously updated forecasts, use collector retrieval timestamps and immutable snapshots to enforce the actual as-of cutoff. If the exact publication time of a market observation is unknown, do not assume it was available earlier on that same date.

A forecast must never use a later snapshot merely because it contains a revised historical value. Use the snapshot that was available at the recorded forecast origin.

## 4. Forecast horizon and update trigger

### 4.1 Primary horizon

The primary horizon is **the next official price-adjustment event for the same product**.

This is an event horizon, not a fixed number of calendar days. The system should not fabricate daily target rows or assume a fixed seven-day adjustment schedule.

### 4.2 Forecast refresh

Create a new forecast vintage when a new validated input changes the information set, such as:
- a new Brent observation;
- a new USD/VND observation;
- a new domestic price or policy observation that is valid as an input at that time; or
- a corrected/re-published data snapshot accepted by the data-quality pipeline.

The update pipeline may run on a schedule, but it should generate a new forecast only after validation and only when the input snapshot or relevant feature set has changed. A failed collector or failed validation gate must not publish a forecast based on an invalid or partially updated dataset.

Generating a new forecast does not mean retraining the model. The deployed model version may remain unchanged until a controlled retraining and evaluation cycle approves a replacement.

## 5. Input data and feature families

Use the accepted `fuel_market_panel.csv` as the event-level starting point and consult the canonical source tables for provenance and time alignment.

### 5.1 Domestic price history
Potential features, calculated only from events preceding the forecast origin:
- previous valid price for the same product;
- lagged price levels and lagged reported changes;
- changes between consecutive observed events;
- time elapsed since the previous effective price event;
- rolling statistics over prior events, where enough history exists.

Do not calculate rolling or lagged features using the event being predicted or any later event.

### 5.2 International market and exchange-rate data
- Brent: `brent_usd_per_barrel` in `USD/bbl`;
- USD/VND: `usd_vnd_rate` in `VND/USD`;
- selected source observation dates, snapshot IDs, and age-in-days fields.

Potential derived features include prior changes and rolling summaries of Brent and FX. Every derived feature must preserve its as-of cutoff and use only observations available at the forecast origin. Do not combine the raw values as if they shared a unit.

### 5.3 Domestic policy and stabilization-fund information
- `stabilization_fund_subsidy`: verified 1 for confirmed spending, 0 for confirmed no spending, NULL when unverified;
- `stabilization_fund_subsidy_amount` and its unit;
- official evidence URL and the matching product/event identifiers.

A NULL label is unknown, not a negative class. Exclude unverified rows from supervised BOG-label training; do not relabel them as zero. Policy annotations for the event being forecast are not available inputs unless independently published before the forecast origin.

### 5.4 Missingness and exclusions
- Preserve missing market values as NULL and report their availability.
- Do not silently forward-fill, interpolate, or replace missing values with zero.
- Do not use quarantined rows, including the 64 zero-price FO records, as valid targets or features.
- Do not infer E10 history from RON95.
- Keep product-specific units and identifiers intact.

## 6. Baseline forecasting method

The initial baseline is a **last-observation-carried-forward event baseline**:

For product p at forecast origin t, predict the next event price as the latest valid observed `price_value` for product p available at t.

This baseline is intentionally simple, reproducible, and appropriate for an irregular event series. It provides a reference that more complex models must outperform on the same forecast origins and evaluation events.

A secondary baseline may be added if justified (for example, a historical median change applied to the last price), but it must be defined before evaluation and must use only the training window. Do not select a baseline after inspecting the test results.

## 7. Validation design

Use chronological, time-aware validation. Random train/test shuffling is not permitted.

The preferred design is expanding-window or rolling-origin evaluation:
1. Sort valid events chronologically within each product.
2. Select an initial training window containing only earlier events.
3. Fit preprocessing and model parameters using that training window only.
4. Predict the next event or next block of events.
5. Advance the origin and repeat, never allowing future events into the training window.
6. Compare all candidate models on the same eligible forecast origins and target events.

Any imputer, scaler, feature selector, encoding rule learned from data, or hyperparameter tuning must be fitted within the corresponding training window. Keep a final chronological holdout untouched until model selection is complete, if the available number of events supports it.

For the continuous-update use case, validation should also reproduce successive forecast vintages: simulate what the system could have known at each origin, create a forecast from that information set, and compare it with the next official event once it becomes available.

Do not prescribe fixed calendar split dates or a 70/15/15 ratio until the accepted snapshot and per-product event counts have been used to verify that the resulting folds contain enough training and evaluation events.

## 8. Evaluation metrics

Report at minimum:
- **MAE** in VND/liter;
- **RMSE** in VND/liter;
- number of evaluated forecasts;
- product ID and evaluation period;
- baseline score and candidate-model score on the same evaluation events.

Calculate metrics only when a forecast can be matched to a valid actual price for the same product and target event. Do not score forecasts for events that have not yet been officially published.

Report results by product as well as any clearly defined aggregate. Do not pool products with different price scales or units without explicitly justifying the aggregation. Consider reporting error by forecast lead time or forecast vintage where sample size supports it.

## 9. BOG estimation and conditional price scenarios

The current accepted data contract provides a verified event-level label:
- `1`: official evidence confirms stabilization-fund spending;
- `0`: official evidence confirms no spending;
- `NULL`: not researched or not verified.

There are 565 verified labels in the accepted snapshot (130 spending and 435 confirmed no-spending); 825 event labels remain unverified. The supervised BOG estimator must train/evaluate only on verified labels and must preserve the class imbalance and temporal ordering.

The BOG output should be interpreted as an estimated probability of fund spending under the historical label definition, not as certainty about a government decision. Abnormal Brent or retail-price movements may be candidate predictors, but they do not prove that BOG will be used.

The product requirement asks for two conditional scenarios:
1. **No-BOG scenario:** estimated domestic price conditional on no stabilization-fund spending.
2. **BOG-used scenario:** estimated domestic price conditional on stabilization-fund spending.

Do not derive the no-BOG price by simply adding/subtracting the annotated fund amount unless the official pricing mechanism, sign convention, applicable product unit, and tax/policy interactions have been verified. The observed `price_value` is the administered retail price; the current schema does not separately provide a verified pre-BOG market price target. Therefore, scenario-price construction remains conditional on a documented and validated policy formula or a separately trained scenario model with sufficient verified examples.

Until that requirement is met, report the BOG probability separately and label any scenario price as unavailable or experimental rather than presenting an unsupported numerical estimate.

## 10. Forecast record and monitoring requirements

Every forecast vintage should be stored as an immutable record with at least:
- forecast ID;
- creation timestamp (UTC);
- product ID;
- target definition and horizon;
- forecast origin / as-of cutoff;
- input `run_id`, `schema_version`, and relevant snapshot identifiers;
- feature values or a reproducible reference to the input feature snapshot;
- model name, model version, and configuration/version hash;
- predicted price and unit;
- BOG probability and scenario outputs, when available;
- forecast status and any warnings about missing inputs.

When the official price event is published, link the actual price to the corresponding forecast target and calculate the error. Retain both the forecast and actual record; never overwrite the original forecast with the realized value.

Monitoring should display historical inputs, forecast vintages, official actual prices, and error metrics over time. Model retraining should happen in controlled cycles after enough new labeled outcomes have accumulated and evaluation demonstrates a justified improvement.

## 11. Explainability requirements

For each forecast, the system should provide a concise explanation of the model's main contributing features, such as:
- recent domestic price history;
- prior Brent observations and changes;
- prior USD/VND observations and changes;
- time since the previous adjustment;
- verified policy/BOG history where legitimately available.

Use an explainability method appropriate to the final model (for example, SHAP for supported tree-based models). Explanations describe how the model used its inputs; they are not causal proof that Brent, FX, or BOG caused a particular price decision.

For BOG estimation, explain which observed features influenced the estimated probability and state the limitations caused by unverified labels and class imbalance.

## 12. Assumptions, limitations, and decisions to confirm

1. The primary target is the observed administered retail `price_value` for each product, not a hypothetical pre-BOG price.
2. The primary horizon is the next official adjustment event for that same product.
3. The accepted panel is event-based and must not be expanded to daily target observations.
4. Strictly prior market observation dates are the accepted conservative panel join rule; operational as-of enforcement must additionally use actual collection/retrieval availability.
5. The accepted snapshot is a research/model-development dataset, not proof that all sources have verified reuse licenses or production-grade provenance.
6. COMECO's current collector is limited to four products and fails closed if one of those four is absent. The historical accepted snapshot includes additional products from prior source records.
7. BOG annotations are incomplete. The current binary field represents confirmed spending versus confirmed no spending; it does not encode a three-way `NO_ACTION / SPEND / CONTRIBUTE` classification.
8. A numerical no-BOG/BOG price scenario requires a verified policy formula or a defensible separately evaluated model. The current data contract alone does not establish that formula.
9. Product eligibility, minimum training history, validation fold sizes, and the final feature list must be confirmed from the current snapshot before model training.
