
from pathlib import Path

import pandas as pd
import numpy as np

from petrotwin.models.baselines.naive import NaivePersistenceBaseline
from petrotwin.models.baselines.seasonal_naive import SeasonalNaiveBaseline


FOLDS = [
    ("Fold 1", "2024-01-01", "2024-07-01", 105),
    ("Fold 2", "2024-07-01", "2025-01-01", 118),
    ("Fold 3", "2025-01-01", "2025-07-01", 130),
    ("Fold 4", "2025-07-01", "2026-01-01", 163),
]


ROLLING_FOLDS = [
    ("Fold 1", "2022-01-01", "2024-01-01", "2024-07-01", 284, 105),
    ("Fold 2", "2022-07-02", "2024-07-01", "2025-01-01", 321, 118),
    ("Fold 3", "2023-01-02", "2025-01-01", "2025-07-01", 367, 130),
    ("Fold 4", "2023-07-02", "2025-07-01", "2026-01-01", 429, 163),
]



def load_data():
    files = sorted(
        Path("data/processed/runs").glob("*/fuel_market_panel.csv")
    )

    if len(files) != 1:
        raise RuntimeError(
            f"Expected exactly one fuel_market_panel.csv, found {len(files)}."
        )

    data = pd.read_csv(files[0])

    required = {"product_id", "effective_at_local", "price_value"}
    missing = required - set(data.columns)

    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    data["effective_at_local"] = pd.to_datetime(
    data["effective_at_local"], utc=True
).dt.tz_convert("Asia/Ho_Chi_Minh")

    data["price_value"] = pd.to_numeric(
        data["price_value"], errors="raise"
    )

    if data[list(required)].isna().any().any():
        raise ValueError("Missing values in required columns.")

    return data


def validate_folds(data):
    print("\nBASELINE BENCHMARK - DATA VALIDATION")
    print("-" * 55)

    for name, start, end, expected in FOLDS:
        start_time = pd.Timestamp(start, tz="Asia/Ho_Chi_Minh")
        end_time = pd.Timestamp(end, tz="Asia/Ho_Chi_Minh")

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ]

        actual = len(validation)
        status = "PASS" if actual == expected else "FAIL"

        print(
            f"{name}: expected={expected}, "
            f"actual={actual}, status={status}"
        )

        if actual != expected:
            raise ValueError(f"{name} does not match T21 configuration.")


def validate_expanding_window(data):
    print("\nEXPANDING WINDOW VALIDATION")
    print("-" * 55)

    initial_start = pd.Timestamp(
        "2018-08-22", tz="Asia/Ho_Chi_Minh"
    )

    expected_train_counts = [632, 737, 855, 985]

    for i, (name, start, end, expected_val) in enumerate(FOLDS):
        start_time = pd.Timestamp(
            start, tz="Asia/Ho_Chi_Minh"
        )
        end_time = pd.Timestamp(
            end, tz="Asia/Ho_Chi_Minh"
        )

        train = data[
            (data["effective_at_local"] >= initial_start)
            & (data["effective_at_local"] < start_time)
        ]

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ]

        expected_train = expected_train_counts[i]

        passed = (
            len(train) == expected_train
            and len(validation) == expected_val
        )

        print(
            f"{name}: train={len(train)}/{expected_train}, "
            f"validation={len(validation)}/{expected_val}, "
            f"status={'PASS' if passed else 'FAIL'}"
        )

        if not passed:
            raise ValueError(
                f"{name} expanding window does not match T21."
            )


def check_product_history(data):
    print("\nPRODUCT HISTORY CHECK")
    print("-" * 55)

    for name, start, end, _ in FOLDS:
        start_time = pd.Timestamp(
            start, tz="Asia/Ho_Chi_Minh"
        )
        end_time = pd.Timestamp(
            end, tz="Asia/Ho_Chi_Minh"
        )

        train = data[
            data["effective_at_local"] < start_time
        ]

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ]

        train_products = set(train["product_id"])
        validation_products = set(validation["product_id"])

        missing_products = sorted(
            validation_products - train_products
        )

        print(f"\n{name}")
        print(f"Train products: {len(train_products)}")
        print(f"Validation products: {len(validation_products)}")
        print(f"Products without prior history: {missing_products}")


