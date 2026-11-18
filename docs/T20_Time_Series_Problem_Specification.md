# T20 — Time-Series Problem Specification

**Project:** PetroTwin  
**Workstream:** M3 — Time Series and Feature Engineering  
**Reference branch:** `feature/comeco-collector`  
**Data contract:** `petrotwin-data-v3`  
**Primary modeling input:** `fuel_market_panel.csv` from the validated processed run  
**Status:** Problem specification. Proposed modeling and inference behavior is not represented as already implemented.

## 1. Purpose and scope

PetroTwin estimates the next official Vietnamese domestic retail fuel price for an individual fuel product. The problem combines historical domestic adjustment events with Brent crude-oil observations, USD/VND reference-rate observations, and verified domestic policy annotations where available.

This specification is written against the repository state on `feature/comeco-collector`. That branch contains an automated source-collection path for Brent, USD/VND and COMECO fuel prices, followed by the canonical data pipeline and an optional MySQL publishing step. It does **not** contain an implemented forecasting model, live forecast service, forecast registry, or model retraining workflow. Those are downstream M3–M5 responsibilities.

T20 defines:
- the prediction target and event grain;
- which source data can be used;
- the information cutoff for each forecast;
- forecast refresh and versioning requirements;
- baseline, feature families, BOG outputs and limitations;
- the handoff contract for validation in T21.

T20 does not implement collectors, train a production model, create a database migration, or claim that the proposed forecast lifecycle already exists.

## 2. Data foundation and source roles

### 2.1 Automated collection path observed on the reference branch

The current collector runner invokes the sources in this order:

1. `petrotwin.collectors.brent`
2. `petrotwin.collectors.fx`
3. `petrotwin.collectors.comeco`
4. `petrotwin.data.pipeline`
5. `petrotwin.data.mysql_loader` when database configuration is present.

The runner supports:
- a full run (collectors, then pipeline and loader);
- `--pipeline-only` (skip collection and process existing raw files);
- `--dry-run` (collectors only; skip pipeline and loader).

The COMECO collector is strict: it expects all four configured target products on the pricing page, rejects missing or duplicate target cards, and fails closed when the effective date/time cannot be parsed. It explicitly excludes `Xăng E10 RON 95 Mức 5`; it must not be mapped to E10 RON95-III. It writes to `data/raw/comeco_fuel_prices.csv` and uses an atomic CSV replacement under a file lock.

A scheduled GitHub Actions workflow is present for the data update path. A schedule or collector run is **not** itself a model forecast. T20 requires a future inference service to create a forecast only after a validated input snapshot has changed.

### 2.2 Source roles and units

| Series | Source / raw file | Meaning | Unit |
|---|---|---|---|
| Brent | EIA series distributed by FRED; `data/raw/brent_crude_prices.csv` | International crude-oil benchmark observations | `USD/bbl` |
| USD/VND | Algolab provider declaring the SBV central reference rate; `data/raw/usd_vnd_rates.csv` | Reference exchange-rate observations | `VND/USD` |
| Domestic fuel prices | COMECO current collector; `data/raw/comeco_fuel_prices.csv` | Current retail prices for the four whitelisted COMECO products | `VND/liter` |
| Historical domestic prices | `data/raw/data_price_petro.csv` | Historical fuel-price records retained by the data pipeline | `VND/liter` |
| BOG annotations | `data/manual/stabilization_fund_annotations.csv`, when present | Manually verified policy annotations matched to domestic events | Product-specific amount unit; label is dimensionless |

Source provenance and reuse terms must be retained. The current configuration records reuse terms as unknown for sources where they have not been verified. The USD/VND source is attributed through a secondary provider; do not describe it as a direct SBV API unless that has been independently established.

### 2.3 Canonical data contract

The pipeline uses `petrotwin-data-v3`. Consumers should use the latest validated run through `data/processed/current.json`, then load `fuel_market_panel.csv` from the referenced immutable run directory. Do not hard-code a run ID in modeling code.

The canonical tables have distinct roles:
- `fuel_market_panel.csv`: event-level modeling table;
- `fuel_price_observation.csv`: domestic source-event records and provenance;
- `market_observation.csv`: source-specific Brent and FX observations;
- `manifest.json`: source and output checksums, retrieval metadata and schema information;
- `quality_report.json`: coverage, missingness and quality diagnostics;
- `quarantine.csv`: rejected source rows for audit only.

Blank CSV cells represent NULL. Quarantined rows are excluded from model features and targets. Published run directories are immutable; a correction should produce a new run rather than modifying an existing snapshot.

## 3. Prediction target

### 3.1 Primary target

For product \(p\), predict the next valid official retail price:

- **Target field:** `price_value`
- **Target unit:** `VND/liter`
- **Product key:** `product_id`
- **Event time:** `effective_at_local` in `Asia/Ho_Chi_Minh` (`+07:00`)
- **Forecast task:** estimate the next official adjustment-event price for the same product.

The target is a price **level**, not the source-reported `price_change_value`. A reported change may be used for historical analysis or as a lagged feature only if it was available at the forecast origin. The change belonging to the event being predicted is future information and must not be used.

