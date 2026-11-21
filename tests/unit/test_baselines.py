from petrotwin.models.baselines.seasonal_naive import SeasonalNaiveBaseline
import pandas as pd
import pytest

from petrotwin.models.baselines.naive import NaivePersistenceBaseline


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