def check_cold_start_observations(data):
    print("\nCOLD-START OBSERVATION CHECK")
    print("-" * 55)

    for name, start, end, _ in FOLDS:
        start_time = pd.Timestamp(
            start, tz="Asia/Ho_Chi_Minh"
        )
        end_time = pd.Timestamp(
            end, tz="Asia/Ho_Chi_Minh"
        )

        train = data[
            data["effective_at_local"] < start_time
        ]

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ]

        missing_products = (
            set(validation["product_id"])
            - set(train["product_id"])
        )

        affected = validation[
            validation["product_id"].isin(missing_products)
        ]

        print(f"\n{name}")
        print(f"Validation observations: {len(validation)}")
        print(f"Observations from new products: {len(affected)}")

        if not affected.empty:
            print(
                affected.groupby("product_id")
                .size()
                .to_string()
            )


def inspect_new_product_timeline(data):
    print("\nNEW PRODUCT TIMELINE")
    print("-" * 55)

    for name, start, end, _ in FOLDS:
        start_time = pd.Timestamp(
            start, tz="Asia/Ho_Chi_Minh"
        )
        end_time = pd.Timestamp(
            end, tz="Asia/Ho_Chi_Minh"
        )

        train = data[data["effective_at_local"] < start_time]

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ]

        new_products = (
            set(validation["product_id"])
            - set(train["product_id"])
        )

        for product in sorted(new_products):
            rows = validation[
                validation["product_id"] == product
            ].sort_values("effective_at_local")

            print(f"\n{name}: {product}")
            print(f"Total observations: {len(rows)}")
            print(
                "First appearance:",
                rows.iloc[0]["effective_at_local"]
            )
            print(
                "Last appearance:",
                rows.iloc[-1]["effective_at_local"]
            )
            print(
                "Distinct adjustment events:",
                rows["effective_at_local"].nunique()
            )


def calculate_metrics(results, prediction_col):
    valid = results.dropna(subset=[prediction_col]).copy()

    if valid.empty:
        return {
            "n": 0,
            "mae": np.nan,
            "rmse": np.nan,
            "mape": np.nan,
        }

    actual = valid["actual_price"].to_numpy(dtype=float)
    predicted = valid[prediction_col].to_numpy(dtype=float)

    errors = actual - predicted

    mae = np.mean(np.abs(errors))
    rmse = np.sqrt(np.mean(errors ** 2))

    nonzero = actual != 0
    mape = (
        np.mean(
            np.abs(errors[nonzero] / actual[nonzero])
        ) * 100
        if nonzero.any()
        else np.nan
    )

    return {
        "n": len(valid),
        "mae": mae,
        "rmse": rmse,
        "mape": mape,
    }


