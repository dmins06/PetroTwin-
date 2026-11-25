import pandas as pd
import pytest

from petrotwin.models.baselines.naive import NaivePersistenceBaseline
from petrotwin.models.baselines.seasonal_naive import SeasonalNaiveBaseline


@pytest.fixture
def sample_history():
    return pd.DataFrame(
        {
            "product_id": ["diesel", "diesel", "diesel",
                           "gasoline", "gasoline"],
            "effective_at_local": [
                "2024-01-01 15:00:00+07:00",
                "2024-01-08 15:00:00+07:00",
                "2024-01-15 15:00:00+07:00",
                "2024-01-01 15:00:00+07:00",
                "2024-01-08 15:00:00+07:00",
            ],
            "price_value": [20000, 21000, 22000, 23000, 24000],
        }
    )


def test_naive_uses_latest_known_price(sample_history):
    model = NaivePersistenceBaseline().fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-10 10:00:00+07:00"],
    })

    result = model.predict(origins)

    assert result.loc[0, "naive_prediction"] == 21000


def test_products_have_separate_histories(sample_history):
    model = NaivePersistenceBaseline().fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["gasoline"],
        "forecast_origin_local": ["2024-01-10 10:00:00+07:00"],
    })

    result = model.predict(origins)

    assert result.loc[0, "naive_prediction"] == 24000


def test_naive_does_not_use_future_observation(sample_history):
    model = NaivePersistenceBaseline().fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-05 10:00:00+07:00"],
    })

    result = model.predict(origins)

    assert result.loc[0, "naive_prediction"] == 20000


def test_predict_before_fit_raises_error():
    model = NaivePersistenceBaseline()

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-10 10:00:00+07:00"],
    })

    with pytest.raises(RuntimeError):
        model.predict(origins)


def test_unknown_product_history_raises_error(sample_history):
    model = NaivePersistenceBaseline().fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["unknown"],
        "forecast_origin_local": ["2024-01-10 10:00:00+07:00"],
    })

    with pytest.raises(ValueError, match="No historical price"):
        model.predict(origins)

def test_seasonal_naive_uses_price_from_previous_week(sample_history):
    model = SeasonalNaiveBaseline().fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-15 15:00:00+07:00"],
    })

    result = model.predict(origins)

    # Origin: Jan 15; previous-week target: Jan 8.
    assert result.loc[0, "seasonal_prediction"] == 21000
    assert result.loc[0, "used_fallback"] == False


def test_seasonal_naive_falls_back_to_persistence(sample_history):
    model = SeasonalNaiveBaseline(
        tolerance="1D",
        fallback="persistence",
    ).fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-20 15:00:00+07:00"],
    })

    result = model.predict(origins)

    assert result.loc[0, "seasonal_prediction"] == 22000
    assert result.loc[0, "used_fallback"] == True


def test_seasonal_naive_does_not_use_future_observation(sample_history):
    model = SeasonalNaiveBaseline().fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-10 10:00:00+07:00"],
    })

    result = model.predict(origins)

    assert result.loc[0, "matched_history_at"] <= pd.Timestamp(
        "2024-01-10 10:00:00+07:00"
    ).tz_convert("UTC")


def test_seasonal_naive_can_disable_fallback(sample_history):
    model = SeasonalNaiveBaseline(
        tolerance="1D",
        fallback="raise",
    ).fit(sample_history)

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-20 15:00:00+07:00"],
    })

    with pytest.raises(ValueError, match="No seasonal observation"):
        model.predict(origins)


def test_seasonal_naive_predict_before_fit():
    model = SeasonalNaiveBaseline()

    origins = pd.DataFrame({
        "product_id": ["diesel"],
        "forecast_origin_local": ["2024-01-15 15:00:00+07:00"],
    })

    with pytest.raises(RuntimeError):
        model.predict(origins)



# ============================================================
# HORIZON, COLD-START, EDGE CASES & T21 VALIDATION INTEGRATION TESTS
# ============================================================

def test_baselines_support_horizon_and_cold_start_global_median(sample_history):
    naive = NaivePersistenceBaseline(horizon=2, cold_start_policy="global_median").fit(
        sample_history
    )
    seas = SeasonalNaiveBaseline(horizon=2, cold_start_policy="global_median").fit(
        sample_history
    )

    origins = pd.DataFrame({
        "product_id": ["brand_new_product"],
        "forecast_origin_local": ["2024-01-10 10:00:00+07:00"],
    })
    n_res = naive.predict(origins)
    s_res = seas.predict(origins, origin_time_col="forecast_origin_local")

    expected_median = float(sample_history["price_value"].median())
    assert n_res.loc[0, "naive_prediction"] == expected_median
    assert s_res.loc[0, "seasonal_prediction"] == expected_median
    assert bool(n_res.loc[0, "used_fallback"]) is True
    assert bool(s_res.loc[0, "used_fallback"]) is True
    assert n_res.loc[0, "horizon"] == 2
    assert s_res.loc[0, "horizon"] == 2


def test_baselines_reject_duplicate_and_missing_history_and_handle_empty_origins(sample_history):
    bad_dup = pd.concat([sample_history, sample_history.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="Duplicate"):
        NaivePersistenceBaseline().fit(bad_dup)

    bad_null = sample_history.copy()
    bad_null.loc[0, "price_value"] = None
    with pytest.raises(ValueError, match="missing values"):
        SeasonalNaiveBaseline().fit(bad_null)

    model = NaivePersistenceBaseline().fit(sample_history)
    empty_origins = pd.DataFrame(columns=["product_id", "forecast_origin_local"])
    empty_out = model.predict(empty_origins)
    assert empty_out.empty
    assert {"naive_prediction", "matched_history_at", "used_fallback", "horizon"}.issubset(
        empty_out.columns
    )


def test_t21_baseline_integration_is_reproducible_and_locks_final_test():
    import json
    from pathlib import Path

    from petrotwin.evaluation import evaluate_baselines_on_t21

    current_path = Path("data/processed/current.json")
    if not current_path.is_file():
        pytest.skip("data/processed/current.json not present")

    current = json.loads(current_path.read_text(encoding="utf-8"))
    panel_df = pd.read_csv(Path("data/processed") / current["path"] / "fuel_market_panel.csv")

    res1 = evaluate_baselines_on_t21(panel_df, mode="expanding", dataset_run_id=current["run_id"])
    res2 = evaluate_baselines_on_t21(panel_df, mode="expanding", dataset_run_id=current["run_id"])

    assert res1["metadata"]["final_test_locked"] is True
    assert len(res1["per_fold"]) == 8  # 2 baselines x 4 expanding folds
    pd.testing.assert_frame_equal(res1["per_fold"], res2["per_fold"])
    pd.testing.assert_frame_equal(res1["pooled"], res2["pooled"])
    # Ensure Final Test (2026) was never touched
    assert res1["oof_predictions"]["effective_at_local"].max() < "2026-01-01"
