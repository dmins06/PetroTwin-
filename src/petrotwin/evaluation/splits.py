"""T21-compliant chronological train/validation/test split utilities."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml


@dataclass(frozen=True)
class ValidationSplit:
    """A single chronological train/evaluation split with leakage-verified boundaries."""

    fold_id: str
    mode: str
    train_start: str
    train_end: str
    eval_start: str
    eval_end: str
    train_df: pd.DataFrame
    eval_df: pd.DataFrame


def _extract_effective_date(df: pd.DataFrame) -> pd.Series:
    if "effective_date_local" in df.columns:
        return pd.to_datetime(df["effective_date_local"]).dt.normalize()
    ts = pd.to_datetime(df["effective_at_local"], errors="raise")
    if ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)
    return ts.dt.normalize()


def _assert_no_split_leakage(
    train_df: pd.DataFrame,
    eval_df: pd.DataFrame,
    fold_id: str,
) -> None:
    """Enforce chronological ordering and prevent splitting an adjustment event across partitions."""
    if train_df.empty or eval_df.empty:
        raise ValueError(
            f"Split {fold_id} produced empty train ({len(train_df)}) or eval ({len(eval_df)})."
        )
    train_dates = _extract_effective_date(train_df)
    eval_dates = _extract_effective_date(eval_df)
    if train_dates.max() >= eval_dates.min():
        raise ValueError(
            f"Chronological leakage in {fold_id}: max train date {train_dates.max().date()} "
            f">= min eval date {eval_dates.min().date()}."
        )
    overlap_ts = set(train_df["effective_at_local"].astype(str)) & set(
        eval_df["effective_at_local"].astype(str)
    )
    if overlap_ts:
        raise ValueError(
            f"Event split leakage in {fold_id}: overlapping timestamps {sorted(overlap_ts)[:3]}"
        )


def iter_validation_splits(
    df: pd.DataFrame,
    config_path: str | Path = "configs/validation.yaml",
    mode: Literal["expanding", "rolling"] = "expanding",
) -> Iterator[ValidationSplit]:
    """Yield chronological ValidationSplits defined in configs/validation.yaml (T21)."""
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    if mode == "expanding":
        folds = cfg["expanding_window"]["folds"]
    elif mode == "rolling":
        folds = cfg["rolling_window"]["folds"]
    else:
        raise ValueError(f"Unsupported validation mode: {mode!r}")

    eff_dt = _extract_effective_date(df)
    for fold in folds:
        fold_id = str(fold["fold_id"])
        if mode == "expanding":
            tr_start, tr_end = str(fold["train_start"]), str(fold["train_end"])
        else:
            tr_start = str(fold["train_cutoff_start"])
            tr_end = (pd.Timestamp(fold["val_start"]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

        va_start, va_end = str(fold["val_start"]), str(fold["val_end"])
        tr_mask = (eff_dt >= pd.Timestamp(tr_start)) & (eff_dt <= pd.Timestamp(tr_end))
        va_mask = (eff_dt >= pd.Timestamp(va_start)) & (eff_dt <= pd.Timestamp(va_end))

        train_df = df.loc[tr_mask].copy()
        eval_df = df.loc[va_mask].copy()
        _assert_no_split_leakage(train_df, eval_df, fold_id)

        yield ValidationSplit(
            fold_id=fold_id,
            mode=mode,
            train_start=tr_start,
            train_end=tr_end,
            eval_start=va_start,
            eval_end=va_end,
            train_df=train_df,
            eval_df=eval_df,
        )


def get_final_test_split(
    df: pd.DataFrame,
    config_path: str | Path = "configs/validation.yaml",
) -> ValidationSplit:
    """Return the locked Final Test split (2026) defined in configs/validation.yaml."""
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    parts = cfg["split_strategy"]["partitions"]
    tr_start = str(parts["initial_train"]["start_date"])
    tr_end = str(parts["validation"]["end_date"])
    te_start = str(parts["final_test"]["start_date"])
    te_end = str(parts["final_test"]["end_date"])

    eff_dt = _extract_effective_date(df)
    train_df = df.loc[(eff_dt >= pd.Timestamp(tr_start)) & (eff_dt <= pd.Timestamp(tr_end))].copy()
    test_df = df.loc[(eff_dt >= pd.Timestamp(te_start)) & (eff_dt <= pd.Timestamp(te_end))].copy()
    _assert_no_split_leakage(train_df, test_df, "final_test")

    return ValidationSplit(
        fold_id="final_test",
        mode="locked_final_test",
        train_start=tr_start,
        train_end=tr_end,
        eval_start=te_start,
        eval_end=te_end,
        train_df=train_df,
        eval_df=test_df,
    )
