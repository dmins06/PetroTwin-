# T21 – Time Series Validation Strategy

## 1. Objective

Define a reliable validation strategy for PetroTwin forecasting models.

The strategy must simulate real-world forecasting conditions, where models learn from historical observations and predict future fuel price adjustment events.

## 2. Validation Approach

Use chronological validation instead of random train-test splitting.

The dataset must be sorted by adjustment date in ascending order before splitting.

Random shuffling is not appropriate because it may allow future observations to appear in the training set while earlier observations are used for testing.

## 3. Initial Data Splitting Strategy

Apply chronological splitting using the following proposed proportions:

| Dataset | Ratio | Purpose |
|---|---:|---|
| Training | 70% | Fit model parameters |
| Validation | 15% | Select models and tune hyperparameters |
| Testing | 15% | Estimate final model performance |

The split boundaries must be determined after inspecting the actual dataset size and chronological coverage.

The Test set must remain isolated until model selection is complete.

## 4. Walk-forward Validation

Use expanding-window validation to evaluate model stability over multiple future periods.

Procedure:

1. Train using the earliest historical observations.
2. Validate on the next chronological block.
3. Expand the training window by adding earlier validation observations.
4. Validate on the next unseen block.
5. Repeat while sufficient historical observations remain.

This approach evaluates whether the model can adapt as new historical data becomes available.

## 5. Leakage Prevention

Apply the following controls:

- Preserve chronological ordering.
- Never shuffle time-series observations.
- Fit scalers, encoders, and preprocessing transformations using training data only.
- Calculate lag and rolling features using past observations only.
- Ensure every feature was available at the corresponding Forecast Origin.
- Exclude the target event's official price and any information published after Forecast Origin.
- Never use Test performance to tune model parameters.

## 6. Validation for BOG Classification

Apply chronological splitting independently to the BOG Estimator dataset.

Before training:

- Exclude unknown and unverified BOG labels from supervised learning.
- Preserve the three classes: NO_ACTION, SPEND, and CONTRIBUTE.
- Examine class distribution in each partition.
- Check whether each class has sufficient observations for meaningful evaluation.
- Document missing classes and class imbalance.

Do not automatically convert missing BOG labels into NO_ACTION.

## 7. Validation Assessment

Evaluate model consistency across Walk-forward folds.

For the forecasting task, record:

- MAE
- RMSE
- MAPE

For the BOG classification task, record:

- Accuracy
- Precision
- Recall
- Macro F1-score
- Confusion Matrix

Compare validation results across folds to identify unstable performance or sensitivity to specific historical periods.

Final metrics must be calculated on the untouched Test set.

## 8. Limitations

- Fuel adjustment events are irregularly spaced in time.
- A limited number of historical events may restrict the number of validation folds.
- BOG labels may be incomplete or imbalanced.
- Performance on historical periods may not fully represent future market conditions.

The final split ratio and number of validation folds must be confirmed after inspecting the actual dataset.

## 9. Deliverables

- Chronological data splitting design.
- Expanding-window Walk-forward validation procedure.
- Data leakage prevention checklist.
- BOG classification validation procedure.
- Validation assessment criteria and documented limitations.
