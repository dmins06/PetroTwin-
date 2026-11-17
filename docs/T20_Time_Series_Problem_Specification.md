# T20 – Time Series Problem Specification

**Document Scope:** M3 Problem Specification (`docs/T20_Time_Series_Problem_Specification.md`)

**Data Foundation Baseline:** `petrotwin-data-v3` (T14 on `main`, `configs/collectors.yaml`, `docs/DATA_DICTIONARY.md`, `docs/DATA_INTERFACE.md`, `docs/M2_DATABASE_GUIDE.md`)

**Status Tagging Convention:**
- **`[Implemented]`**: Verified in the current T14 codebase, database schema, or raw/processed datasets.
- **`[Proposed]`**: Target design for M3–M5 modeling, inference, evaluation, and XAI stages (not yet implemented).
- **`[Assumption]`**: Explicit working premise requiring verification before production modeling.
- **`[Unverified]`**: External policy or pricing behavior lacking authoritative quantitative evidence in T14.
- **`[Out of Scope]`**: Explicitly excluded from T20.

---

## 1. Problem Definition and Forecast Target

PetroTwin aims to forecast Vietnamese domestic retail fuel prices based on historical official retail adjustment events, international crude oil benchmarks, exchange rates, and verified stabilization-fund (BOG) records.

### 1.1 Target Variable and Product Scope

- **Target variable (`[Implemented]`):** `price_value` in `fuel_market_panel.csv` / `v_current_fuel_market_panel` (`DECIMAL(18,4)`, strictly positive, non-NULL).
- **Unit (`[Implemented]`):** `VND/liter` (`unit_code = "VND/liter"`).
- **Granularity (`[Implemented]`):** Per `product_id` and per official adjustment instant (`effective_at_local` in `Asia/Ho_Chi_Minh`, `+07:00`).
- **Modeling Strategy (`[Proposed]`):** Models and forecasts are produced per `product_id` (either separate per-product models or a global model conditioned on `product_id`), without cross-grade price substitution or imputation for products lacking valid target observations.

Supported `product_id` values in the T14 canonical contract (`docs/DATA_INTERFACE.md`, `src/petrotwin/data/pipeline.py`) **`[Implemented]`**:

| `product_id` | Source Label | T14 Coverage Status |
|---|---|---|
| `gasoline_e10_ron95_iii` | `Xăng E10 RON 95-III` | Active in COMECO & historical PVOIL |
| `gasoline_e5_ron92_ii` | `Xăng E5 RON 92-II` | Active in COMECO & historical PVOIL |
| `diesel_0001s_v` | `Dầu DO 0,001S-V` | Active in COMECO & historical PVOIL |
| `diesel_005s_ii` | `Dầu DO 0,05S-II` | Active in COMECO & historical PVOIL |
| `gasoline_ron95_iii` | `Xăng RON 95-III` | Historical PVOIL only |
| `gasoline_ron95_iv` | `Xăng RON 95-IV` | Historical PVOIL only |
| `diesel_0005s_iv` | `Dầu DO 0,005S-IV` | Historical PVOIL only |
| `kerosene_ko` | `Dầu KO` | Historical PVOIL only |
| `fuel_oil_35s` | `Dầu FO 3,5%S` | Historical PVOIL (positive rows only; zero-price rows quarantined) |
| `fuel_oil_3s` | `Dầu FO 3%S` | **0 valid target rows** (all source rows are `0` and quarantined) |

### 1.2 Price State Distinctions

Consumers and downstream models must strictly distinguish three price concepts **`[Proposed]`**:

1. **Official Adjustment Price (`price_value` at event `t`):** The retail price announced by the domestic fuel source (`comeco` or historical `pvoil`) at an official adjustment instant `effective_at_local`.
2. **Effective Retail Price Between Cycles:** The latest official adjustment price carried forward in calendar time for display/monitoring between two adjustment events. Carry-forward series must **never** be used as event-level training targets **`[Implemented]`**.
3. **Forecasted Next-Cycle Price (`predicted_price`):** The model estimate generated at a `forecast_origin` for the next official adjustment event (`target_event`), accompanied by BOG scenario outputs **`[Proposed]`**.

