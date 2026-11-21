
"""Weekly seasonal naive baseline for fuel price forecasting."""

import pandas as pd


class SeasonalNaiveBaseline:
    """Predict using the observation closest to one seasonal period ago."""

    def __init__(
        self,
        product_col: str = "product_id",
        time_col: str = "effective_at_local",
        target_col: str = "price_value",
        origin_time_col: str = "forecast_origin_local",
        seasonal_period: str = "7D",
        tolerance: str = "1D",
        fallback: str = "persistence",
    ) -> None:
        if fallback not in {"persistence", "raise"}:
            raise ValueError(
                "fallback must be either 'persistence' or 'raise'."
            )

        self.product_col = product_col
        self.time_col = time_col
        self.target_col = target_col
        self.origin_time_col = origin_time_col
        self.seasonal_period = pd.Timedelta(seasonal_period)
        self.tolerance = pd.Timedelta(tolerance)
        self.fallback = fallback
        self._history = None

        if self.seasonal_period <= pd.Timedelta(0):
            raise ValueError("seasonal_period must be positive.")

        if self.tolerance < pd.Timedelta(0):
            raise ValueError("tolerance cannot be negative.")

    def fit(self, data: pd.DataFrame):
        """Store historical observations."""
        required = [
            self.product_col,
            self.time_col,
            self.target_col,
        ]
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
            raise ValueError(
                "Duplicate product and effective timestamp found."
            )

        self._history = history.sort_values(
            [self.product_col, self.time_col]
        ).reset_index(drop=True)

        return self

    def predict(self, forecast_origins: pd.DataFrame) -> pd.DataFrame:
        """Generate weekly seasonal predictions without using future data."""
        if self._history is None:
            raise RuntimeError("Call fit() before predict().")

        required = [self.product_col, self.origin_time_col]
        missing = set(required) - set(forecast_origins.columns)

        if missing:
            raise ValueError(
                f"Missing forecast origin columns: {sorted(missing)}"
            )

        result = forecast_origins.copy()

        if result.empty:
            result["seasonal_prediction"] = pd.Series(dtype="float64")
            result["matched_history_at"] = pd.Series(
                dtype="datetime64[ns, UTC]"
            )
            result["used_fallback"] = pd.Series(dtype="bool")
            return result

        result[self.origin_time_col] = pd.to_datetime(
            result[self.origin_time_col], utc=True, errors="raise"
        )

        predictions = []
        matched_times = []
        fallback_flags = []

        for _, origin in result.iterrows():
            product = origin[self.product_col]
            origin_time = origin[self.origin_time_col]
            target_time = origin_time - self.seasonal_period

            eligible = self._history[
                (self._history[self.product_col] == product)
                & (self._history[self.time_col] <= origin_time)
            ].copy()

            if eligible.empty:
                raise ValueError(
                    f"No historical price for product '{product}' "
                    f"at or before forecast origin {origin_time}."
                )

            eligible["time_distance"] = (
                eligible[self.time_col] - target_time
            ).abs()

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

        return result
