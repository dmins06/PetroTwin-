"""Reproducible T25 baseline benchmark runner and error-analysis package."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from petrotwin.evaluation.metrics import (
    aggregate_regression_metrics,
    evaluate_baselines_on_t21,
)

COMECO_ACTIVE_PRODUCTS: tuple[str, ...] = (
    "diesel_0001s_v",
    "diesel_005s_ii",
    "gasoline_e10_ron95_iii",
    "gasoline_e5_ron92_ii",
)


@dataclass
class BaselineBenchmarkResult:
    """Container for T25 benchmark tables, error analysis, and package metadata."""

    run_id: str
    metadata: dict[str, Any]
    expanding_per_fold: pd.DataFrame
    expanding_summary: pd.DataFrame
    expanding_per_product: pd.DataFrame
    rolling_per_fold: pd.DataFrame
    rolling_summary: pd.DataFrame
    comeco_active_summary: pd.DataFrame
    by_quarter: pd.DataFrame
    by_interval_horizon: pd.DataFrame
    top_errors_naive: pd.DataFrame
    oof_predictions: pd.DataFrame


def _load_current_panel(processed_dir: str | Path) -> tuple[pd.DataFrame, str]:
    proc = Path(processed_dir)
    current = json.loads((proc / "current.json").read_text(encoding="utf-8"))
    panel_path = proc / current["path"] / "fuel_market_panel.csv"
    return pd.read_csv(panel_path), str(current["run_id"])


def _compute_interval_days(panel_df: pd.DataFrame) -> pd.DataFrame:
    work = panel_df[["product_id", "effective_at_local"]].copy()
    work["_ts"] = pd.to_datetime(work["effective_at_local"], utc=True, errors="raise")
    work = work.sort_values(["product_id", "_ts"], kind="mergesort")
    prev_ts = work.groupby("product_id", sort=False)["_ts"].shift(1)
    work["days_since_previous_event"] = (work["_ts"] - prev_ts).dt.total_seconds() / 86400.0
    work["effective_at_local"] = work["effective_at_local"].astype(str)
    return work[["product_id", "effective_at_local", "days_since_previous_event"]]


def _bucket_interval_days(days: pd.Series) -> pd.Series:
    vals = pd.to_numeric(days, errors="coerce")
    conditions = [
        vals.notna() & (vals <= 5.5),
        vals.notna() & (vals > 5.5) & (vals <= 8.5),
        vals.notna() & (vals > 8.5),
    ]
    choices = ["1-5d (early/irregular)", "6-8d (standard_7d)", "9d+ (extended_cycle)"]
    return pd.Series(np.select(conditions, choices, default="unknown (cold_start)"), index=days.index)


def _build_mode_summary(eval_res: dict[str, Any], mode: str) -> pd.DataFrame:
    per_fold = eval_res["per_fold"]
    pooled = eval_res["pooled"]
    rows: list[dict[str, Any]] = []
    for model_name, grp in per_fold.groupby("model_name", sort=False):
        p_row = pooled[pooled["model_name"] == model_name].iloc[0]
        rows.append(
            {
                "model_name": model_name,
                "mode": mode,
                "n_folds": len(grp),
                "mean_mae": round(float(grp["mae"].mean()), 4),
                "std_mae": round(float(grp["mae"].std(ddof=1)), 4) if len(grp) > 1 else 0.0,
                "mean_rmse": round(float(grp["rmse"].mean()), 4),
                "std_rmse": round(float(grp["rmse"].std(ddof=1)), 4) if len(grp) > 1 else 0.0,
                "mean_fallback_rate_pct": round(float(grp["fallback_rate_pct"].mean()), 2),
                "pooled_valid_n": int(p_row["valid_n"]),
                "pooled_mae": p_row["mae"],
                "pooled_rmse": p_row["rmse"],
                "pooled_mape": p_row["mape"],
                "directional_valid_n": int(p_row["directional_valid_n"]),
                "directional_accuracy": p_row["directional_accuracy"],
            }
        )
    return pd.DataFrame(rows)


def run_baseline_benchmark(
    processed_dir: str | Path = "data/processed",
    validation_config_path: str | Path = "configs/validation.yaml",
    baseline_config_path: str | Path = "configs/baseline.yaml",
    output_dir: str | Path | None = None,
) -> BaselineBenchmarkResult:
    """Run T25 baseline validation, error analysis, and fair-comparison packaging."""
    panel_df, run_id = _load_current_panel(processed_dir)

    exp_res = evaluate_baselines_on_t21(
        panel_df,
        validation_config_path=validation_config_path,
        baseline_config_path=baseline_config_path,
        mode="expanding",
        dataset_run_id=run_id,
    )
    rol_res = evaluate_baselines_on_t21(
        panel_df,
        validation_config_path=validation_config_path,
        baseline_config_path=baseline_config_path,
        mode="rolling",
        dataset_run_id=run_id,
    )

    exp_summary = _build_mode_summary(exp_res, "expanding")
    rol_summary = _build_mode_summary(rol_res, "rolling")

    # Enrich expanding OOF predictions with calendar quarter & adjustment-interval horizon
    intervals = _compute_interval_days(panel_df)
    oof = exp_res["oof_predictions"].merge(
        intervals,
        on=["product_id", "effective_at_local"],
        how="left",
        validate="many_to_one",
    )
    eff_local = pd.to_datetime(oof["effective_at_local"]).dt.tz_localize(None)
    oof["effective_date_local"] = eff_local.dt.strftime("%Y-%m-%d")
    oof["year_quarter"] = eff_local.dt.to_period("Q").astype(str)
    oof["interval_horizon_bucket"] = _bucket_interval_days(oof["days_since_previous_event"])
    oof["signed_error"] = oof["y_pred"] - oof["y_true"]
    oof["abs_error"] = oof["signed_error"].abs()

    comeco_oof = oof[oof["product_id"].isin(COMECO_ACTIVE_PRODUCTS)]
    comeco_summary = aggregate_regression_metrics(
        comeco_oof, group_cols=["model_name", "horizon"], y_prev_col="y_prev"
    )
    by_quarter = aggregate_regression_metrics(
        oof, group_cols=["model_name", "year_quarter"], y_prev_col="y_prev"
    )
    by_interval = aggregate_regression_metrics(
        oof, group_cols=["model_name", "horizon", "interval_horizon_bucket"], y_prev_col="y_prev"
    )

    top_errors = (
        oof[oof["model_name"] == "naive_persistence"]
        .sort_values("abs_error", ascending=False)
        .head(5)[
            [
                "fold_id",
                "effective_date_local",
                "product_id",
                "y_true",
                "y_pred",
                "signed_error",
                "abs_error",
                "used_fallback",
                "interval_horizon_bucket",
            ]
        ]
        .reset_index(drop=True)
    )

    naive_oof = oof[oof["model_name"] == "naive_persistence"]
    meta = {
        "schema_version": "petrotwin-benchmark-v1",
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "dataset_run_id": run_id,
        "validation_config": str(validation_config_path),
        "baseline_config": str(baseline_config_path),
        "split_strategy": {
            "primary": "expanding_window (4 folds, 2024-01-01..2025-12-31)",
            "supplementary": "rolling_window (730d lookback, 4 folds, 2024-01-01..2025-12-31)",
            "final_test_policy": "locked (2026-01-01..2026-11-12)",
        },
        "input_data_summary": {
            "processed_dir": str(processed_dir),
            "total_panel_rows": len(panel_df),
            "validation_observations": len(naive_oof),
            "directional_valid_observations": int(exp_summary["directional_valid_n"].iloc[0]),
            "cold_start_observations": int(naive_oof["used_fallback"].sum()),
        },
        "final_test_locked": True,
        "expanding_naive_seasonal_overlap_pct": exp_res["metadata"]["naive_seasonal_overlap_pct"],
        "rolling_naive_seasonal_overlap_pct": rol_res["metadata"]["naive_seasonal_overlap_pct"],
    }

    result = BaselineBenchmarkResult(
        run_id=run_id,
        metadata=meta,
        expanding_per_fold=exp_res["per_fold"],
        expanding_summary=exp_summary,
        expanding_per_product=exp_res["per_product"],
        rolling_per_fold=rol_res["per_fold"],
        rolling_summary=rol_summary,
        comeco_active_summary=comeco_summary,
        by_quarter=by_quarter,
        by_interval_horizon=by_interval,
        top_errors_naive=top_errors,
        oof_predictions=oof,
    )

    if output_dir is not None:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        result.expanding_per_fold.to_csv(out_path / "expanding_per_fold.csv", index=False)
        result.expanding_summary.to_csv(out_path / "expanding_summary.csv", index=False)
        result.expanding_per_product.to_csv(out_path / "expanding_per_product.csv", index=False)
        result.rolling_per_fold.to_csv(out_path / "rolling_per_fold.csv", index=False)
        result.rolling_summary.to_csv(out_path / "rolling_summary.csv", index=False)
        result.comeco_active_summary.to_csv(out_path / "comeco_active_summary.csv", index=False)
        result.by_quarter.to_csv(out_path / "error_by_quarter.csv", index=False)
        result.by_interval_horizon.to_csv(out_path / "error_by_interval_horizon.csv", index=False)
        result.top_errors_naive.to_csv(out_path / "top_errors_naive.csv", index=False)
        result.oof_predictions.to_csv(out_path / "oof_predictions_expanding.csv", index=False)
        (out_path / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PetroTwin T25 baseline benchmark.")
    parser.add_argument("--processed-dir", default="data/processed")
    parser.add_argument("--validation-config", default="configs/validation.yaml")
    parser.add_argument("--baseline-config", default="configs/baseline.yaml")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    res = run_baseline_benchmark(
        processed_dir=args.processed_dir,
        validation_config_path=args.validation_config,
        baseline_config_path=args.baseline_config,
        output_dir=args.output_dir,
    )
    print("METADATA:", json.dumps(res.metadata))
    print("\n=== EXPANDING PER FOLD ===")
    print(
        res.expanding_per_fold[
            ["model_name", "fold_id", "valid_n", "mae", "rmse", "mape", "fallback_rate_pct"]
        ].to_string(index=False)
    )
    print("\n=== EXPANDING SUMMARY ===")
    print(res.expanding_summary.to_string(index=False))
    print("\n=== ROLLING SUMMARY ===")
    print(res.rolling_summary.to_string(index=False))
    print("\n=== COMECO ACTIVE 4 SUMMARY ===")
    print(
        res.comeco_active_summary[
            ["model_name", "valid_n", "mae", "rmse", "mape", "directional_valid_n", "directional_accuracy"]
        ].to_string(index=False)
    )
    print("\n=== ERROR BY QUARTER (NAIVE) ===")
    q_naive = res.by_quarter[res.by_quarter["model_name"] == "naive_persistence"]
    print(q_naive[["year_quarter", "valid_n", "mae", "rmse", "mape"]].to_string(index=False))
    print("\n=== ERROR BY INTERVAL HORIZON ===")
    print(
        res.by_interval_horizon[
            ["model_name", "interval_horizon_bucket", "valid_n", "mae", "rmse", "mape"]
        ].to_string(index=False)
    )
    print("\n=== TOP 5 ERRORS (NAIVE) ===")
    print(res.top_errors_naive.to_string(index=False))


if __name__ == "__main__":
    main()
