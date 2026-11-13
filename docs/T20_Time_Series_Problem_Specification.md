# T20 – Time Series Problem Specification

## 1. Problem Definition

PetroTwin aims to forecast Vietnamese domestic retail fuel prices based on historical fuel price adjustment events and relevant international market factors.

The system focuses on predicting the retail fuel price at the next official adjustment event rather than predicting a new domestic price every calendar day.

## 2. Forecast Target

The main forecasting target is `price_value`, representing the official domestic retail fuel price.

- **Target variable:** `price_value`
- **Unit:** VND/liter
- **Product identification:** `product_id`
- **Data source:** `fuel_market_panel.csv`

The prediction should be performed separately for each supported fuel product.

## 3. Data Frequency

The primary modeling dataset uses an event-based structure:

- One record represents one valid domestic fuel price adjustment event.
- Domestic retail prices remain effective between adjustment events.
- Daily price visualization may carry forward the latest effective price.
- Daily visualization data must not be confused with the original event-level training dataset.

## 4. Forecast Origin

Forecast Origin is the timestamp when the system generates a prediction.

Only information available at or before this timestamp can be used as input features.

Potential input features include:

- Historical Brent crude oil prices.
- USD/VND exchange rates.
- Previous domestic retail fuel prices.
- Historical adjustment information.
- Verified historical BOG information.
- Singapore finished fuel prices, if available in the dataset.

All input features must respect their actual publication or availability timestamps.

## 5. Forecast Horizon

The forecasting horizon is the next official domestic fuel price adjustment event.

The system does not assume a fixed seven-day forecasting horizon because adjustment timing may vary.

## 6. BOG Scenario Estimator

PetroTwin includes a BOG Scenario Estimator to estimate the probability of possible stabilization fund actions.

The classification target contains three classes:

| Label | Class | Description |
|---|---|---|
| 0 | NO_ACTION | No BOG action |
| 1 | SPEND | Spend/use the stabilization fund |
| 2 | CONTRIBUTE | Contribute to the stabilization fund |

The estimator provides class probabilities as decision-support information. It does not claim to reproduce or determine the government's actual policy decision.

Unknown or unverified BOG information must remain NULL. Mixed or ambiguous cases require further review.

## 7. Forecasting Architecture

The proposed workflow is:

1. Collect market information such as Brent and USD/VND.
2. Generate a market-based price forecast.
3. Estimate possible BOG scenarios.
4. Produce the final retail price forecast.
5. Compare predictions with official published prices and BOG information.
6. Evaluate forecasting performance.

The relationship between the market-based price and the final retail price must be defined using a verified pricing mechanism. The official `price_value` must not automatically be treated as a pre-BOG base price.

## 8. Update Policy

When new market observations become available, the system may update input features and generate a new forecast.

Each forecast version should be stored with its Forecast Origin. Previous predictions must not be overwritten.

Model retraining is not required every time new market data arrives.

## 9. Evaluation Policy

The market price forecasting model will be evaluated using:

- MAE
- RMSE
- MAPE

The BOG classification model will be evaluated using:

- Accuracy
- Precision
- Recall
- Macro F1-score
- Confusion Matrix

Evaluation must use chronological validation and test data.

## 10. Assumptions and Limitations

- Domestic retail fuel prices are adjusted at official adjustment events, not necessarily every day.
- Brent and USD/VND movements do not mechanically determine domestic retail prices.
- BOG decisions depend on regulatory and policy considerations.
- BOG labels may be incomplete or unavailable for some historical events.
- Future market movements cannot be known with certainty at Forecast Origin.
- The pricing mechanism and data availability must be verified before combining market forecasts with BOG scenarios.

## 11. Deliverables

- Defined forecasting problem and target variable.
- Defined event-based data frequency.
- Defined Forecast Origin and forecast horizon.
- Defined BOG classification target.
- Proposed forecasting workflow.
- Documented assumptions and limitations.
