
import pandas as pd
import pytest

from petrotwin.features.pipeline import (
    FoldFeaturePipeline,
    build_features,
    get_feature_columns,
)


@pytest.fixture
def sample_panel():
    return pd.DataFrame(
        {
            "product_id": ["diesel"] * 5 + ["gasoline"] * 5,
            "effective_at_local": (
                pd.date_range("2024-01-01", periods=5, freq="30D").tolist()
                + pd.date_range("2024-01-01", periods=5, freq="30D").tolist()
            ),
            "price_value": [100, 110, 120, 130, 140] + [
                200, 210, 220, 230, 240
            ],
            "price_change_value": [None, 10, 10, 10, 10] * 2,
            "brent_usd_per_barrel": [None, 70, 72, 74, 76] * 2,
            "usd_vnd_rate": [None, 25000, 25100, 25200, 25300] * 2,
            "stabilization_fund_subsidy": [None, 0, 1, None, 0] * 2,
        }
    )


def test_lag_uses_previous_event_only(sample_panel):
    result = build_features(sample_panel)

    diesel = result[result["product_id"] == "diesel"].reset_index(drop=True)

    assert pd.isna(diesel.loc[0, "price_value_lag_1"])
    assert diesel.loc[1, "price_value_lag_1"] == 100
    assert diesel.loc[2, "price_value_lag_1"] == 110


def test_products_have_separate_histories(sample_panel):
    result = build_features(sample_panel)

    gasoline = result[
        result["product_id"] == "gasoline"
    ].reset_index(drop=True)

    assert pd.isna(gasoline.loc[0, "price_value_lag_1"])
    assert gasoline.loc[1, "price_value_lag_1"] == 200


def test_rolling_excludes_current_event(sample_panel):
    result = build_features(sample_panel)

    diesel = result[result["product_id"] == "diesel"].reset_index(drop=True)

    assert diesel.loc[2, "price_value_rolling_mean_3"] == 105
    assert diesel.loc[3, "price_value_rolling_mean_3"] == 110


def test_current_target_is_not_a_feature(sample_panel):
    result = build_features(sample_panel)
    columns = get_feature_columns(result)

    assert "price_value" not in columns
    assert "price_change_value" not in columns
    assert "stabilization_fund_subsidy" not in columns


def test_fold_pipeline_uses_training_history(sample_panel):
    train = sample_panel[
        sample_panel["effective_at_local"] <= "2024-03-01"
    ]
    valid = sample_panel[
        sample_panel["effective_at_local"] > "2024-03-01"
    ]

    pipeline = FoldFeaturePipeline()
    pipeline.fit(train)

    transformed = pipeline.transform(valid)

    diesel = transformed[
        transformed["product_id"] == "diesel"
    ].reset_index(drop=True)

    assert diesel.loc[0, "price_value_lag_1"] == 120


def test_transform_requires_fit(sample_panel):
    pipeline = FoldFeaturePipeline()

    with pytest.raises(RuntimeError):
        pipeline.transform(sample_panel)


def test_duplicate_event_keys_are_rejected(sample_panel):
    duplicated = pd.concat(
        [sample_panel, sample_panel.iloc[[0]]],
        ignore_index=True,
    )

    with pytest.raises(ValueError, match="Duplicate"):
        build_features(duplicated)


# ============================================================
# BOG HISTORICAL FEATURE TESTS
# ============================================================