---

## 2. Data Frequency and Forecast Trigger Policy

### 2.1 Three Distinct Frequency Concepts

1. **Event Frequency (`[Implemented]`):** Domestic retail fuel prices follow an **irregular, event-based** cadence (`fuel_market_panel.csv` has one row per `(product_id, effective_at_local, target_snapshot_id)`). Daily expansion is prohibited during cleaning.
2. **Input Observation Frequency (`[Implemented]`):**
   - **Brent (`brent_dated_usd_bbl`, `eia_fred`):** Daily on market trading days (`DCOILBRENTEU`); non-trading/holiday markers `.` are preserved as `observation_status = "missing"` with `value = NULL`.
   - **USD/VND (`usd_vnd_sbv_reference`, `algolab_sbv_central_rate`):** Daily on SBV working days (plus occasional Saturday observations in historical data); Vietnamese public holidays are absent rather than interpolated.
3. **Forecast Generation Frequency (`[Proposed]`):** **Continuous / event-driven** within each inter-adjustment cycle whenever new valid market data arrives—distinct from the scheduler's polling frequency.

### 2.2 Forecast Trigger Rules (`[Proposed]`)

- **Scheduled Check vs. Forecast Trigger:** A periodic scheduler (e.g., GitHub Actions cron or `petrotwin.collectors.runner`) polls upstream sources, but **scheduler execution alone must NOT trigger a new forecast** if no valid market input has changed.
- **Trigger Conditions:**
  - **New valid Brent observation:** Update feature snapshot and generate a new forecast version.
  - **New valid USD/VND observation:** Update feature snapshot and generate a new forecast version.
  - **Both Brent and USD/VND updated in the same runner cycle:** Combine into a **single** updated feature snapshot (`feature_snapshot_id`) and emit one new forecast version (preventing redundant duplicate forecasts).
- **Non-Trigger / Rejection Conditions:**
  - **Exact duplicate data:** Identical `(feature_snapshot_id, model_version)` must not create a new forecast version.
  - **Invalid or quarantined data:** Rows failing validation (e.g., negative/zero prices, conflicting duplicates) are quarantined **`[Implemented]`** and must not trigger forecasts **`[Proposed]`**.
  - **Stale data:** Observations exceeding configured freshness thresholds (e.g., `fx.max_age_days = 7` in `configs/collectors.yaml` **`[Implemented]`**) must be flagged as stale (`fx_age_days` / `brent_age_days`) and must not be silently treated as fresh triggers **`[Proposed]`**.
  - **Source revisions / late-arriving historical data:** If an upstream source revises a historical value or backfills a missed past date (e.g., `brent.py --backfill` or `fx.py` catch-up **`[Implemented]`**), only the **current open Forecast Cycle** may emit a new forward forecast version; past closed Forecast Cycles must **never** be retroactively re-forecasted or overwritten **`[Proposed]`**.

---

## 3. Forecast Origin and Temporal Alignment (Anti-Leakage Policy)

### 3.1 Core Anti-Leakage Invariant

**Forecast Origin (`forecast_origin`)** is the exact UTC timestamp at which the forecasting system constructs the feature snapshot and executes inference **`[Proposed]`**.

Every input feature used at `forecast_origin` must satisfy:

`feature_available_at <= forecast_origin`

A feature with an earlier `observation_date` that was only published or ingested **after** `forecast_origin` is strictly ineligible for that forecast **`[Proposed]`**.

### 3.2 Required Timestamp Semantics

