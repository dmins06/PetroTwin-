"""Point-in-time-safe feature engineering for PetroTwin."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {
    "product_id",
    "effective_at_local",
    "price_value",
}

DEFAULT_LAGS = (1, 2, 3, 5)
DEFAULT_ROLLING_WINDOWS = (3, 5, 7)

# Historical values are shifted before they become predictors.
HISTORICAL_COLUMNS = (
    "price_value",
    "price_change_value",
    "stabilization_fund_subsidy",
    "brent_usd_per_barrel",
    "usd_vnd_rate",
)

# These columns are converted when present, but are not automatically
# approved as predictors. Point-in-time availability must be verified first.
CURRENT_EXOGENOUS_COLUMNS = (
    "brent_usd_per_barrel",
    "brent_age_days",
    "usd_vnd_rate",
    "fx_age_days",
)

TARGET_COLUMNS = {
    "price_value",
    "price_change_value",
    "stabilization_fund_subsidy",
    "bog_amount",
    "bog_class",
}


def _prepare_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """Validate, copy and chronologically sort the input panel."""
    missing = REQUIRED_COLUMNS.difference(panel.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    if panel.empty:
        raise ValueError("Input panel must not be empty.")

    data = panel.copy()

    if data["product_id"].isna().any():
        raise ValueError("product_id must not contain null values.")

    data["effective_at_local"] = pd.to_datetime(
        data["effective_at_local"],
        errors="raise",
    )

    if data["effective_at_local"].isna().any():
        raise ValueError("effective_at_local must not contain null values.")

    duplicate_mask = data.duplicated(
        subset=["product_id", "effective_at_local"],
        keep=False,
    )
    if duplicate_mask.any():
        raise ValueError(
            "Duplicate (product_id, effective_at_local) keys found."
        )

    data = data.sort_values(
        ["product_id", "effective_at_local"],
        kind="mergesort",
    ).reset_index(drop=True)

    data["price_value"] = pd.to_numeric(
        data["price_value"],
        errors="coerce",
    )

    for column in HISTORICAL_COLUMNS:
        if column in data.columns:
            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

    for column in CURRENT_EXOGENOUS_COLUMNS:
        if column in data.columns:
            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

    return data


def _add_calendar_features(data: pd.DataFrame) -> pd.DataFrame:
    """Create calendar features from the effective timestamp.

    Note: these describe the effective timestamp, not a separately supplied
    forecast origin. Use them as predictors only when that schedule is known
    at the forecast origin.
    """
    result = data.copy()
    timestamp = result["effective_at_local"]

    day_of_week = timestamp.dt.dayofweek
    month = timestamp.dt.month
    day_of_year = timestamp.dt.dayofyear

    result["calendar_day_of_week"] = day_of_week
    result["calendar_day_of_month"] = timestamp.dt.day
    result["calendar_month"] = month
    result["calendar_day_of_year"] = day_of_year

    result["calendar_day_of_week_sin"] = np.sin(
        2 * np.pi * day_of_week / 7
    )
    result["calendar_day_of_week_cos"] = np.cos(
        2 * np.pi * day_of_week / 7
    )
    result["calendar_month_sin"] = np.sin(
        2 * np.pi * (month - 1) / 12
    )
    result["calendar_month_cos"] = np.cos(
        2 * np.pi * (month - 1) / 12
    )

    previous_event = result.groupby(
        "product_id",
        sort=False,
    )["effective_at_local"].shift(1)

    result["days_since_previous_event"] = (
        timestamp - previous_event
    ).dt.total_seconds() / 86400

    return result


def _add_historical_features(
    data: pd.DataFrame,
    lags: tuple[int, ...],
    rolling_windows: tuple[int, ...],
) -> pd.DataFrame:
    """Create strictly backward-looking lag and rolling features."""
    result = data.copy()

    for column in HISTORICAL_COLUMNS:
        if column not in result.columns:
            continue

        grouped = result.groupby("product_id", sort=False)[column]

        for lag in lags:
            result[f"{column}_lag_{lag}"] = grouped.shift(lag)

        # Shift by one event before rolling to exclude the current event.
        previous_values = grouped.shift(1)

        for window in rolling_windows:
            rolling = previous_values.groupby(
                result["product_id"],
                sort=False,
            ).rolling(
                window=window,
                min_periods=1,
            )

            mean = rolling.mean().reset_index(level=0, drop=True)
            result[f"{column}_rolling_mean_{window}"] = (
                mean.reindex(result.index)
            )

            if column in {
                "price_value",
                "brent_usd_per_barrel",
                "usd_vnd_rate",
            }:
                std = rolling.std().reset_index(level=0, drop=True)
                result[f"{column}_rolling_std_{window}"] = (
                    std.reindex(result.index)
                )

    return result


def _add_bog_historical_features(
    data: pd.DataFrame,
    bog_annotations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Add historical BOG features without using the current event's label.

    BOG classes:
        0 = NO_ACTION (amount == 0)
        1 = SPEND (amount > 0)
        2 = CONTRIBUTE (amount < 0)

    Missing annotations remain NaN; they are never converted to NO_ACTION.
    """
    result = data.copy()

    result["bog_amount"] = np.nan
    result["bog_class"] = np.nan

    if bog_annotations is not None:
        required = {
            "product_id",
            "source_record_date",
            "amount",
        }
        missing = required.difference(bog_annotations.columns)
        if missing:
            raise ValueError(
                f"Missing BOG columns: {sorted(missing)}"
            )

        bog = bog_annotations[
            ["product_id", "source_record_date", "amount"]
        ].copy()

        if bog["product_id"].isna().any():
            raise ValueError("BOG product_id must not contain null values.")

        bog["_event_date"] = pd.to_datetime(
            bog["source_record_date"],
            errors="raise",
        ).dt.date

        if bog["_event_date"].isna().any():
            raise ValueError("BOG dates must not contain null values.")

        bog["amount"] = pd.to_numeric(
            bog["amount"],
            errors="coerce",
        )
        if bog["amount"].isna().any():
            raise ValueError("BOG amount must be numeric and non-null.")

        if bog.duplicated(
            ["product_id", "_event_date"]
        ).any():
            raise ValueError(
                "Duplicate BOG (product_id, source_record_date) keys found."
            )

        result["_event_date"] = (
            result["effective_at_local"].dt.date
        )

        result = result.merge(
            bog[["product_id", "_event_date", "amount"]],
            on=["product_id", "_event_date"],
            how="left",
            validate="one_to_one",
        )

        result["bog_amount"] = result["amount"]
        result = result.drop(columns=["_event_date", "amount"])

    result["bog_class"] = np.select(
        [
            result["bog_amount"].eq(0),
            result["bog_amount"].gt(0),
            result["bog_amount"].lt(0),
        ],
        [0, 1, 2],
        default=np.nan,
    )

    grouped_amount = result.groupby(
        "product_id",
        sort=False,
    )["bog_amount"]
    grouped_class = result.groupby(
        "product_id",
        sort=False,
    )["bog_class"]

    # Shift first: current-event BOG must not enter its own predictors.
    previous_amount = grouped_amount.shift(1)
    previous_class = grouped_class.shift(1)

    result["bog_amount_lag_1"] = previous_amount
    result["bog_class_lag_1"] = previous_class

    for class_id, class_name in (
        (0, "no_action"),
        (1, "spend"),
        (2, "contribute"),
    ):
        result[f"bog_{class_name}_count_3"] = (
            previous_class.eq(class_id)
            .astype(float)
            .groupby(result["product_id"], sort=False)
            .transform(
                lambda values: values.rolling(
                    window=3,
                    min_periods=1,
                ).sum()
            )
        )

    result["bog_known_count_3"] = (
        previous_class.notna()
        .astype(float)
        .groupby(result["product_id"], sort=False)
        .transform(
            lambda values: values.rolling(
                window=3,
                min_periods=1,
            ).sum()
        )
    )

    result["bog_last_known_amount"] = (
        previous_amount
        .groupby(result["product_id"], sort=False)
        .ffill()
    )

    return result

