"""Regression (price) and BOG probability/classification evaluation metrics (T24)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

from petrotwin.evaluation.splits import iter_validation_splits
from petrotwin.models.baselines.naive import NaivePersistenceBaseline
from petrotwin.models.baselines.seasonal_naive import SeasonalNaiveBaseline

BOG_CLASSES: tuple[int, int, int] = (0, 1, 2)
BOG_CLASS_NAMES: dict[int, str] = {
    0: "NO_ACTION",
    1: "SPEND",
    2: "CONTRIBUTE",
}
PROB_CLIP_EPS: float = 1e-15


def _round_or_none(value: float | None, decimals: int) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), decimals)


def compute_regression_metrics(
    y_true: np.ndarray | pd.Series | list[Any],
    y_pred: np.ndarray | pd.Series | list[Any],
    y_prev: np.ndarray | pd.Series | list[Any] | None = None,
    unit: str = "VND/liter",
    decimals: int = 4,
) -> dict[str, Any]:
    """Compute MAE, RMSE, zero-safe MAPE (%), and Directional Accuracy (%).

    Safely filters out NaN/None/Inf pairs and empty inputs without raising,
    returning status='not_evaluable' and None metrics when no valid pairs remain.
    Zero actuals are excluded from MAPE without epsilon replacement.
    """
    yt_raw = np.asarray(pd.to_numeric(pd.Series(list(y_true)), errors="coerce"), dtype=float)
    yp_raw = np.asarray(pd.to_numeric(pd.Series(list(y_pred)), errors="coerce"), dtype=float)

    if yt_raw.size != yp_raw.size:
        raise ValueError(
            f"Length mismatch: y_true has {yt_raw.size} items, y_pred has {yp_raw.size} items."
        )

    total_n = int(yt_raw.size)
    valid_mask = np.isfinite(yt_raw) & np.isfinite(yp_raw)
    valid_n = int(valid_mask.sum())
    invalid_n = total_n - valid_n

    if valid_n == 0:
        return {
            "status": "not_evaluable",
            "unit": unit,
            "total_n": total_n,
            "valid_n": 0,
            "invalid_count": invalid_n,
            "mae": None,
            "rmse": None,
            "mape": None,
            "mape_valid_n": 0,
            "mape_excluded_zero_count": 0,
            "directional_accuracy": None,
            "directional_valid_n": 0,
        }

    yt = yt_raw[valid_mask]
    yp = yp_raw[valid_mask]
    err = yp - yt

    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err**2)))

    nonzero_mask = yt != 0.0
    mape_valid_n = int(nonzero_mask.sum())
    mape_zero_count = int((~nonzero_mask).sum())
    mape: float | None = (
        float(np.mean(np.abs(err[nonzero_mask] / yt[nonzero_mask])) * 100.0)
        if mape_valid_n > 0
        else None
    )

    dir_acc: float | None = None
    dir_valid_n = 0
    if y_prev is not None:
        yprev_raw = np.asarray(
            pd.to_numeric(pd.Series(list(y_prev)), errors="coerce"), dtype=float
        )
        if yprev_raw.size != total_n:
            raise ValueError(
                f"Length mismatch: y_prev has {yprev_raw.size} items, expected {total_n}."
            )
        dir_mask = valid_mask & np.isfinite(yprev_raw)
        dir_valid_n = int(dir_mask.sum())
        if dir_valid_n > 0:
            true_dir = np.sign(yt_raw[dir_mask] - yprev_raw[dir_mask])
            pred_dir = np.sign(yp_raw[dir_mask] - yprev_raw[dir_mask])
            dir_acc = float(np.mean(true_dir == pred_dir) * 100.0)

    return {
        "status": "ok",
        "unit": unit,
        "total_n": total_n,
        "valid_n": valid_n,
        "invalid_count": invalid_n,
        "mae": _round_or_none(mae, decimals),
        "rmse": _round_or_none(rmse, decimals),
        "mape": _round_or_none(mape, decimals),
        "mape_valid_n": mape_valid_n,
        "mape_excluded_zero_count": mape_zero_count,
        "directional_accuracy": _round_or_none(dir_acc, decimals),
        "directional_valid_n": dir_valid_n,
    }


def _validate_probabilities(
    y_prob: np.ndarray,
    n_samples: int,
) -> tuple[np.ndarray, Literal["binary", "multiclass"]]:
    arr = np.asarray(y_prob, dtype=float)
    if arr.ndim == 1:
        if arr.shape[0] != n_samples:
            raise ValueError(f"Expected {n_samples} probability rows, got {arr.shape[0]}.")
        if not np.all(np.isfinite(arr)):
            raise ValueError("Probabilities contain NaN or Inf values.")
        if np.any((arr < 0.0) | (arr > 1.0)):
            raise ValueError("Binary probabilities must lie in [0, 1].")
        return arr, "binary"

    if arr.ndim == 2 and arr.shape == (n_samples, len(BOG_CLASSES)):
        if not np.all(np.isfinite(arr)):
            raise ValueError("Probability matrix contains NaN or Inf values.")
        if np.any((arr < 0.0) | (arr > 1.0)):
            raise ValueError("Multiclass probabilities must lie in [0, 1].")
        row_sums = arr.sum(axis=1)
        if not np.allclose(row_sums, 1.0, atol=1e-5):
            raise ValueError("Multiclass probability rows must sum to 1.0.")
        return arr, "multiclass"

    raise ValueError(
        f"y_prob must have shape ({n_samples},) for binary or ({n_samples}, 3) for multiclass, "
        f"got {arr.shape}."
    )


def compute_bog_metrics(
    y_true: np.ndarray | pd.Series | list[Any],
    y_pred: np.ndarray | pd.Series | list[Any] | None = None,
    y_prob: np.ndarray | pd.DataFrame | list[Any] | None = None,
    threshold: float = 0.5,
    positive_class: int = 1,
    eps: float = PROB_CLIP_EPS,
    decimals: int = 4,
) -> dict[str, Any]:
    """Compute BOG probability calibration (Brier, Log-loss) and classification metrics.

    Preserves NULL/missing labels (never converts NULL to NO_ACTION), retains all three
    BOG classes (0=NO_ACTION, 1=SPEND, 2=CONTRIBUTE) in the confusion matrix, clips
    probabilities safely for Log-loss, and explicitly flags single-class or empty folds.
    """
    if not (0.0 < threshold < 1.0):
        raise ValueError(f"threshold must be in (0, 1), got {threshold}")
    if positive_class not in BOG_CLASSES:
        raise ValueError(f"positive_class must be in {BOG_CLASSES}, got {positive_class}")
    if y_pred is None and y_prob is None:
        raise ValueError("At least one of y_pred or y_prob must be provided.")

    yt_series = pd.to_numeric(pd.Series(list(y_true)), errors="coerce")
    total_n = len(yt_series)
    valid_label_mask = yt_series.notna() & np.isfinite(yt_series.to_numpy(dtype=float))
    null_label_count = int((~valid_label_mask).sum())
    valid_n = int(valid_label_mask.sum())

    empty_cm = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    if valid_n == 0:
        return {
            "status": "not_evaluable",
            "total_n": total_n,
            "valid_n": 0,
            "null_label_count": null_label_count,
            "threshold": threshold,
            "positive_class": positive_class,
            "class_counts": {BOG_CLASS_NAMES[c]: 0 for c in BOG_CLASSES},
            "imbalance_ratio": None,
            "accuracy": None,
            "precision_macro": None,
            "recall_macro": None,
            "f1_macro": None,
            "binary_f1_positive": None,
            "per_class": {
                BOG_CLASS_NAMES[c]: {"support": 0, "precision": None, "recall": None, "f1": None}
                for c in BOG_CLASSES
            },
            "confusion_matrix": empty_cm,
            "brier_score": None,
            "log_loss": None,
        }

    yt_valid = yt_series.loc[valid_label_mask].to_numpy(dtype=int)
    invalid_classes = set(yt_valid.tolist()) - set(BOG_CLASSES)
    if invalid_classes:
        raise ValueError(f"Invalid BOG class values found: {sorted(invalid_classes)}")

    prob_mode: Literal["binary", "multiclass"] | None = None
    prob_valid: np.ndarray | None = None
    brier_score: float | None = None
    log_loss_val: float | None = None

    if y_prob is not None:
        prob_all, prob_mode = _validate_probabilities(np.asarray(y_prob, dtype=float), total_n)
        prob_valid = prob_all[valid_label_mask.to_numpy()]

        if prob_mode == "multiclass":
            one_hot = np.zeros((valid_n, len(BOG_CLASSES)), dtype=float)
            one_hot[np.arange(valid_n), yt_valid] = 1.0
            brier_score = float(np.mean(np.sum((prob_valid - one_hot) ** 2, axis=1)))
            clipped = np.clip(prob_valid, eps, 1.0 - eps)
            true_probs = clipped[np.arange(valid_n), yt_valid]
            log_loss_val = float(-np.mean(np.log(true_probs)))
        else:
            y_bin = (yt_valid == positive_class).astype(float)
            brier_score = float(np.mean((prob_valid - y_bin) ** 2))
            clipped = np.clip(prob_valid, eps, 1.0 - eps)
            log_loss_val = float(
                -np.mean(y_bin * np.log(clipped) + (1.0 - y_bin) * np.log(1.0 - clipped))
            )

    if y_pred is not None:
        yp_series = pd.to_numeric(pd.Series(list(y_pred)), errors="coerce")
        if len(yp_series) != total_n:
            raise ValueError(f"Length mismatch: y_pred has {len(yp_series)}, expected {total_n}.")
        yp_valid_raw = yp_series.loc[valid_label_mask]
        if yp_valid_raw.isna().any():
            raise ValueError("y_pred contains NaN on rows with valid y_true labels.")
        yp_valid = yp_valid_raw.to_numpy(dtype=int)
    elif prob_valid is not None:
        if prob_mode == "multiclass":
            yp_valid = np.argmax(prob_valid, axis=1).astype(int)
        else:
            yp_valid = np.where(prob_valid >= threshold, positive_class, 0).astype(int)
    else:  # pragma: no cover
        raise RuntimeError("Unreachable state: neither y_pred nor y_prob available.")

    # Build 3x3 confusion matrix preserving all 3 classes
    cm = np.zeros((3, 3), dtype=int)
    for t_cls, p_cls in zip(yt_valid, yp_valid):
        if p_cls in BOG_CLASSES:
            cm[t_cls, p_cls] += 1

    accuracy = float(np.mean(yt_valid == yp_valid))

    per_class: dict[str, dict[str, Any]] = {}
    class_counts: dict[str, int] = {}
    observed_classes: list[int] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []

    for c in BOG_CLASSES:
        support = int((yt_valid == c).sum())
        class_counts[BOG_CLASS_NAMES[c]] = support
        tp = int(cm[c, c])
        fp = int(cm[:, c].sum() - tp)
        fn = int(cm[c, :].sum() - tp)

        if support > 0:
            observed_classes.append(c)

        prec = (tp / (tp + fp)) if (tp + fp) > 0 else None
        rec = (tp / (tp + fn)) if (tp + fn) > 0 else None
        f1 = (
            (2.0 * prec * rec / (prec + rec))
            if (prec is not None and rec is not None and (prec + rec) > 0.0)
            else (0.0 if (support > 0 and (tp + fp) > 0) else None)
        )
        per_class[BOG_CLASS_NAMES[c]] = {
            "support": support,
            "precision": _round_or_none(prec, decimals),
            "recall": _round_or_none(rec, decimals),
            "f1": _round_or_none(f1, decimals),
        }
        if support > 0:
            if prec is not None:
                precisions.append(prec)
            if rec is not None:
                recalls.append(rec)
            if f1 is not None:
                f1s.append(f1)

    nonzero_counts = [v for v in class_counts.values() if v > 0]
    imbalance_ratio: float | None = (
        float(max(nonzero_counts) / min(nonzero_counts)) if len(nonzero_counts) >= 2 else None
    )

    if len(observed_classes) < 2:
        status = "undefined_single_class"
        precision_macro = None
        recall_macro = None
        f1_macro = None
    else:
        status = "ok" if len(observed_classes) == 3 else "provisional_missing_class"
        precision_macro = float(np.mean(precisions)) if len(precisions) == len(observed_classes) else None
        recall_macro = float(np.mean(recalls)) if len(recalls) == len(observed_classes) else None
        f1_macro = float(np.mean(f1s)) if len(f1s) == len(observed_classes) else None

    # Binary F1 for positive_class (e.g. SPEND vs Rest)
    pos_support = class_counts[BOG_CLASS_NAMES[positive_class]]
    pos_tp = int(np.sum((yt_valid == positive_class) & (yp_valid == positive_class)))
    pos_fp = int(np.sum((yt_valid != positive_class) & (yp_valid == positive_class)))
    pos_fn = int(np.sum((yt_valid == positive_class) & (yp_valid != positive_class)))
    if pos_support == 0 or (pos_tp + pos_fp) == 0:
        binary_f1: float | None = None
    else:
        binary_f1 = float(2.0 * pos_tp / (2.0 * pos_tp + pos_fp + pos_fn))

    return {
        "status": status,
        "total_n": total_n,
        "valid_n": valid_n,
        "null_label_count": null_label_count,
        "threshold": threshold,
        "positive_class": positive_class,
        "class_counts": class_counts,
        "imbalance_ratio": _round_or_none(imbalance_ratio, decimals),
        "accuracy": _round_or_none(accuracy, decimals),
        "precision_macro": _round_or_none(precision_macro, decimals),
        "recall_macro": _round_or_none(recall_macro, decimals),
        "f1_macro": _round_or_none(f1_macro, decimals),
        "binary_f1_positive": _round_or_none(binary_f1, decimals),
        "per_class": per_class,
        "confusion_matrix": cm.tolist(),
        "brier_score": _round_or_none(brier_score, decimals),
        "log_loss": _round_or_none(log_loss_val, decimals),
    }


def aggregate_regression_metrics(
    predictions_df: pd.DataFrame,
    group_cols: list[str],
    y_true_col: str = "y_true",
    y_pred_col: str = "y_pred",
    y_prev_col: str | None = None,
    unit_col: str | None = "unit_code",
    require_single_unit: bool = True,
) -> pd.DataFrame:
    """Aggregate regression metrics by product, horizon, or evaluation window.

    Refuses to pool mixed units when unit_col is present and require_single_unit=True.
    """
    if predictions_df.empty:
        return pd.DataFrame(columns=[*group_cols, "status", "valid_n", "mae", "rmse", "mape"])

    rows: list[dict[str, Any]] = []
    for keys, grp in predictions_df.groupby(group_cols, sort=True, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        key_dict = dict(zip(group_cols, keys))

        unit = "VND/liter"
        if unit_col and unit_col in grp.columns:
            units = sorted(grp[unit_col].dropna().astype(str).unique().tolist())
            if require_single_unit and len(units) > 1:
                raise ValueError(
                    f"Cannot pool mixed units {units} inside group {key_dict}. "
                    "Include unit_col in group_cols or filter to a single unit."
                )
            if len(units) == 1:
                unit = units[0]

        y_prev = grp[y_prev_col] if (y_prev_col and y_prev_col in grp.columns) else None
        metrics = compute_regression_metrics(
            grp[y_true_col],
            grp[y_pred_col],
            y_prev=y_prev,
            unit=unit,
        )
        rows.append({**key_dict, **metrics})

    return pd.DataFrame(rows)


def evaluate_baselines_on_t21(
    panel_df: pd.DataFrame,
    validation_config_path: str | Path = "configs/validation.yaml",
    baseline_config_path: str | Path = "configs/baseline.yaml",
    mode: Literal["expanding", "rolling"] = "expanding",
    dataset_run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate T23 Naive & Seasonal Naive baselines on T21 folds using T24 metrics.

    Constructs strictly prior forecast origins (effective_at_local - 1 second) so each
    target event is forecasted strictly before it becomes effective. Final Test (2026)
    is never touched.
    """
    b_cfg = yaml.safe_load(Path(baseline_config_path).read_text(encoding="utf-8"))
    horizon = int(b_cfg.get("horizon", 1))
    naive_cfg = b_cfg["models"]["naive_persistence"]
    seas_cfg = b_cfg["models"]["seasonal_naive"]

    splits = list(iter_validation_splits(panel_df, config_path=validation_config_path, mode=mode))
    oof_frames: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []

    for split in splits:
        eval_ts = pd.to_datetime(split.eval_df["effective_at_local"], utc=True, errors="raise")
        origins = pd.DataFrame(
            {
                "product_id": split.eval_df["product_id"].to_numpy(),
                "forecast_origin_local": (eval_ts - pd.Timedelta(seconds=1)).to_numpy(),
            }
        )

        # Fit strictly on split.train_df so _global_median never sees eval_df;
        # pass split.eval_df via additional_history for strictly causal walk-forward lookup.
        naive_model = NaivePersistenceBaseline(
            horizon=horizon,
            cold_start_policy=str(naive_cfg.get("cold_start_policy", "global_median")),
        ).fit(split.train_df)
        seas_model = SeasonalNaiveBaseline(
            seasonal_period=str(seas_cfg.get("seasonal_period", "7D")),
            tolerance=str(seas_cfg.get("tolerance", "1D")),
            fallback=str(seas_cfg.get("fallback", "persistence")),
            horizon=horizon,
            cold_start_policy=str(seas_cfg.get("cold_start_policy", "global_median")),
        ).fit(split.train_df)

        naive_out = naive_model.predict(origins, additional_history=split.eval_df)
        seas_out = seas_model.predict(origins, additional_history=split.eval_df)

        y_true = pd.to_numeric(split.eval_df["price_value"], errors="coerce").to_numpy(dtype=float)
        # Exclude cold-start fallback rows from directional reference price (no prior product price).
        y_prev = np.where(
            naive_out["used_fallback"].to_numpy(dtype=bool),
            np.nan,
            naive_out["naive_prediction"].to_numpy(dtype=float),
        )
        unit_vals = (
            split.eval_df["unit_code"].astype(str).to_numpy()
            if "unit_code" in split.eval_df.columns
            else np.full(len(split.eval_df), "VND/liter")
        )

        for model_name, out_df, pred_col in (
            ("naive_persistence", naive_out, "naive_prediction"),
            ("seasonal_naive", seas_out, "seasonal_prediction"),
        ):
            preds = out_df[pred_col].to_numpy(dtype=float)
            m = compute_regression_metrics(y_true, preds, y_prev=y_prev)
            fold_rows.append(
                {
                    "model_name": model_name,
                    "mode": mode,
                    "fold_id": split.fold_id,
                    "horizon": horizon,
                    "train_start": split.train_start,
                    "train_end": split.train_end,
                    "eval_start": split.eval_start,
                    "eval_end": split.eval_end,
                    "fallback_rate_pct": round(float(out_df["used_fallback"].mean() * 100.0), 2),
                    **m,
                }
            )
            oof_frames.append(
                pd.DataFrame(
                    {
                        "model_name": model_name,
                        "mode": mode,
                        "fold_id": split.fold_id,
                        "horizon": horizon,
                        "product_id": split.eval_df["product_id"].astype(str).to_numpy(),
                        "unit_code": unit_vals,
                        "effective_at_local": split.eval_df["effective_at_local"].astype(str).to_numpy(),
                        "y_true": y_true,
                        "y_pred": preds,
                        "y_prev": y_prev,
                        "used_fallback": out_df["used_fallback"].to_numpy(dtype=bool),
                    }
                )
            )

    per_fold_df = pd.DataFrame(fold_rows)
    oof_df = pd.concat(oof_frames, ignore_index=True)

    # Measure exact overlap between Naive and Seasonal Naive predictions
    n_preds = oof_df[oof_df["model_name"] == "naive_persistence"]["y_pred"].to_numpy()
    s_preds = oof_df[oof_df["model_name"] == "seasonal_naive"]["y_pred"].to_numpy()
    overlap_rate_pct = round(float(np.mean(np.isclose(n_preds, s_preds)) * 100.0), 2)

    pooled_df = aggregate_regression_metrics(
        oof_df, group_cols=["model_name", "horizon"], y_prev_col="y_prev"
    )
    per_product_df = aggregate_regression_metrics(
        oof_df, group_cols=["model_name", "product_id"], y_prev_col="y_prev"
    )

    return {
        "metadata": {
            "dataset_run_id": dataset_run_id,
            "mode": mode,
            "horizon": horizon,
            "validation_config": str(validation_config_path),
            "baseline_config": str(baseline_config_path),
            "final_test_locked": True,
            "naive_seasonal_overlap_pct": overlap_rate_pct,
        },
        "per_fold": per_fold_df,
        "pooled": pooled_df,
        "per_product": per_product_df,
        "oof_predictions": oof_df,
    }