| Timestamp | Meaning | Source vs. System | T14 Status |
|---|---|---|---|
| `observation_date` | Calendar date to which the market observation applies (`YYYY-MM-DD`) | Upstream source | **`[Implemented]`** in `market_observation` |
| `published_at` | Exact timestamp when the upstream publisher released the observation | Upstream source | **`[Gap]`** Not provided by FRED CSV or Algolab JSON-LD/UDF; must **not** be fabricated from `retrieved_at_utc` |
| `retrieved_at` (`retrieved_at_utc`) | UTC timestamp when PetroTwin collected the observation | PetroTwin collector | **`[Implemented]`** (`extracted_at_utc` → `retrieved_at_utc`) |
| `available_at` (`feature_available_at`) | Earliest timestamp at which the observation is verified and usable by the feature store | Derived conservatively | **`[Proposed]`** (Gap in T14; requires explicit design in M3) |
| `forecast_origin` | UTC timestamp when a forecast version is generated | Forecasting service | **`[Proposed]`** (Not yet in T14 schema) |

### 3.3 Feature Selection & Alignment Rules

1. **Historical Training / Backtest Alignment (`[Implemented]` in T14):**
   - For a fuel adjustment event on local calendar date `D` (`effective_at_local` in `Asia/Ho_Chi_Minh`), `src/petrotwin/data/pipeline.py` (`_integrate_fuel_market`) selects the latest `observed` record per series with `observation_date < D` via `bisect_left`, preserving `brent_observation_date`, `brent_age_days`, `brent_snapshot_id`, `fx_observation_date`, `fx_age_days`, and `fx_snapshot_id`.
   - Same-day (`observation_date == D`) and future (`observation_date > D`) market observations are rejected by `_validate_integrated_outputs` and `_assess_event_fx_freshness` **`[Implemented]`**.
2. **Live Continuous Inference Alignment (`[Proposed]`):**
   - Select the latest `observed` Brent observation and latest `observed` USD/VND observation satisfying `feature_available_at <= forecast_origin`.
   - When Brent and USD/VND update on different dates (e.g., FRED publication lag vs. Algolab daily SBV rate), each series is joined **independently** to its own latest available observation; never drop or forward-fill one series to match the other's date.
   - **Missing features:** If no prior valid observation exists, keep feature values as `NULL` rather than imputing **`[Implemented]`**.
   - **Stale features:** Record `brent_age_days` and `fx_age_days` relative to `forecast_origin` (or `event_date` in historical training). In T14, `_assess_event_fx_freshness()` checks event-level `fx_age_days > fx.max_age_days` (`7`) in `quality_report.json` **`[Implemented]`**.
3. **T14 Schema Gap for Point-in-Time Availability (`[Gap]`):**
   - Because T14 sources do not provide `published_at`, and `retrieved_at_utc` reflects collection time (including later backfills), T14's `observation_date < D` rule serves as a conservative calendar-day guard against same-day/future leakage in historical panels, but **cannot reconstruct exact historical sub-day availability**. Live M3 inference must persist `available_at` (ingestion-completion timestamp) and `forecast_origin` at inference time so live forecasts never depend on retroactive reconstruction **`[Proposed]`**.

---

## 4. Forecast Horizon and Forecast Cycle

### 4.1 Definitions (`[Proposed]`)

- **Forecast Origin (`forecast_origin`):** Timestamp when a forecast version is produced.
- **Forecast Target Event (`target_event`):** The next official domestic fuel price adjustment event after the current effective cycle.
- **Forecast Horizon (`horizon`):** The variable time span between `forecast_origin` and `target_event.effective_at_local`. **A fixed 7-day horizon must NOT be assumed**, as adjustment timing can shift around holidays or extraordinary regulatory actions **`[Implemented]`**.
- **Forecast Cycle (`forecast_cycle`):** The open interval starting immediately after one official adjustment event (`cycle_start_event`) and ending when the next official adjustment event (`target_event`) is published.

### 4.2 Target Event Representation Before Official Announcement (`[Proposed]`)

- While a Forecast Cycle is open and the exact next adjustment timestamp has not been officially announced, `target_event_id` must be represented as **`pending` / `tentative`** (linked to `forecast_cycle_id`), **never** by fabricating a future official adjustment date as ground-truth data.
- Multiple `forecast_origin` runs may occur within the same `forecast_cycle` as new Brent or USD/VND observations arrive.