def _add_price_anomaly_features(
    data: pd.DataFrame,
    window: int = 3,
) -> pd.DataFrame:
    """Create backward-looking price increase anomaly features."""
    result = data.copy()

    if "price_change_value" not in result.columns:
        return result

    if window < 2:
        raise ValueError("Anomaly window must be at least 2.")

    grouped = result.groupby(
        "product_id",
        sort=False,
    )["price_change_value"]

    # Previous event only: exclude current event.
    previous_change = grouped.shift(1)

    result["price_change_mean_3"] = (
        previous_change.groupby(
            result["product_id"],
            sort=False,
        ).transform(
            lambda values: values.rolling(
                window=window,
                min_periods=2,
            ).mean()
        )
    )

    result["price_change_std_3"] = (
        previous_change.groupby(
            result["product_id"],
            sort=False,
        ).transform(
            lambda values: values.rolling(
                window=window,
                min_periods=2,
            ).std()
        )
    )

    # Compare previous change with events before that event.
    baseline_values = previous_change.groupby(
        result["product_id"],
        sort=False,
    ).shift(1)

    baseline_mean = baseline_values.groupby(
        result["product_id"],
        sort=False,
    ).transform(
        lambda values: values.rolling(
            window=window,
            min_periods=2,
        ).mean()
    )

    baseline_std = baseline_values.groupby(
        result["product_id"],
        sort=False,
    ).transform(
        lambda values: values.rolling(
            window=window,
            min_periods=2,
        ).std()
    )

    result["price_increase_zscore_lag_1"] = (
        (previous_change - baseline_mean)
        / baseline_std.replace(0, np.nan)
    )

    result["price_increase_positive_lag_1"] = (
        previous_change.gt(0).astype(float)
    )

    return result

