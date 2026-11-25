"""Seasonal naive baseline for fuel price forecasting (T23)."""

from __future__ import annotations

from typing import Any

import pandas as pd


class SeasonalNaiveBaseline:
    """Predict using the observation closest to one seasonal period ago."""

    model_name: str = "seasonal_naive"

    def __init__(
        self,
        product_col: str = "product_id",
        time_col: str = "effective_at_local",
        target_col: str = "price_value",
        origin_time_col: str = "forecast_origin_local",
        seasonal_period: str = "7D",
        tolerance: str = "1D",
        fallback: str = "persistence",
        horizon: int = 1,
        cold_start_policy: str = "raise",
    ) -> None:
        if fallback not in {"persistence", "raise"}:
            raise ValueError("fallback must be either 'persistence' or 'raise'.")
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1, got {horizon}")
        if cold_start_policy not in {"raise", "global_median"}:
            raise ValueError("cold_start_policy must be 'raise' or 'global_median'.")

        self.product_col = product_col
        self.time_col = time_col
        self.target_col = target_col
        self.origin_time_col = origin_time_col
        self.seasonal_period = pd.Timedelta(seasonal_period)
        self.tolerance = pd.Timedelta(tolerance)
        self.fallback = fallback
        self.horizon = int(horizon)
        self.cold_start_policy = cold_start_policy
        self.model_version = f"seasonal-naive-{seasonal_period}-v1"
        self._history: pd.DataFrame | None = None
        self._global_median: float = 0.0

        if self.seasonal_period <= pd.Timedelta(0):
            raise ValueError("seasonal_period must be positive.")
        if self.tolerance < pd.Timedelta(0):
            raise ValueError("tolerance cannot be negative.")

    def fit(self, data: pd.DataFrame) -> SeasonalNaiveBaseline:
        """Store historical observations."""
        required = [self.product_col, self.time_col, self.target_col]
        missing = set(required) - set(data.columns)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")

        history = data[required].copy()
        if history.empty:
            raise ValueError("Training history must not be empty.")

        history[self.time_col] = pd.to_datetime(
            history[self.time_col], utc=True, errors="raise"
        )
        history[self.target_col] = pd.to_numeric(
            history[self.target_col], errors="raise"
        )

        if history.isnull().any().any():
            raise ValueError("Historical data contains missing values.")

        if history.duplicated([self.product_col, self.time_col]).any():
            raise ValueError("Duplicate product and effective timestamp found.")

        self._history = history.sort_values(
            [self.product_col, self.time_col], kind="mergesort"
        ).reset_index(drop=True)
        self._global_median = float(self._history[self.target_col].median())
        return self

    def predict(
        self,
        forecast_origins: pd.DataFrame,
        origin_time_col: str | None = None,
        additional_history: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Generate seasonal naive predictions without using future data."""
        if self._history is None:
            raise RuntimeError("Call fit() before predict().")

        orig_col = origin_time_col or self.origin_time_col
        required = [self.product_col, orig_col]
        missing = set(required) - set(forecast_origins.columns)
        if missing:
            raise ValueError(f"Missing forecast origin columns: {sorted(missing)}")

        result = forecast_origins.copy()
        if result.empty:
            result["seasonal_prediction"] = pd.Series(dtype="float64")
            result["matched_history_at"] = pd.Series(dtype="datetime64[ns, UTC]")
            result["used_fallback"] = pd.Series(dtype="bool")
            result["horizon"] = pd.Series(dtype="int64")
            return result

        result[orig_col] = pd.to_datetime(result[orig_col], utc=True, errors="raise")

        lookup_history = self._history
        if additional_history is not None and not additional_history.empty:
            extra = additional_history[[self.product_col, self.time_col, self.target_col]].copy()
            extra[self.time_col] = pd.to_datetime(extra[self.time_col], utc=True, errors="raise")
            extra[self.target_col] = pd.to_numeric(extra[self.target_col], errors="coerce")
            extra = extra.dropna(subset=[self.target_col])
            lookup_history = (
                pd.concat([self._history, extra], ignore_index=True)
                .drop_duplicates(subset=[self.product_col, self.time_col], keep="last")
                .sort_values([self.product_col, self.time_col], kind="mergesort")
                .reset_index(drop=True)
            )

        predictions: list[float] = []
        matched_times: list[pd.Timestamp] = []
        fallback_flags: list[bool] = []

        for _, origin in result.iterrows():
            product = origin[self.product_col]
            origin_time = origin[orig_col]
            target_time = origin_time - self.seasonal_period

            eligible = lookup_history[
                (lookup_history[self.product_col] == product)
                & (lookup_history[self.time_col] <= origin_time)
            ].copy()

            if eligible.empty:
                if self.cold_start_policy == "global_median":
                    predictions.append(self._global_median)
                    matched_times.append(pd.NaT)
                    fallback_flags.append(True)
                    continue
                raise ValueError(
                    f"No historical price for product '{product}' "
                    f"at or before forecast origin {origin_time}."
                )

            eligible["time_distance"] = (eligible[self.time_col] - target_time).abs()
            seasonal_candidates = eligible[
                eligible["time_distance"] <= self.tolerance
            ].sort_values(
                ["time_distance", self.time_col],
                ascending=[True, False],
            )

            used_fallback = False
            if not seasonal_candidates.empty:
                matched = seasonal_candidates.iloc[0]
            elif self.fallback == "persistence":
                matched = eligible.iloc[-1]
                used_fallback = True
            else:
                raise ValueError(
                    f"No seasonal observation within {self.tolerance} "
                    f"of {target_time} for product '{product}'."
                )

            predictions.append(float(matched[self.target_col]))
            matched_times.append(matched[self.time_col])
            fallback_flags.append(used_fallback)

        result["seasonal_prediction"] = predictions
        result["matched_history_at"] = matched_times
        result["used_fallback"] = fallback_flags
        result["horizon"] = self.horizon
        return result

    def get_metadata(self) -> dict[str, Any]:
        """Return model configuration and fitted state metadata."""
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "seasonal_period": str(self.seasonal_period),
            "tolerance": str(self.tolerance),
            "fallback": self.fallback,
            "horizon": self.horizon,
            "cold_start_policy": self.cold_start_policy,
            "train_rows": len(self._history) if self._history is not None else 0,
            "global_median": self._global_median,
        }