### 4.3 Multi-Version Lifecycle Within a Cycle (`[Proposed]`)

- Every triggered forecast appends a new `forecast_version` (`1, 2, ...`) inside the active `forecast_cycle`.
- The latest version is flagged (`is_latest_in_cycle = true`) for operational display, while **all prior versions remain immutable** for audit.
- Once the official adjustment event is published, all forecast versions belonging to that `forecast_cycle` are bound to the realized `target_event_id` for evaluation, explicitly distinguishing:
  - **Final pre-announcement forecast** (the last version generated before `target_event.effective_at_local`), and
  - **Earlier intra-cycle forecasts** (for tracking error convergence as market data accumulates).

---

## 5. BOG Scenario Estimator

### 5.1 Three-Class Action Target (`[Proposed]` target; `[Implemented]` partial labels)

The BOG Action Classification task predicts the regulatory Price Stabilization Fund action at the next adjustment event:

| Label | Class | Meaning |
|---|---|---|
| `0` | `NO_ACTION` | No BOG spending or contribution |
| `1` | `SPEND` | Draw/spend from BOG to cushion retail price increases |
| `2` | `CONTRIBUTE` | Contribute to BOG (extract into fund) |

- **Strict Label Integrity:** Unknown, unverified, or ambiguous BOG records must remain **`NULL`** and must **never** be defaulted to `0` (`NO_ACTION`).
- **T14 Label Gap (`[Gap]`):** Verified in `data/manual/stabilization_fund_annotations.csv` (`565` rows, `2019-01-16` to `2026-10-22`): T14 currently records only `amount >= 0` (`435` rows with `amount = 0` → `stabilization_fund_subsidy = 0`; `130` rows with `amount > 0` → `stabilization_fund_subsidy = 1`), enforced by `CHECK (fund_amount >= 0)` in `001_initial_schema.sql`. T14 does **not** yet distinguish `NO_ACTION (0)` from `CONTRIBUTE (2)`. Supporting 3-class BOG training requires extending manual annotations with verified `CONTRIBUTE` records in a future schema/data iteration.

### 5.2 Input Signals & Non-Determinism (`[Proposed]`)

- Only features available at `forecast_origin` may be used: recent Brent level and multi-day changes, USD/VND level and changes, prior domestic retail prices and inter-cycle changes, days elapsed since last adjustment, and verified historical BOG state.
- Any derived feature not yet in `fuel_market_panel.csv` (e.g., rolling Brent volatility, Singapore MOPS finished-product benchmarks) must be explicitly marked as a **proposed feature** to be engineered in M3.
- **Non-deterministic policy rule:** Abnormal market price surges are **signals** to evaluate BOG activation probability—they must **never** be treated as deterministic proof that BOG will be used.

### 5.3 Two-Tier Output Structure (`[Proposed]`)

1. **Tier 1 — BOG Action Prediction:**
   - Calibrated probabilities `[P(NO_ACTION), P(SPEND), P(CONTRIBUTE)]` summing to `1.0`, plus confidence/uncertainty metadata.
2. **Tier 2 — Retail Price Scenarios:**
   - **Scenario 1 (No BOG):** Forecasted `price_value` per `product_id` assuming no BOG intervention.
   - **Scenario 2 (With BOG):** Forecasted `price_value` per `product_id` under assumed BOG intervention, plus the explicit price difference (`Scenario 2 - Scenario 1`) when quantitative basis exists.
- **Scenario Epistemic Status (`[Unverified]`):** Because the exact regulatory pre-BOG base-price formula and BOG magnitude rule are **not verified** in T14 data (and `price_value` in `fuel_market_panel.csv` is the post-adjustment retail price, not an unsubsidized pre-BOG base price), Tier 2 outputs are defined as **scenario analysis under explicitly disclosed assumptions**, never as official prices or guaranteed regulatory outcomes.

---

## 6. Forecasting Architecture

### 6.1 End-to-End Logical Pipeline

