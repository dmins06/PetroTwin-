"""Point-in-time-safe feature engineering for PetroTwin (T22)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

LITERS_PER_BARREL = 158.9873

REQUIRED_COLUMNS = {
    "product_id",
    "effective_at_local",
    "price_value",
}

INFERENCE_REQUIRED_COLUMNS = {
    "product_id",
    "effective_at_local",
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

# Strictly prior-day exogenous columns (observation_date < origin_date).
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


def _prepare_panel(
    panel: pd.DataFrame,
    require_target: bool = True,
) -> pd.DataFrame:
    """Validate, copy and chronologically sort the input panel."""
    required = REQUIRED_COLUMNS if require_target else INFERENCE_REQUIRED_COLUMNS
    missing = required.difference(panel.columns)
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
        raise ValueError("Duplicate (product_id, effective_at_local) keys found.")

    data = data.sort_values(
        ["product_id", "effective_at_local"],
        kind="mergesort",
    ).reset_index(drop=True)

    if "price_value" not in data.columns:
        data["price_value"] = np.nan
    data["price_value"] = pd.to_numeric(data["price_value"], errors="coerce")

    for column in HISTORICAL_COLUMNS:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    for column in CURRENT_EXOGENOUS_COLUMNS:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    return data


def _add_calendar_features(data: pd.DataFrame) -> pd.DataFrame:
    """Create calendar features from the effective timestamp."""
    result = data.copy()
    timestamp = result["effective_at_local"]

    day_of_week = timestamp.dt.day_of_week
    month = timestamp.dt.month
    day_of_year = timestamp.dt.day_of_year

    result["calendar_day_of_week"] = day_of_week
    result["calendar_day_of_month"] = timestamp.dt.day
    result["calendar_month"] = month
    result["calendar_day_of_year"] = day_of_year

    result["calendar_day_of_week_sin"] = np.sin(2 * np.pi * day_of_week / 7)
    result["calendar_day_of_week_cos"] = np.cos(2 * np.pi * day_of_week / 7)
    result["calendar_month_sin"] = np.sin(2 * np.pi * (month - 1) / 12)
    result["calendar_month_cos"] = np.cos(2 * np.pi * (month - 1) / 12)

    previous_event = result.groupby("product_id", sort=False)["effective_at_local"].shift(1)
    result["days_since_previous_event"] = (timestamp - previous_event).dt.total_seconds() / 86400

    return result


def _validate_panel_market_dates(data: pd.DataFrame) -> None:
    """Reject any panel row where market observation_date >= effective date."""
    eff_date = data["effective_at_local"].dt.tz_localize(None).dt.normalize()
    for col in ("brent_observation_date", "fx_observation_date"):
        if col in data.columns:
            obs_date = pd.to_datetime(data[col], errors="coerce").dt.normalize()
            leak_mask = obs_date.notna() & (obs_date >= eff_date)
            if leak_mask.any():
                raise ValueError(
                    f"Point-in-time leakage detected: {col} >= effective date "
                    f"in {int(leak_mask.sum())} row(s)."
                )


def _compute_daily_series_stats(
    market_df: pd.DataFrame,
    series_id: str,
    value_col_name: str,
    prefix: str,
) -> pd.DataFrame:
    """Compute strictly backward daily returns and rolling volatility for one market series."""
    required = {"series_id", "observation_date", "value"}
    missing = required.difference(market_df.columns)
    if missing:
        raise ValueError(f"Missing market_observations columns: {sorted(missing)}")

    sub = market_df.loc[market_df["series_id"] == series_id, ["observation_date", "value"]].copy()
    sub["value"] = pd.to_numeric(sub["value"], errors="coerce")
    sub = sub.dropna(subset=["observation_date", "value"])
    sub["obs_dt"] = pd.to_datetime(sub["observation_date"], errors="raise").dt.normalize()
    sub = sub.sort_values("obs_dt", kind="mergesort").drop_duplicates(subset=["obs_dt"], keep="last")

    ret_1d = sub["value"].pct_change(1)
    out = pd.DataFrame(
        {
            "obs_dt": sub["obs_dt"],
            f"{prefix}_obs_dt": sub["obs_dt"],
            value_col_name: sub["value"],
            f"{prefix}_ret_1d": ret_1d,
            f"{prefix}_ret_7d": sub["value"].pct_change(7),
            f"{prefix}_rolling_mean_7d": sub["value"].rolling(window=7, min_periods=1).mean(),
            f"{prefix}_rolling_std_7d": sub["value"].rolling(window=7, min_periods=2).std(),
            f"{prefix}_intra_cycle_volatility": ret_1d.rolling(window=7, min_periods=2).std(),
            f"{prefix}_intra_cycle_range": (
                sub["value"].rolling(window=7, min_periods=1).max()
                - sub["value"].rolling(window=7, min_periods=1).min()
            ),
        }
    )
    return out.reset_index(drop=True)


def _add_point_in_time_market_features(
    data: pd.DataFrame,
    market_observations: pd.DataFrame | None = None,
    fx_max_age_days: int = 7,
) -> pd.DataFrame:
    """Attach strictly prior-day exogenous Brent/FX and intra-cycle volatility features."""
    result = data.copy()
    origin_dates = result["effective_at_local"].dt.tz_localize(None).dt.normalize()

    if market_observations is not None:
        unique_origins = (
            pd.DataFrame({"origin_dt": origin_dates})
            .drop_duplicates()
            .sort_values("origin_dt")
            .reset_index(drop=True)
        )

        brent_daily = _compute_daily_series_stats(
            market_observations, "brent_dated_usd_bbl", "brent_usd_per_barrel", "brent"
        )
        fx_daily = _compute_daily_series_stats(
            market_observations, "usd_vnd_sbv_reference", "usd_vnd_rate", "fx"
        )

        joined = pd.merge_asof(
            unique_origins,
            brent_daily,
            left_on="origin_dt",
            right_on="obs_dt",
            direction="backward",
            allow_exact_matches=False,
        ).drop(columns=["obs_dt"])
        joined["brent_age_days"] = (joined["origin_dt"] - joined["brent_obs_dt"]).dt.days.astype(float)

        joined = pd.merge_asof(
            joined,
            fx_daily,
            left_on="origin_dt",
            right_on="obs_dt",
            direction="backward",
            allow_exact_matches=False,
        ).drop(columns=["obs_dt"])
        joined["fx_age_days"] = (joined["origin_dt"] - joined["fx_obs_dt"]).dt.days.astype(float)

        stale_fx = joined["fx_age_days"].isna() | (joined["fx_age_days"] > fx_max_age_days)
        fx_value_cols = [
            "usd_vnd_rate",
            "fx_ret_1d",
            "fx_ret_7d",
            "fx_rolling_mean_7d",
            "fx_rolling_std_7d",
            "fx_intra_cycle_volatility",
            "fx_intra_cycle_range",
        ]
        joined.loc[stale_fx, fx_value_cols] = np.nan
        joined = joined.drop(columns=["brent_obs_dt", "fx_obs_dt"])

        aligned = pd.DataFrame({"origin_dt": origin_dates}).merge(joined, on="origin_dt", how="left")
        for col in aligned.columns:
            if col != "origin_dt":
                result[col] = aligned[col].to_numpy()
    else:
        _validate_panel_market_dates(result)

    if "brent_usd_per_barrel" in result.columns and "usd_vnd_rate" in result.columns:
        result["brent_vnd_implied"] = result["brent_usd_per_barrel"] * result["usd_vnd_rate"]
        result["brent_vnd_per_liter_proxy"] = result["brent_vnd_implied"] / LITERS_PER_BARREL

    grp = result.groupby("product_id", sort=False)
    if "brent_usd_per_barrel" in result.columns:
        prev_brent = grp["brent_usd_per_barrel"].shift(1)
        result["brent_change_since_previous_event"] = result["brent_usd_per_barrel"] - prev_brent
        result["brent_pct_change_since_previous_event"] = np.where(
            prev_brent > 0,
            (result["brent_usd_per_barrel"] - prev_brent) / prev_brent,
            np.nan,
        )
    if "usd_vnd_rate" in result.columns:
        prev_fx = grp["usd_vnd_rate"].shift(1)
        result["fx_pct_change_since_previous_event"] = np.where(
            prev_fx > 0,
            (result["usd_vnd_rate"] - prev_fx) / prev_fx,
            np.nan,
        )

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
            ).rolling(window=window, min_periods=1)

            mean = rolling.mean().reset_index(level=0, drop=True)
            result[f"{column}_rolling_mean_{window}"] = mean.reindex(result.index)

            if column in {
                "price_value",
                "brent_usd_per_barrel",
                "usd_vnd_rate",
            }:
                std = rolling.std().reset_index(level=0, drop=True)
                result[f"{column}_rolling_std_{window}"] = std.reindex(result.index)

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
        required = {"product_id", "source_record_date", "amount"}
        missing = required.difference(bog_annotations.columns)
        if missing:
            raise ValueError(f"Missing BOG columns: {sorted(missing)}")

        bog = bog_annotations[["product_id", "source_record_date", "amount"]].copy()

        if bog["product_id"].isna().any():
            raise ValueError("BOG product_id must not contain null values.")

        bog["_event_date"] = pd.to_datetime(bog["source_record_date"], errors="raise").dt.date
        if bog["_event_date"].isna().any():
            raise ValueError("BOG dates must not contain null values.")

        bog["amount"] = pd.to_numeric(bog["amount"], errors="coerce")
        if bog["amount"].isna().any():
            raise ValueError("BOG amount must be numeric and non-null.")

        if bog.duplicated(["product_id", "_event_date"]).any():
            raise ValueError("Duplicate BOG (product_id, source_record_date) keys found.")

        result["_event_date"] = result["effective_at_local"].dt.date
        result = result.merge(
            bog[["product_id", "_event_date", "amount"]],
            on=["product_id", "_event_date"],
            how="left",
            validate="one_to_one",
        )
        result["bog_amount"] = result["amount"]
        result = result.drop(columns=["_event_date", "amount"])
    elif "stabilization_fund_subsidy_amount" in result.columns:
        result["bog_amount"] = pd.to_numeric(
            result["stabilization_fund_subsidy_amount"], errors="coerce"
        )

    result["bog_class"] = np.select(
        [
            result["bog_amount"].eq(0),
            result["bog_amount"].gt(0),
            result["bog_amount"].lt(0),
        ],
        [0, 1, 2],
        default=np.nan,
    )

    grouped_amount = result.groupby("product_id", sort=False)["bog_amount"]
    grouped_class = result.groupby("product_id", sort=False)["bog_class"]

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
            .transform(lambda values: values.rolling(window=3, min_periods=1).sum())
        )

    result["bog_known_count_3"] = (
        previous_class.notna()
        .astype(float)
        .groupby(result["product_id"], sort=False)
        .transform(lambda values: values.rolling(window=3, min_periods=1).sum())
    )

    result["bog_last_known_amount"] = previous_amount.groupby(
        result["product_id"], sort=False
    ).ffill()

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

    grouped = result.groupby("product_id", sort=False)["price_change_value"]

    # Previous event only: exclude current event.
    previous_change = grouped.shift(1)

    result["price_change_mean_3"] = previous_change.groupby(
        result["product_id"], sort=False
    ).transform(lambda values: values.rolling(window=window, min_periods=2).mean())

    result["price_change_std_3"] = previous_change.groupby(
        result["product_id"], sort=False
    ).transform(lambda values: values.rolling(window=window, min_periods=2).std())

    # Compare previous change with events before that event.
    baseline_values = previous_change.groupby(result["product_id"], sort=False).shift(1)

    baseline_mean = baseline_values.groupby(result["product_id"], sort=False).transform(
        lambda values: values.rolling(window=window, min_periods=2).mean()
    )
    baseline_std = baseline_values.groupby(result["product_id"], sort=False).transform(
        lambda values: values.rolling(window=window, min_periods=2).std()
    )

    result["price_increase_zscore_lag_1"] = (previous_change - baseline_mean) / baseline_std.replace(
        0, np.nan
    )
    # Preserve NaN when previous_change is missing instead of coercing to 0.0.
    result["price_increase_positive_lag_1"] = np.where(
        previous_change.isna(),
        np.nan,
        previous_change.gt(0).astype(float),
    )

    return result


def build_features(
    panel: pd.DataFrame,
    lags: tuple[int, ...] = DEFAULT_LAGS,
    rolling_windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS,
    bog_annotations: pd.DataFrame | None = None,
    market_observations: pd.DataFrame | None = None,
    fx_max_age_days: int = 7,
    require_target: bool = True,
) -> pd.DataFrame:
    """Build point-in-time-safe features from a chronological history.

    Original columns are retained for auditing. Current-event target columns
    are excluded from the approved predictor list.
    """
    if any(lag < 1 for lag in lags):
        raise ValueError("All lags must be positive integers.")

    if any(window < 1 for window in rolling_windows):
        raise ValueError("All rolling windows must be positive integers.")

    data = _prepare_panel(panel, require_target=require_target)
    data = _add_calendar_features(data)
    data = _add_point_in_time_market_features(
        data,
        market_observations=market_observations,
        fx_max_age_days=fx_max_age_days,
    )
    data = _add_historical_features(data, lags, rolling_windows)
    data = _add_price_anomaly_features(data)
    data = _add_bog_historical_features(data, bog_annotations)

    return data


def get_feature_columns(
    features: pd.DataFrame,
    include_exogenous: bool = True,
) -> list[str]:
    """Return explicitly approved predictor columns.

    Current-event targets, raw BOG labels, identifiers and source metadata
    are intentionally excluded.
    """
    approved_prefixes: list[str] = [
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
    ]
    if include_exogenous:
        approved_prefixes.extend(
            [
                "brent_usd_per_barrel_lag_",
                "usd_vnd_rate_lag_",
                "brent_usd_per_barrel_rolling_",
                "usd_vnd_rate_rolling_",
                "brent_ret_",
                "brent_rolling_",
                "brent_intra_cycle_",
                "brent_vnd_",
                "brent_change_since_",
                "brent_pct_change_since_",
                "fx_ret_",
                "fx_rolling_",
                "fx_intra_cycle_",
                "fx_pct_change_since_",
            ]
        )

    prefix_tuple = tuple(approved_prefixes)
    return [
        column
        for column in features.columns
        if column not in TARGET_COLUMNS and column.startswith(prefix_tuple)
    ]


@dataclass
class FoldFeaturePipeline:
    """Fold-specific feature transformer.

    fit() stores training history and fits imputation statistics on train only.
    transform() computes backward-looking features for new rows using stored
    training history and supports both evaluation rows (with price_value) and
    live inference rows (without price_value).
    """

    lags: tuple[int, ...] = DEFAULT_LAGS
    rolling_windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS
    impute: bool = False
    bog_annotations: pd.DataFrame | None = None
    market_observations: pd.DataFrame | None = None
    fx_max_age_days: int = 7
    include_exogenous: bool = True

    _history: pd.DataFrame | None = field(default=None, init=False, repr=False)
    _medians: pd.Series | None = field(default=None, init=False, repr=False)
    feature_columns_: list[str] = field(default_factory=list, init=False)

    def fit(self, train: pd.DataFrame) -> FoldFeaturePipeline:
        """Fit preprocessing statistics on training data only."""
        if self._history is not None:
            raise RuntimeError(
                "Pipeline is already fitted. Create a new instance per fold."
            )

        history = _prepare_panel(train, require_target=True)
        self._history = history.copy()

        train_features = build_features(
            history,
            lags=self.lags,
            rolling_windows=self.rolling_windows,
            bog_annotations=self.bog_annotations,
            market_observations=self.market_observations,
            fx_max_age_days=self.fx_max_age_days,
            require_target=True,
        )

        self.feature_columns_ = get_feature_columns(
            train_features,
            include_exogenous=self.include_exogenous,
        )

        if self.impute:
            numeric_features = train_features[self.feature_columns_].select_dtypes(
                include="number"
            )
            self._medians = numeric_features.median().fillna(0.0)

        logger.debug(
            "Fitted FoldFeaturePipeline on %d rows with %d features.",
            len(history),
            len(self.feature_columns_),
        )
        return self

    def transform(self, data: pd.DataFrame) -> pd.DataFrame:
        """Transform new chronological rows using stored training history."""
        if self._history is None:
            raise RuntimeError("Call fit(train) before transform(data).")

        new_data = _prepare_panel(data, require_target=False)
        if new_data.empty:
            return new_data

        history = self._history.copy()
        last_timestamp = history.groupby("product_id")["effective_at_local"].max()

        for product_id, group in new_data.groupby("product_id"):
            if (
                product_id in last_timestamp.index
                and group["effective_at_local"].min() <= last_timestamp.loc[product_id]
            ):
                raise ValueError(
                        "Transform data must occur strictly after "
                        "the fitted history for each product."
                    )

        combined = pd.concat([history, new_data], ignore_index=True)

        combined_features = build_features(
            combined,
            lags=self.lags,
            rolling_windows=self.rolling_windows,
            bog_annotations=self.bog_annotations,
            market_observations=self.market_observations,
            fx_max_age_days=self.fx_max_age_days,
            require_target=False,
        )

        new_keys = new_data[["product_id", "effective_at_local"]]
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
                    transformed[column] = transformed[column].fillna(median)

        return transformed

    def fit_transform(self, train: pd.DataFrame) -> pd.DataFrame:
        """Fit on training data and return its engineered features."""
        self.fit(train)

        result = build_features(
            self._history,
            lags=self.lags,
            rolling_windows=self.rolling_windows,
            bog_annotations=self.bog_annotations,
            market_observations=self.market_observations,
            fx_max_age_days=self.fx_max_age_days,
            require_target=True,
        )

        if self.impute and self._medians is not None:
            for column, median in self._medians.items():
                if column in result.columns:
                    result[column] = result[column].fillna(median)

        return result
