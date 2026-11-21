
"""Naive persistence baseline for fuel price forecasting."""

import pandas as pd


class NaivePersistenceBaseline:
    """Predict the next price using the latest price known at each origin."""

    def __init__(
        self,
        product_col: str = "product_id",
        time_col: str = "effective_at_local",
        target_col: str = "price_value",
    ) -> None:
        self.product_col = product_col
        self.time_col = time_col
        self.target_col = target_col
        self._history = None

    def fit(self, data: pd.DataFrame):
        """Store historical observations used to generate predictions."""
        required = {self.product_col, self.time_col, self.target_col}
        missing = required - set(data.columns)

        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")

        history = data[list(required)].copy()

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

    def predict(
        self,
        forecast_origins: pd.DataFrame,
        origin_time_col: str = "forecast_origin_local",
    ) -> pd.DataFrame:
        """
        Predict each product's price using only observations available
        at or before its forecast origin.

        forecast_origins must contain product_col and origin_time_col.
        """
        if self._history is None:
            raise RuntimeError("Call fit() before predict().")

        required = {self.product_col, origin_time_col}
        missing = required - set(forecast_origins.columns)

        if missing:
            raise ValueError(f"Missing forecast origin columns: {sorted(missing)}")

        result = forecast_origins.copy()

        if result.empty:
            result["naive_prediction"] = pd.Series(dtype="float64")
            result["matched_history_at"] = pd.Series(dtype="datetime64[ns, UTC]")
            return result

        result[origin_time_col] = pd.to_datetime(
            result[origin_time_col], utc=True, errors="raise"
        )

        predictions = []
        matched_times = []

        for _, origin in result.iterrows():
            product = origin[self.product_col]
            origin_time = origin[origin_time_col]

            eligible = self._history[
                (self._history[self.product_col] == product)
                & (self._history[self.time_col] <= origin_time)
            ]

            if eligible.empty:
                raise ValueError(
                    f"No historical price for product '{product}' "
                    f"at or before forecast origin {origin_time}."
                )

            latest = eligible.iloc[-1]

            predictions.append(float(latest[self.target_col]))
            matched_times.append(latest[self.time_col])

        result["naive_prediction"] = predictions
        result["matched_history_at"] = matched_times

        return result