| Step | Stage | T14 / M3 Status |
|---|---|---|
| 1 | Collect Brent (`eia_fred`), USD/VND (`algolab_sbv_central_rate`), and COMECO retail prices (`comeco`) | **`[Implemented]`** (`brent.py`, `fx.py`, `comeco.py`, `runner.py`) |
| 2 | Validate schemas, units, freshness, duplicates, and quarantine invalid rows | **`[Implemented]`** (`pipeline.py`, `mysql_loader.py`) |
| 3 | Construct feature snapshot at `forecast_origin` enforcing `feature_available_at <= forecast_origin` | **`[Implemented]`** (event-level `pipeline.py`) / **`[Proposed]`** (live `forecast_origin` store) |
| 4 | Resolve active `forecast_cycle` and tentative/realized `target_event` | **`[Proposed]`** |
| 5 | Predict market-based / no-BOG price per `product_id` | **`[Proposed]`** |
| 6 | Estimate 3-class BOG action probabilities (`NO_ACTION`, `SPEND`, `CONTRIBUTE`) | **`[Proposed]`** |
| 7 | Compute With-BOG vs. No-BOG retail price scenarios and scenario delta | **`[Proposed]`** |
| 8 | Persist immutable `forecast_version` and metadata | **`[Proposed]`** |
| 9 | Render input trajectories, intra-cycle forecast evolution, and scenario charts | **`[Proposed]`** |
| 10 | Reconcile forecasts against official announced prices/BOG and compute error metrics | **`[Proposed]`** |

No forecasting or BOG classification model is implemented in T14; Steps 4–10 define the M3+ implementation contract.

---

## 7. Update Policy and Forecast Versioning (Logical Data Contract)

### 7.1 Logical Forecast Record Contract (`[Proposed]`)

This is a **logical contract** for M3+ storage; it does **not** alter the T14 database schema (`001_initial_schema.sql`).

| Field | Type / Format | Description |
|---|---|---|
| `forecast_id` | UUID / hash string | Unique identifier of one product-level forecast record |
| `forecast_origin` | UTC ISO-8601 timestamp | Exact time inference was executed |
| `forecast_cycle_id` | string | Identifier of the open cycle (`cycle_start_event` → next adjustment) |
| `target_event_id` | string, nullable | Realized event key once announced, or `NULL` with `target_status = "pending"` |
| `product_id` | string | Controlled `product_id` from T14 |
| `model_version` | string | Immutable identifier of the trained price + BOG model artifacts |
| `feature_snapshot_id` | SHA-256 / string | Deterministic fingerprint of exact input values and source `snapshot_id`s |
| `forecast_version` | integer | Monotonic version (`1, 2, ...`) within `(forecast_cycle_id, product_id)` |
| `predicted_price` | DECIMAL | Baseline / No-BOG predicted price (`VND/liter`) |
| `bog_action_probabilities` | JSON | `{"NO_ACTION": p0, "SPEND": p1, "CONTRIBUTE": p2}` |
| `scenario_outputs` | JSON | Explicit `no_bog_price`, `with_bog_price`, `scenario_delta`, and disclosed BOG assumption |
| `created_at` | UTC ISO-8601 timestamp | Record persistence timestamp |

### 7.2 Versioning vs. Controlled Retraining (`[Proposed]`)

- **Continuous Inference (Append-Only):** New valid market inputs create a new `forecast_version`; prior forecast versions are **never** overwritten. Identical `(feature_snapshot_id, model_version)` within the same cycle is deduplicated.
- **Controlled Model Retraining:** Model retraining is **decoupled** from continuous inference. Models are **not** blindly retrained on every incoming Brent/FX tick; retraining occurs on a controlled schedule or when accumulated evaluation metrics indicate drift/degradation, gated by chronological validation and champion-vs-challenger comparison before promoting a new `model_version`.

---

## 8. Evaluation and Feedback Loop (`[Proposed]`)

### 8.1 Post-Announcement Reconciliation