def test_bog_features_use_previous_event_only():
    panel = pd.DataFrame({
        "product_id": ["RON95"] * 4,
        "effective_at_local": pd.to_datetime([
            "2025-01-01",
            "2025-01-15",
            "2025-02-01",
            "2025-02-15",
        ]),
        "price_value": [20000, 20500, 21000, 21500],
    })

    bog = pd.DataFrame({
        "product_id": ["RON95"] * 4,
        "source_record_date": pd.to_datetime([
            "2025-01-01",
            "2025-01-15",
            "2025-02-01",
            "2025-02-15",
        ]),
        "amount": [0, 500, 0, 1000],
    })

    result = build_features(panel, bog_annotations=bog)

    assert pd.isna(result.loc[0, "bog_amount_lag_1"])
    assert result.loc[1, "bog_amount_lag_1"] == 0
    assert result.loc[2, "bog_amount_lag_1"] == 500

    # BOG hiện tại không được đưa vào đặc trưng của chính sự kiện đó.
    assert result.loc[3, "bog_amount"] == 1000
    assert result.loc[3, "bog_amount_lag_1"] == 0


def test_missing_bog_is_not_converted_to_no_action():
    panel = pd.DataFrame({
        "product_id": ["RON95"] * 3,
        "effective_at_local": pd.to_datetime([
            "2025-01-01",
            "2025-01-15",
            "2025-02-01",
        ]),
        "price_value": [20000, 20500, 21000],
    })

    bog = pd.DataFrame({
        "product_id": ["RON95"],
        "source_record_date": pd.to_datetime(["2025-01-01"]),
        "amount": [0],
    })

    result = build_features(panel, bog_annotations=bog)

    assert result.loc[0, "bog_class"] == 0
    assert pd.isna(result.loc[1, "bog_class"])
    assert pd.isna(result.loc[2, "bog_class"])

    # Chỉ đếm NO_ACTION đã biết, không tính dữ liệu thiếu.
    assert result.loc[1, "bog_no_action_count_3"] == 1
    assert result.loc[2, "bog_no_action_count_3"] == 1


def test_bog_histories_are_separated_by_product():
    panel = pd.DataFrame({
        "product_id": [
            "DIESEL",
            "DIESEL",
            "RON95",
            "RON95",
        ],
        "effective_at_local": pd.to_datetime([
            "2025-01-01",
            "2025-01-15",
            "2025-01-01",
            "2025-01-15",
        ]),
        "price_value": [18000, 18500, 20000, 20500],
    })

    bog = pd.DataFrame({
        "product_id": ["DIESEL", "RON95"],
        "source_record_date": pd.to_datetime([
            "2025-01-01",
            "2025-01-01",
        ]),
        "amount": [500, 0],
    })

    result = build_features(panel, bog_annotations=bog)

    diesel = result[
        (result["product_id"] == "DIESEL")
        & (
            result["effective_at_local"]
            == pd.Timestamp("2025-01-15")
        )
    ].iloc[0]

    ron95 = result[
        (result["product_id"] == "RON95")
        & (
            result["effective_at_local"]
            == pd.Timestamp("2025-01-15")
        )
    ].iloc[0]

    assert diesel["bog_amount_lag_1"] == 500
    assert ron95["bog_amount_lag_1"] == 0
def test_price_anomaly_features_use_past_events_only():
    panel = pd.DataFrame({
        "product_id": ["RON95"] * 6,
        "effective_at_local": pd.date_range(
            "2025-01-01", periods=6, freq="15D"
        ),
        "price_value": [20000, 20100, 20200, 20300, 21000, 21100],
        "price_change_value": [None, 100, 100, 100, 700, 100],
    })

    result = build_features(panel)

    # Không lấy mức tăng 700 của sự kiện hiện tại vào historical mean.
    assert result.loc[4, "price_change_mean_3"] == 100

    # Mức thay đổi của sự kiện trước là 100.
    assert result.loc[4, "price_change_value_lag_1"] == 100

    # Feature phải nằm trong danh sách được phép.
    columns = get_feature_columns(result)
    assert "price_change_mean_3" in columns
    assert "price_change_std_3" in columns
    assert "price_increase_zscore_lag_1" in columns



# ============================================================
# POINT-IN-TIME MARKET, INTRA-CYCLE, IMPUTATION & LIVE INFERENCE TESTS
# ============================================================

