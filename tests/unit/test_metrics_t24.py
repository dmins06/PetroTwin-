"""Unit tests for PetroTwin T24 price regression & BOG probability/classification metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from petrotwin.evaluation import (
    aggregate_regression_metrics,
    compute_bog_metrics,
    compute_regression_metrics,
)


def test_regression_metrics_handle_zero_actuals_nan_inf_and_empty():
    # Standard valid case + directional accuracy
    res = compute_regression_metrics(
        y_true=[20000.0, 21000.0],
        y_pred=[20200.0, 20800.0],
        y_prev=[19500.0, 20000.0],
    )
    assert res["status"] == "ok"
    assert res["mae"] == pytest.approx(200.0)
    assert res["rmse"] == pytest.approx(200.0)
    assert res["directional_accuracy"] == pytest.approx(100.0)

    # Zero actual excluded from MAPE without epsilon replacement
    res_zero = compute_regression_metrics(
        y_true=[0.0, 20000.0, np.nan, np.inf],
        y_pred=[100.0, 20200.0, 20000.0, 20000.0],
    )
    assert res_zero["valid_n"] == 2
    assert res_zero["invalid_count"] == 2
    assert res_zero["mape_valid_n"] == 1
    assert res_zero["mape_excluded_zero_count"] == 1
    assert res_zero["mape"] == pytest.approx(1.0)

    # All-zero actuals -> MAPE is None (never 0.0 or inf)
    res_all_zero = compute_regression_metrics(y_true=[0.0, 0.0], y_pred=[10.0, 20.0])
    assert res_all_zero["mape"] is None
    assert res_all_zero["mape_excluded_zero_count"] == 2

    # Empty or all-NaN -> status='not_evaluable'
    res_empty = compute_regression_metrics(y_true=[], y_pred=[])
    assert res_empty["status"] == "not_evaluable"
    assert res_empty["mae"] is None


def test_bog_metrics_probability_clipping_and_validation():
    # Probabilities at exact 0.0 and 1.0 must clip safely in Log-loss (no inf/NaN)
    y_true = [0, 1, 0, 1]
    y_prob = [0.0, 1.0, 1.0, 0.0]  # 2 perfect, 2 maximally wrong
    res = compute_bog_metrics(y_true=y_true, y_prob=y_prob, threshold=0.5, positive_class=1)
    assert res["status"] == "provisional_missing_class"  # class 2 absent
    assert res["brier_score"] == pytest.approx(0.5)
    assert res["log_loss"] is not None and np.isfinite(res["log_loss"])
    assert res["binary_f1_positive"] == pytest.approx(0.5)
    assert res["per_class"]["CONTRIBUTE"]["support"] == 0
    assert res["per_class"]["CONTRIBUTE"]["f1"] is None

    # Multiclass probabilities
    probs_3 = np.array([
        [0.8, 0.1, 0.1],
        [0.1, 0.8, 0.1],
        [0.1, 0.2, 0.7],
    ])
    res_mc = compute_bog_metrics(y_true=[0, 1, 2], y_prob=probs_3)
    assert res_mc["status"] == "ok"
    assert res_mc["accuracy"] == pytest.approx(1.0)
    assert res_mc["f1_macro"] == pytest.approx(1.0)
    assert res_mc["brier_score"] < 0.15
    assert res_mc["confusion_matrix"] == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]

    # Out-of-bounds or non-unit-sum probabilities must raise ValueError
    with pytest.raises(ValueError, match="must lie in"):
        compute_bog_metrics(y_true=[0, 1], y_prob=[-0.1, 0.5])
    with pytest.raises(ValueError, match="sum to 1.0"):
        compute_bog_metrics(y_true=[0], y_prob=[[0.5, 0.5, 0.5]])


def test_bog_metrics_null_labels_single_class_and_imbalance():
    # All NULL -> not_evaluable (never coerced to NO_ACTION)
    res_null = compute_bog_metrics(y_true=[None, np.nan], y_pred=[0, 0])
    assert res_null["status"] == "not_evaluable"
    assert res_null["null_label_count"] == 2
    assert res_null["valid_n"] == 0

    # Single class (only NO_ACTION) -> undefined_single_class
    res_single = compute_bog_metrics(y_true=[0, 0, None], y_pred=[0, 0, 0])
    assert res_single["status"] == "undefined_single_class"
    assert res_single["valid_n"] == 2
    assert res_single["null_label_count"] == 1
    assert res_single["f1_macro"] is None

    # Imbalanced labels (9 NO_ACTION vs 1 SPEND)
    res_imb = compute_bog_metrics(
        y_true=[0] * 9 + [1],
        y_prob=[0.1] * 9 + [0.8],
        threshold=0.5,
        positive_class=1,
    )
    assert res_imb["imbalance_ratio"] == pytest.approx(9.0)
    assert res_imb["binary_f1_positive"] == pytest.approx(1.0)


def test_aggregate_regression_metrics_rejects_mixed_units():
    df = pd.DataFrame({
        "model_name": ["m1", "m1"],
        "horizon": [1, 1],
        "unit_code": ["VND/liter", "VND/kg"],
        "y_true": [20000.0, 15000.0],
        "y_pred": [20100.0, 15100.0],
    })
    with pytest.raises(ValueError, match="mixed units"):
        aggregate_regression_metrics(df, group_cols=["model_name", "horizon"])

    # Including unit_code in group_cols succeeds cleanly
    agg = aggregate_regression_metrics(df, group_cols=["model_name", "horizon", "unit_code"])
    assert len(agg) == 2