Evaluation is permitted **only after** the official adjustment event is published and ingested:
1. Join all `forecast_version` records in the closed `forecast_cycle` to the realized `(product_id, effective_at_local)` event.
2. Persist `actual_price` and verified `actual_bog_action` (or `NULL` if unverified).
3. Never use future actuals to retroactively alter historical forecasts or features.

### 8.2 Evaluation Metrics

- **Retail Price Forecasting (per `product_id`):**
  - Residual (`predicted_price - actual_price`), Absolute Error (`|residual|`), **MAE**, **RMSE**, and **MAPE**.
  - Stratified by `forecast_origin`, position within `forecast_cycle` (e.g., final pre-announcement forecast vs. earlier intra-cycle versions), and `model_version`.
  - Scenario comparison (No-BOG vs. With-BOG) evaluated against actuals conditioned on verified BOG outcome.
- **BOG Classification:**
  - **Accuracy**, **Precision**, **Recall**, **Macro F1-score**, and **Confusion Matrix**.
  - **Probability Calibration & Brier Score** (since calibrated BOG probabilities are a primary decision-support output).

### 8.3 Feedback Loop & Retraining Governance

- All evaluation metrics and reconciliation records are persisted over time to monitor model drift.
- Retraining uses chronological (expanding-window or rolling-origin) validation and must outperform or meet acceptance gates against the active champion `model_version` before promotion.

---

## 9. Explainable AI (XAI) Specification (`[Proposed]`)

Every forecast output must be accompanied by human-readable and machine-auditable explanations covering:

1. **Global Feature Importance:** Overall feature rankings across a `model_version` (training/validation summary).
2. **Local Per-Forecast Explanation:** At a specific `(forecast_origin, product_id)`:
   - Top contributing features to `predicted_price`.
   - Direction and magnitude of **Brent** price changes.
   - Direction and magnitude of **USD/VND** exchange-rate movements.
   - Contribution of **domestic price history** and prior adjustment cycles.
   - Factors increasing or decreasing `P(SPEND)` and `P(CONTRIBUTE)` in the BOG estimator.
3. **Scenario Explanation:** Explicit explanation of why Scenario 1 (No BOG) and Scenario 2 (With BOG) differ and the exact numeric price difference (`scenario_delta`).
4. **Methodology & Uncertainty Limits:**
   - **SHAP** (e.g., TreeSHAP / KernelSHAP depending on model family) is recommended for local additive feature attributions once models are trained.
   - **Causal disclaimer:** SHAP and feature attributions measure statistical model contribution, **not** causal economic or regulatory proof. Explanations must surface feature staleness (`brent_age_days`, `fx_age_days`) and missingness when present.

---

## 10. Assumptions, Risks, Limitations, and T14 Gaps

### 10.1 Verified T14 Baseline & Documentation Discrepancies (`[Implemented]`)

- **Active raw files on `main` (`afa272d`):** `data/raw/brent_crude_prices.csv` (`2,146` rows), `data/raw/usd_vnd_rates.csv` (`2,417` rows), `data/raw/data_price_petro.csv` (`1,462` rows, including `64` zero-price FO rows quarantined), `data/raw/comeco_fuel_prices.csv` (`4` rows for `2026-11-12`), and `data/manual/stabilization_fund_annotations.csv` (`565` rows, `2019-01-16` to `2026-10-22`).
- **Observed T14 Doc-vs-Code Discrepancies (recorded without modifying T14):**
  - `reports/T14-update-runbook.md` states `comeco.py` writes to `data/raw/data_price_petro.csv`, whereas verified code (`src/petrotwin/collectors/comeco.py`, `src/petrotwin/data/pipeline.py`) writes COMECO rows to `data/raw/comeco_fuel_prices.csv` and preserves `data/raw/data_price_petro.csv` for PVOIL.
  - `docs/DATA_INTERFACE.md` cites the earlier `2026-10-23` snapshot (`28b22922e761cb08a8d8`) counts, while `main` now holds backfilled Brent/FX/PVOIL and COMECO raw snapshots.

### 10.2 Key Risks and Unresolved Gaps