def run_simulated_benchmark(data):
    print("\nSIMULATED BASELINE BENCHMARK")
    print("-" * 55)
    print("Origin policy: 1 second before target effective time")
    print("Status: SIMULATED - NOT OFFICIAL T25 BACKTEST")

    data = data.sort_values(
        ["effective_at_local", "product_id"]
    ).reset_index(drop=True)

    if data.duplicated(
        ["product_id", "effective_at_local"]
    ).any():
        raise ValueError(
            "Duplicate product/effective timestamp detected."
        )

    all_results = []

    for fold_name, start, end, _ in FOLDS:
        start_time = pd.Timestamp(
            start, tz="Asia/Ho_Chi_Minh"
        )
        end_time = pd.Timestamp(
            end, tz="Asia/Ho_Chi_Minh"
        )

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ].copy()

        print(f"\nProcessing {fold_name}...")

        for _, target in validation.iterrows():
            product = target["product_id"]
            target_time = target["effective_at_local"]

            origin_time = target_time - pd.Timedelta(
                seconds=1
            )

            history = data[
                data["effective_at_local"] < origin_time
            ].copy()

            product_history = history[
                history["product_id"] == product
            ]

            result = {
                "fold": fold_name,
                "product_id": product,
                "target_time": target_time,
                "forecast_origin_local": origin_time,
                "actual_price": float(target["price_value"]),
                "naive_prediction": np.nan,
                "seasonal_prediction": np.nan,
                "seasonal_fallback": None,
                "status": "cold_start",
            }

            if product_history.empty:
                all_results.append(result)
                continue

            origins = pd.DataFrame({
                "product_id": [product],
                "forecast_origin_local": [origin_time],
            })

            naive = NaivePersistenceBaseline()
            naive.fit(product_history)

            naive_result = naive.predict(origins)

            seasonal = SeasonalNaiveBaseline()
            seasonal.fit(product_history)

            seasonal_result = seasonal.predict(origins)

            result["naive_prediction"] = float(
                naive_result.iloc[0]["naive_prediction"]
            )

            result["seasonal_prediction"] = float(
                seasonal_result.iloc[0]["seasonal_prediction"]
            )

            result["seasonal_fallback"] = bool(
                seasonal_result.iloc[0]["used_fallback"]
            )

            result["status"] = "predicted"

            all_results.append(result)

    results = pd.DataFrame(all_results)

    output_dir = Path("reports/baseline_simulated")
    output_dir.mkdir(parents=True, exist_ok=True)

    results.to_csv(
        output_dir / "predictions.csv",
        index=False,
    )

    summary = []

    for fold_name, _, _, _ in FOLDS:
        fold_results = results[
            results["fold"] == fold_name
        ]

        for model, prediction_col in [
            ("naive", "naive_prediction"),
            ("seasonal_naive", "seasonal_prediction"),
        ]:
            metrics = calculate_metrics(
                fold_results, prediction_col
            )

            summary.append({
                "fold": fold_name,
                "model": model,
                **metrics,
            })

    summary_df = pd.DataFrame(summary)

    summary_df.to_csv(
        output_dir / "metrics_by_fold.csv",
        index=False,
    )

    print("\nMETRICS BY FOLD")
    print("-" * 55)
    print(summary_df.to_string(index=False))

    print("\nOVERALL METRICS")
    print("-" * 55)

    for model, prediction_col in [
        ("naive", "naive_prediction"),
        ("seasonal_naive", "seasonal_prediction"),
    ]:
        metrics = calculate_metrics(
            results, prediction_col
        )

        print(
            f"{model}: "
            f"N={metrics['n']}, "
            f"MAE={metrics['mae']:.2f}, "
            f"RMSE={metrics['rmse']:.2f}, "
            f"MAPE={metrics['mape']:.2f}%"
        )

    print("\nCOLD START")
    print("-" * 55)

    cold_start = results[
        results["status"] == "cold_start"
    ]

    print(f"Total: {len(cold_start)}")

    if not cold_start.empty:
        print(
            cold_start[
                ["fold", "product_id", "target_time"]
            ].to_string(index=False)
        )

    print("\nSEASONAL FALLBACK")
    print("-" * 55)

    predicted = results[
        results["status"] == "predicted"
    ]

    fallback_count = (
        predicted["seasonal_fallback"]
        .fillna(False)
        .astype(bool)
        .sum()
    )

    print(
        f"Fallback count: {fallback_count}/{len(predicted)}"
    )

    print("\nResults saved to:", output_dir)

    return results, summary_df


def validate_rolling_window(data):
    print("\nROLLING WINDOW VALIDATION")
    print("-" * 55)

    for name, cutoff, start, end, expected_train, expected_val in ROLLING_FOLDS:
        cutoff_time = pd.Timestamp(
            cutoff, tz="Asia/Ho_Chi_Minh"
        )
        start_time = pd.Timestamp(
            start, tz="Asia/Ho_Chi_Minh"
        )
        end_time = pd.Timestamp(
            end, tz="Asia/Ho_Chi_Minh"
        )

        train = data[
            (data["effective_at_local"] >= cutoff_time)
            & (data["effective_at_local"] < start_time)
        ]

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ]

        passed = (
            len(train) == expected_train
            and len(validation) == expected_val
        )

        print(
            f"{name}: train={len(train)}/{expected_train}, "
            f"validation={len(validation)}/{expected_val}, "
            f"status={'PASS' if passed else 'FAIL'}"
        )

        if not passed:
            raise ValueError(
                f"{name} rolling window does not match T21."
            )