def test_market_asof_join_enforces_strict_prior_date_and_intra_cycle_vol():
    panel = pd.DataFrame({
        "product_id": ["RON95"] * 3,
        "effective_at_local": [
            "2025-01-09 15:00:00+07:00",
            "2025-01-16 15:00:00+07:00",
            "2025-01-30 15:00:00+07:00",
        ],
        "price_value": [20000, 20500, 21000],
    })
    market = pd.DataFrame({
        "series_id": [
            "brent_dated_usd_bbl",
            "brent_dated_usd_bbl",
            "brent_dated_usd_bbl",
            "brent_dated_usd_bbl",
            "usd_vnd_sbv_reference",
            "usd_vnd_sbv_reference",
            "usd_vnd_sbv_reference",
        ],
        "observation_date": [
            "2025-01-06",
            "2025-01-07",
            "2025-01-08",
            "2025-01-09",  # same day as 2025-01-09 event -> MUST NOT BE USED
            "2025-01-08",
            "2025-01-09",  # same day -> MUST NOT BE USED
            "2025-01-15",  # >7d before 2025-01-30 -> stale on 2025-01-30
        ],
        "value": [74.0, 75.0, 76.0, 999.0, 24000.0, 99999.0, 24100.0],
    })

    result = build_features(panel, market_observations=market, fx_max_age_days=7)

    # Same-day 999.0 / 99999.0 must NOT leak into 2025-01-09
    assert result.loc[0, "brent_usd_per_barrel"] == 76.0
    assert result.loc[0, "usd_vnd_rate"] == 24000.0
    assert pd.notna(result.loc[0, "brent_intra_cycle_volatility"])
    # On 2025-01-30, latest FX is 2025-01-15 (15d > 7d) -> must be NaN
    assert pd.isna(result.loc[2, "usd_vnd_rate"])

    cols = get_feature_columns(result, include_exogenous=True)
    assert "brent_intra_cycle_volatility" in cols
    assert "brent_vnd_implied" in cols


def test_panel_same_day_market_date_raises_leakage_error():
    panel = pd.DataFrame({
        "product_id": ["RON95"],
        "effective_at_local": ["2025-01-09 15:00:00+07:00"],
        "price_value": [20000],
        "brent_observation_date": ["2025-01-09"],
        "brent_usd_per_barrel": [75.0],
    })
    with pytest.raises(ValueError, match="Point-in-time leakage detected"):
        build_features(panel)


def test_price_increase_positive_preserves_nan():
    panel = pd.DataFrame({
        "product_id": ["RON95"] * 3,
        "effective_at_local": pd.date_range("2025-01-01", periods=3, freq="7D"),
        "price_value": [20000, 20500, 20300],
        "price_change_value": [None, 500, -200],
    })
    result = build_features(panel)
    assert pd.isna(result.loc[0, "price_increase_positive_lag_1"])
    assert pd.isna(result.loc[1, "price_increase_positive_lag_1"])
    assert result.loc[2, "price_increase_positive_lag_1"] == 1.0


def test_fold_pipeline_imputation_and_live_inference_without_target(sample_panel):
    train = sample_panel[sample_panel["effective_at_local"] <= "2024-03-01"]
    live = sample_panel[sample_panel["effective_at_local"] > "2024-03-01"].drop(
        columns=["price_value"]
    )

    pipe = FoldFeaturePipeline(impute=True)
    tr_out = pipe.fit_transform(train)
    medians_snapshot = pipe._medians.copy()

    live_out = pipe.transform(live)
    pd.testing.assert_series_equal(pipe._medians, medians_snapshot)
    assert not tr_out[pipe.feature_columns_].isna().any().any()
    assert not live_out[pipe.feature_columns_].isna().any().any()

    # Out-of-order transform timestamp must raise ValueError
    with pytest.raises(ValueError, match="strictly after"):
        pipe.transform(train.iloc[[-1]])