def build_features(
    panel: pd.DataFrame,
    lags: tuple[int, ...] = DEFAULT_LAGS,
    rolling_windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS,
    bog_annotations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build features from a complete chronological history.

    Original columns are retained for auditing. Current-event target columns
    are excluded from the approved predictor list.
    """
    if any(lag < 1 for lag in lags):
        raise ValueError("All lags must be positive integers.")

    if any(window < 1 for window in rolling_windows):
        raise ValueError("All rolling windows must be positive integers.")

    data = _prepare_panel(panel)
    data = _add_calendar_features(data)
    data = _add_historical_features(data, lags, rolling_windows)
    data = _add_price_anomaly_features(data)
    data = _add_bog_historical_features(data, bog_annotations)

    return data


def get_feature_columns(features: pd.DataFrame) -> list[str]:
    """Return explicitly approved predictor columns.

    Current-event targets, raw BOG labels, identifiers and source metadata
    are intentionally excluded.
    """
    approved_prefixes = (
        "calendar_",
        "days_since_previous_event",
        "price_value_lag_",
        "price_change_value_lag_",
        "stabilization_fund_subsidy_lag_",
        "price_value_rolling_",
        "price_change_value_rolling_",
        "stabilization_fund_subsidy_rolling_",
        "bog_amount_lag_",
        "bog_class_lag_",
        "bog_spend_count_",
        "bog_no_action_count_",
        "bog_contribute_count_",
        "bog_known_count_",
        "bog_last_known_amount",
        "price_change_mean_",
        "price_change_std_",
        "price_increase_zscore_",
        "price_increase_positive_",
    )

    return [
        column
        for column in features.columns
        if column.startswith(approved_prefixes)
    ]


@dataclass
class FoldFeaturePipeline:
    """Fold-specific feature transformer.

    fit() stores training history and fits imputation statistics on train only.
    transform() computes backward-looking features for new rows using stored
    training history. This class does not itself construct forecast origins or
    verify market-data publication timestamps.
    """

    lags: tuple[int, ...] = DEFAULT_LAGS
    rolling_windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS
    impute: bool = False
    bog_annotations: pd.DataFrame | None = None

    _history: pd.DataFrame | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _medians: pd.Series | None = field(
        default=None,
        init=False,
        repr=False,
    )
    feature_columns_: list[str] = field(
        default_factory=list,
        init=False,
    )

    def fit(self, train: pd.DataFrame) -> FoldFeaturePipeline:
        """Fit preprocessing statistics on training data only."""
        if self._history is not None:
            raise RuntimeError(
                "Pipeline is already fitted. Create a new instance per fold."
            )

        history = _prepare_panel(train)
        self._history = history.copy()

        train_features = build_features(
            history,
            lags=self.lags,
            rolling_windows=self.rolling_windows,
            bog_annotations=self.bog_annotations,
        )

        self.feature_columns_ = get_feature_columns(train_features)

        if self.impute:
            numeric_features = train_features[
                self.feature_columns_
            ].select_dtypes(include="number")
            self._medians = numeric_features.median()

        return self

    def transform(self, data: pd.DataFrame) -> pd.DataFrame:
        """Transform new chronological rows using stored training history."""
        if self._history is None:
            raise RuntimeError("Call fit(train) before transform(data).")

        new_data = _prepare_panel(data)
        if new_data.empty:
            return new_data

        history = self._history.copy()

        last_timestamp = history.groupby("product_id")[
            "effective_at_local"
        ].max()

        for product_id, group in new_data.groupby("product_id"):
            if product_id in last_timestamp.index:
                if group["effective_at_local"].min() <= last_timestamp.loc[
                    product_id
                ]:
                    raise ValueError(
                        "Transform data must occur strictly after "
                        "the fitted history for each product."
                    )

        combined = pd.concat(
            [history, new_data],
            ignore_index=True,
        )

        combined_features = build_features(
            combined,
            lags=self.lags,
            rolling_windows=self.rolling_windows,
            bog_annotations=self.bog_annotations,
        )

        new_keys = new_data[
            ["product_id", "effective_at_local"]
        ]

        transformed = combined_features.merge(
            new_keys,
            on=["product_id", "effective_at_local"],
            how="inner",
            validate="one_to_one",
        )

        transformed = transformed.sort_values(
            ["product_id", "effective_at_local"],
            kind="mergesort",
        ).reset_index(drop=True)

        for column in self.feature_columns_:
            if column not in transformed.columns:
                transformed[column] = np.nan

        if self.impute and self._medians is not None:
            for column, median in self._medians.items():
                if column in transformed.columns:
                    transformed[column] = transformed[column].fillna(
                        median
                    )

        return transformed

    def fit_transform(self, train: pd.DataFrame) -> pd.DataFrame:
        """Fit on training data and return its engineered features."""
        self.fit(train)

        result = build_features(
            self._history,
            lags=self.lags,
            rolling_windows=self.rolling_windows,
            bog_annotations=self.bog_annotations,
        )

        if self.impute and self._medians is not None:
            for column, median in self._medians.items():
                if column in result.columns:
                    result[column] = result[column].fillna(median)

        return result