### 3.2 Event-based frequency

Domestic price observations are irregular adjustment events. They are not a daily time series of independently observed target prices.

Do not:
- expand each domestic event into fabricated daily target rows;
- assume the next adjustment always occurs exactly seven days later;
- treat the carried-forward display price between events as a new observed target;
- invent a target event date before an official event is known.

The forecast horizon is the interval from the forecast origin to the next official adjustment event. Its calendar duration is variable.

### 3.3 Product-level modeling

Every `product_id` is a separate target series. Do not substitute another grade's price or history.

The COMECO collector currently collects only these four live products:

| COMECO source label | Canonical product ID |
|---|---|
| Xăng E10 RON 95 Mức 3 | `gasoline_e10_ron95_iii` |
| Xăng E5 RON 92 Mức 2 | `gasoline_e5_ron92_ii` |
| Dầu DO 0,001S Mức 5 | `diesel_0001s_v` |
| Dầu DO 0,05S Mức 2 | `diesel_005s_ii` |

The collector's whitelist is intentional. In particular, E10 RON 95 Mức 5 is excluded and must never be treated as E10 RON95-III.

The data pipeline also retains historical domestic price records in `data_price_petro.csv` when present. Historical product coverage and current COMECO collection coverage are not interchangeable. Before fitting a model, inspect the selected product's actual event count, source coverage, missingness and date range in the current validated snapshot. Products with too few events must be marked exploratory or excluded from standalone model claims.

## 4. Forecast origin and point-in-time information

### 4.1 Forecast origin

`forecast_origin` is the exact UTC timestamp at which an inference run constructs its feature snapshot and produces a prediction.

For every forecast, the future inference service should persist:
- `forecast_origin` (UTC);
- the selected processed `run_id` and `schema_version`;
- the source/snapshot identifiers used for features;
- the model artifact/version;
- a deterministic feature-snapshot fingerprint;
- the product and forecast-cycle identifiers;
- the prediction and any scenario/probability outputs.

### 4.2 Information cutoff

The core rule is:

`feature_available_at <= forecast_origin`

Only information available to the system by the forecast origin may be used. An observation with an earlier source date but collected or validated after the forecast origin cannot be retroactively inserted into that forecast's feature set.

The current canonical event panel applies a conservative historical join: for a domestic event on local date D, the pipeline selects the latest observed Brent and FX rows with `observation_date < D`. Same-day and future market observations are not eligible. This is a calendar-day leakage guard; it does not reconstruct exact historical intraday publication times.

For future live inference:
- record the ingestion/validation completion time as the system availability time;
- independently select the latest eligible Brent and FX observations;
- retain each selected observation date, snapshot ID and age;
- do not fabricate `published_at` from `retrieved_at_utc`;
- if publication availability cannot be established, use a conservative cutoff rather than assuming the value was known earlier.

### 4.3 Missing and stale inputs

Preserve missing values as NULL and record their provenance. Do not silently forward-fill, interpolate or replace missing values with zero.

The current collector configuration sets `fx.optional: true` and `fx.max_age_days: 7`. The pipeline checks FX freshness for event integration. A future model inference policy must explicitly decide whether a missing or stale feature causes:
- a forecast to be withheld;
- a forecast to be emitted with a warning and a model that supports missing values; or
- a fallback to a separately validated feature set.

That decision must be versioned and visible in the forecast record. It must not silently treat stale or absent observations as fresh.

## 5. Forecast refresh and lifecycle

### 5.1 Proposed refresh triggers

A future forecast service may create a new forecast version when a successfully validated input changes the information set, for example:
- a new Brent observation;
- a new USD/VND observation;
- a new valid domestic price/policy input that is eligible at the forecast origin;
- an accepted corrected snapshot that changes an input used by the active forecast cycle.

If Brent and FX are both updated in one collection cycle, construct one combined feature snapshot and emit one forecast version for that snapshot rather than duplicate forecasts for each source update.

Do not create a new forecast merely because:
- a scheduled workflow ran;
- the collectors returned no new valid data;
- the same feature snapshot and model version were already evaluated;
- a source batch failed validation or was quarantined.

### 5.2 Forecasting is not retraining

A new input snapshot should normally trigger **inference**, not model training. The deployed model version remains fixed until a separate, controlled retraining and evaluation process approves a replacement.

This separation is important for reproducibility: changes in predictions caused by new market information must be distinguishable from changes caused by a new model.

### 5.3 Forecast cycles and immutable versions

A forecast cycle begins after one official domestic adjustment event and ends when the next official event is published. Before the next event is known, its target event is pending; do not invent a target date.

Each eligible input update may append a new forecast vintage within the open cycle. Previous versions must remain immutable. When the actual event is ingested, link the cycle's forecasts to the realized event for evaluation. Keep the final pre-announcement forecast distinguishable from earlier intra-cycle forecasts.

## 6. Feature specification

All feature values must be derived using data available at the forecast origin and must preserve product identity, units and provenance.

### 6.1 Domestic price history

