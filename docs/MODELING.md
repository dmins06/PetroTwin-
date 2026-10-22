# Modeling and evaluation protocol

- Define the forecast target, fuel grade, forecast origin, and horizon before comparing models.
- Begin with a naive baseline appropriate to the adjustment cadence; use identical forecast origins for every candidate.
- Use expanding-window or rolling-origin validation in chronological order. Do not randomly shuffle time-series observations.
- Fit imputers, scalers, feature selection, and other learned transforms within each training window only.
- Align external predictors by the time they were available, not just the date they describe.
- Report MAE and RMSE with units and sample counts, overall and by fuel grade/horizon where supportable.
- Track model version, configuration, input cutoff, creation timestamp, prediction, and later actual price for each forecast.
- Treat scenario analysis as conditional estimation and SHAP as model explanation, not causal evidence.