def run_rolling_benchmark(data):
    print("\nSIMULATED ROLLING BASELINE BENCHMARK")
    print("-" * 55)

    all_results = []

    for name, cutoff, start, end, _, _ in ROLLING_FOLDS:
        start_time = pd.Timestamp(start, tz="Asia/Ho_Chi_Minh")
        end_time = pd.Timestamp(end, tz="Asia/Ho_Chi_Minh")

        validation = data[
            (data["effective_at_local"] >= start_time)
            & (data["effective_at_local"] < end_time)
        ].sort_values(["effective_at_local", "product_id"])

        for _, target in validation.iterrows():
            origin_time = (
                target["effective_at_local"]
                - pd.Timedelta(seconds=1)
            )

            # Moving 730-day window, ending at each forecast origin
            history_start = origin_time - pd.Timedelta(days=730)

            history = data[
                (data["effective_at_local"] >= history_start)
                & (data["effective_at_local"] < origin_time)
                & (data["product_id"] == target["product_id"])
            ].copy()

            result = {
                "fold": name,
                "product_id": target["product_id"],
                "target_time": target["effective_at_local"],
                "forecast_origin_local": origin_time,
                "actual_price": float(target["price_value"]),
                "naive_prediction": float("nan"),
                "seasonal_prediction": float("nan"),
                "seasonal_fallback": None,
                "status": "cold_start",
            }

            if history.empty:
                all_results.append(result)
                continue

            origins = pd.DataFrame({
                "product_id": [target["product_id"]],
                "forecast_origin_local": [origin_time],
            })

            naive = NaivePersistenceBaseline()
            naive.fit(history)
            naive_result = naive.predict(origins)

            seasonal = SeasonalNaiveBaseline()
            seasonal.fit(history)
            seasonal_result = seasonal.predict(origins)

            result["naive_prediction"] = float(
                naive_result.iloc[0]["naive_prediction"]
            )
            result["seasonal_prediction"] = float(
                seasonal_result.iloc[0]["seasonal_prediction"]
            )
            result["seasonal_fallback"] = bool(
                seasonal_result.iloc[0]["used_fallback"]
            )
            result["status"] = "predicted"

            all_results.append(result)

    results = pd.DataFrame(all_results)

    output_dir = Path("reports/baseline_simulated")
    output_dir.mkdir(parents=True, exist_ok=True)

    results.to_csv(
        output_dir / "rolling_predictions.csv",
        index=False,
    )

    summary = []

    for fold_name, *_ in ROLLING_FOLDS:
        fold_results = results[results["fold"] == fold_name]

        for model, prediction_col in [
            ("naive", "naive_prediction"),
            ("seasonal_naive", "seasonal_prediction"),
        ]:
            summary.append({
                "fold": fold_name,
                "model": model,
                **calculate_metrics(fold_results, prediction_col),
            })

    summary_df = pd.DataFrame(summary)
    summary_df.to_csv(
        output_dir / "rolling_metrics_by_fold.csv",
        index=False,
    )

    print("\nROLLING METRICS BY FOLD")
    print(summary_df.to_string(index=False))

    print("\nROLLING OVERALL METRICS")

    for model, prediction_col in [
        ("naive", "naive_prediction"),
        ("seasonal_naive", "seasonal_prediction"),
    ]:
        metrics = calculate_metrics(results, prediction_col)
        print(
            f"{model}: N={metrics['n']}, "
            f"MAE={metrics['mae']:.2f}, "
            f"RMSE={metrics['rmse']:.2f}, "
            f"MAPE={metrics['mape']:.2f}%"
        )

    print("\nROLLING COLD START")
    print(results[results["status"] == "cold_start"][
        ["fold", "product_id", "target_time"]
    ].to_string(index=False))

    return results, summary_df


def export_product_metrics():
    output_dir = Path("reports/baseline_simulated")

    datasets = {
        "expanding": output_dir / "predictions.csv",
        "rolling": output_dir / "rolling_predictions.csv",
    }

    all_metrics = []

    for window_type, file_path in datasets.items():
        results = pd.read_csv(file_path)

        for (fold, product), group in results.groupby(
            ["fold", "product_id"]
        ):
            for model, prediction_col in [
                ("naive", "naive_prediction"),
                ("seasonal_naive", "seasonal_prediction"),
            ]:
                metrics = calculate_metrics(
                    group, prediction_col
                )

                all_metrics.append({
                    "window_type": window_type,
                    "fold": fold,
                    "product_id": product,
                    "model": model,
                    "total_observations": len(group),
                    "cold_start_count": (
                        group["status"] == "cold_start"
                    ).sum(),
                    **metrics,
                })

    metrics_df = pd.DataFrame(all_metrics)

    metrics_df.to_csv(
        output_dir / "metrics_by_product.csv",
        index=False,
    )

    print("\nPRODUCT METRICS")
    print("-" * 55)
    print(metrics_df.to_string(index=False))

    print("\nSaved:", output_dir / "metrics_by_product.csv")

    return metrics_df



def main():
    data = load_data()

    validate_folds(data)
    validate_expanding_window(data)
    validate_rolling_window(data)

    check_product_history(data)
    check_cold_start_observations(data)
    inspect_new_product_timeline(data)

    run_simulated_benchmark(data)
    run_rolling_benchmark(data)

    export_product_metrics()

    print("\nAll simulated baseline benchmarks completed.")
    print("Official T25 benchmark is still pending.")



if __name__ == "__main__":
    main()