Candidate features:
- last valid price for the same product;
- lagged prices and lagged source-reported changes;
- changes between consecutive observed events;
- time since the previous effective event;
- rolling summaries over previous events, where the product has enough history.

Do not calculate lags or rolling statistics using the target event or later events.

### 6.2 Brent and USD/VND

Candidate features:
- latest eligible Brent level and USD/VND level;
- prior percentage/absolute changes over explicitly defined lookback periods;
- rolling volatility or summary statistics, if justified and computed from past observations only;
- observation dates, snapshot IDs and feature ages.

Keep the units separate: Brent is `USD/bbl`, FX is `VND/USD`, and domestic price is `VND/liter`. Do not add or directly compare raw values across these units.

### 6.3 Policy and BOG information

The domestic retail-price collector does not itself provide authoritative BOG action labels. Use only independently verified annotations, retaining the evidence URL, product/event match, amount and amount unit.

For the existing binary annotation semantics:
- `1`: confirmed BOG spending;
- `0`: confirmed no spending;
- `NULL`: unverified or unavailable.

NULL is unknown, not a negative label. Do not train a supervised BOG classifier by converting NULL to zero.

The existing amount semantics do not establish a complete three-class policy history. In particular, the available annotation design must not be assumed to distinguish `NO_ACTION` from `CONTRIBUTE` unless verified records and a compatible schema are added.

## 7. Baseline and candidate modeling

### 7.1 Required baseline

Use a last-observed-price baseline for each product:

\[
\hat{y}_{p,t+1}=y_{p,t}
\]

where \(y_{p,t}\) is the latest valid observed price for product p available at the forecast origin.

This baseline is appropriate for an irregular event series and must be evaluated on exactly the same forecast origins and target events as candidate models.

### 7.2 Candidate models

M3 may evaluate statistical or machine-learning models (for example, SARIMAX or tree-based regression) only after confirming that the product-specific sample size and features support them. T20 does not mandate a particular algorithm or claim that any candidate is implemented.

All preprocessing, feature selection and parameter fitting must use only the training history available at the relevant origin. T21 defines the validation procedure.

## 8. BOG probability and price scenarios

The system's BOG analysis has two conceptually separate outputs:

1. an estimated probability for a clearly defined, historically supported BOG label;
2. conditional retail-price scenarios, such as with and without a BOG intervention.

Abnormal Brent movement or a large predicted retail-price increase may be a signal for estimating BOG probability. It is not proof that BOG will be used.

The current binary verified label can support, at most, a spending-versus-confirmed-no-spending task when labels are sufficient. It cannot support a reliable three-class `NO_ACTION / SPEND / CONTRIBUTE` classifier without additional verified labels.

The observed domestic retail price is already the published retail price. The current data contract does not provide a verified pre-BOG base-price formula or a complete quantitative rule for the BOG adjustment. Therefore:
- do not calculate a with-BOG price by arbitrarily adding/subtracting the annotation amount;
- do not present a scenario delta as an official policy calculation;
- mark scenario prices unavailable or experimental until the formula, units, sign convention and policy interactions are verified;
- report a BOG probability separately if it can be validated.

## 9. Forecast record contract (proposed)

The following is a logical M3–M5 handoff contract, not a claim that these fields already exist in the current database:

| Field | Meaning |
|---|---|
| `forecast_id` | Unique forecast record ID |
| `forecast_origin` | UTC inference timestamp |
| `forecast_cycle_id` | Active interval between official adjustment events |
| `target_event_id` | Realized event key after announcement; pending before then |
| `product_id` | Canonical fuel product |
| `run_id` / `schema_version` | Validated input snapshot and contract version |
| `feature_snapshot_id` | Fingerprint of the exact feature values and source snapshots |
| `model_version` | Immutable model artifact/version |
| `predicted_price` | Predicted next-event price, VND/liter |
| `bog_probability` / `bog_label_definition` | Probability and precise label semantics, when supported |
| `scenario_outputs` | Conditional scenario outputs and assumptions, when quantitatively supported |
| `status` / `warnings` | Success, withheld, stale-input or other inference status |

Forecast records should be append-only. A new prediction must not overwrite an earlier forecast.

## 10. Evaluation and monitoring handoff

After the official target event has been collected and validated:
- match forecasts to the actual price for the same product and event;
- preserve the forecast origin and input/model versions;
- calculate error only for forecasts with a valid matched actual;
- report metrics by product and forecast vintage/lead position where sample size allows;
- compare candidate models with the last-observed-price baseline on identical cases;
- keep unverified BOG outcomes NULL and exclude them from supervised BOG scoring.

T21 specifies chronological validation and leakage controls. Monitoring of production forecasts and controlled model retraining are downstream implementation tasks.

## 11. Assumptions, gaps and acceptance checks

Before implementation, the team should confirm:
1. which products have enough validated historical events for modeling;
2. whether historical `data_price_petro.csv` records can be combined with current COMECO events under the existing canonical source contract;
3. the operational definition of feature availability for live forecasts;
4. how stale or missing Brent/FX inputs affect forecast eligibility;
5. the supported BOG label definition and whether additional verified labels exist;
6. whether a quantitative BOG scenario formula can be sourced and independently verified.