| Category | Risk / Limitation | Impact / Mitigation Requirement |
|---|---|---|
| Late market data | Brent (EIA/FRED publication lag) or FX updates arrive late | Track `observation_date` vs. `retrieved_at_utc`; never use uncollected/future data |
| Missing / stale features | Missing observations at `forecast_origin` or `fx_age_days > max_age_days` | Preserve `NULL`, surface staleness flags, avoid silent imputation |
| Availability timestamp gap | T14 lacks `published_at` and `available_at` columns | Live M3 inference must persist `available_at` and `forecast_origin` |
| Policy / regulatory shifts | Adjustment cadence or pricing rules can change without warning | Avoid fixed 7-day horizon; monitor error drift |
| Incomplete BOG labels | T14 `stabilization_fund_annotations.csv` lacks `CONTRIBUTE (2)` labels and post-`2026-10-22` coverage | Keep unverified BOG labels `NULL`; extend annotations before 3-class training |
| Unverified pre-BOG pricing | Exact quantitative pre-BOG formula is `[Unverified]` | Treat Tier 2 BOG prices as scenario analysis with disclosed assumptions |
| Variable horizon | Horizon varies across cycles | Group evaluations by cycle position and realized horizon |
| Model degradation | Market regime shifts degrade accuracy | Controlled retraining with chronological validation & champion-challenger gating |
| Leakage risk | Sub-day or post-origin backfill leakage in training sets | Enforce `feature_available_at <= forecast_origin` and `observation_date < event_date` |

---

## 11. Deliverables and Acceptance Criteria

| # | Deliverable | Verifiable Acceptance Criteria |
|---|---|---|
| 1 | **Target definition** | `price_value` (`VND/liter`) per `product_id` defined; official, carried-forward, and forecasted prices distinguished; no synthetic grade substitution. |
| 2 | **Event-based temporal granularity** | Event frequency, market observation frequency, and forecast generation frequency defined separately; carry-forward prohibited in event training. |
| 3 | **Forecast Origin** | `forecast_origin` defined; `observation_date`, `published_at`, `retrieved_at`, and `available_at` distinguished; T14 timestamp gap documented. |
| 4 | **Forecast trigger policy** | Event-driven triggers on valid Brent/FX/both updates defined; duplicate, invalid, stale, and late-revision handling specified without scheduler-only triggers. |
| 5 | **Forecast horizon & Forecast Cycle** | Next-event variable horizon (no fixed 7-day assumption) and open/closed `forecast_cycle` lifecycle defined with tentative target representation. |
| 6 | **Temporal alignment & anti-leakage policy** | `feature_available_at <= forecast_origin` and T14 `observation_date < D` documented; missing/stale feature rules and T14 gaps explicitly recorded. |
| 7 | **BOG action classification** | 3-class target (`0=NO_ACTION`, `1=SPEND`, `2=CONTRIBUTE`) defined; `NULL` preserved for unverified cases; non-deterministic surge signals and T14 `CONTRIBUTE` gap documented. |
| 8 | **BOG price scenarios** | Two-tier output separating BOG probabilities from No-BOG / With-BOG retail price scenarios; unverified pre-BOG formula marked `[Unverified]`. |
| 9 | **Continuous inference & forecast versioning** | Logical 12-field forecast contract defined; append-only versioning, deduplication, and separation from controlled retraining specified. |
| 10 | **Evaluation & feedback loop** | Post-announcement matching, price metrics (MAE, RMSE, MAPE), BOG metrics (Accuracy, Precision, Recall, Macro F1, Confusion Matrix, Calibration/Brier), and chronological retraining gates specified. |
| 11 | **XAI specification** | Global, local, and scenario explanations (Brent, USD/VND, domestic history, BOG drivers, scenario delta) defined with SHAP proposal and non-causal disclaimer. |
| 12 | **Assumptions, limitations & unresolved gaps** | Implemented T14 vs. Proposed M3+ vs. Unverified/Gap clearly tagged throughout with zero fabricated numbers or unverified pricing claims. |
