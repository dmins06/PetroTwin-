"""Unit tests for PetroTwin T25 baseline benchmark & error-analysis package."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from petrotwin.evaluation import run_baseline_benchmark


def test_run_baseline_benchmark_exports_reproducible_package(tmp_path: Path):
    if not Path("data/processed/current.json").is_file():
        pytest.skip("data/processed/current.json not present")

    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"

    res1 = run_baseline_benchmark(output_dir=out1)
    res2 = run_baseline_benchmark(output_dir=out2)

    assert res1.metadata["final_test_locked"] is True
    assert len(res1.expanding_per_fold) == 8
    assert len(res1.rolling_per_fold) == 8
    assert len(res1.top_errors_naive) == 5
    assert not res1.by_quarter.empty
    assert not res1.by_interval_horizon.empty
    assert not res1.comeco_active_summary.empty

    # Bit-for-bit reproducibility
    pd.testing.assert_frame_equal(res1.expanding_summary, res2.expanding_summary)
    pd.testing.assert_frame_equal(res1.oof_predictions, res2.oof_predictions)

    expected_files = (
        "expanding_per_fold.csv",
        "expanding_summary.csv",
        "expanding_per_product.csv",
        "rolling_per_fold.csv",
        "rolling_summary.csv",
        "comeco_active_summary.csv",
        "error_by_quarter.csv",
        "error_by_interval_horizon.csv",
        "top_errors_naive.csv",
        "oof_predictions_expanding.csv",
        "metadata.json",
    )
    for fname in expected_files:
        assert (out1 / fname).is_file(), f"Missing benchmark artifact: {fname}"

    meta = json.loads((out1 / "metadata.json").read_text(encoding="utf-8"))
    assert meta["dataset_run_id"] == res1.run_id
    assert meta["final_test_locked"] is True
    assert "generated_at_utc" in meta
    assert "split_strategy" in meta
    assert meta["input_data_summary"]["validation_observations"] == 516
    assert meta["input_data_summary"]["directional_valid_observations"] == 513
    assert 0.0 <= meta["expanding_naive_seasonal_overlap_pct"] <= 100.0
