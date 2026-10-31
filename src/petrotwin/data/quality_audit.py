"""Read-only profiling and quality checks for the accepted PetroTwin snapshot.

This module complements, but does not replace, ``petrotwin.data.pipeline``.
It never edits raw inputs or published processed outputs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .pipeline import PRODUCT_IDS


RAW_FILES = {
    "Brent": "data/raw/brent_crude_prices.csv",
    "USD/VND": "data/raw/usd_vnd_rates.csv",
    "Fuel prices": "data/raw/data_price_petro.csv",
}

OUTPUT_FILES = {
    "fuel_price_observation": "fuel_price_observation.csv",
    "market_observation": "market_observation.csv",
    "fuel_market_panel": "fuel_market_panel.csv",
    "quarantine": "quarantine.csv",
}


def _text_sha256(path: Path) -> tuple[str, str]:
    """Return exact and LF-normalized hashes for Git text checkouts on Windows."""
    payload = path.read_bytes()
    exact = hashlib.sha256(payload).hexdigest()
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return exact, hashlib.sha256(normalized).hexdigest()


def find_repository_root(start: Path | None = None) -> Path:
    """Find a checkout by walking upward from the current directory."""
    here = (start or Path.cwd()).resolve()
    candidates = [here, *here.parents]
    for candidate in candidates:
        if (candidate / "data/processed/current.json").is_file() and (
            candidate / "src/petrotwin/data/pipeline.py"
        ).is_file():
            return candidate
    raise FileNotFoundError(
        "Could not find a PetroTwin checkout with data/processed/current.json. "
        "Open the notebook inside the repository."
    )


def load_bundle(root: Path | None = None) -> dict[str, Any]:
    """Load raw files, the accepted run, its manifest, and its quality report."""
    repo = find_repository_root(root)
    pointer = json.loads((repo / "data/processed/current.json").read_text(encoding="utf-8"))
    run_dir = (repo / "data/processed" / pointer["path"]).resolve()
    if not run_dir.is_relative_to(repo / "data/processed"):
        raise ValueError("The current-run pointer resolves outside data/processed")

    raw = {
        label: pd.read_csv(repo / relative_path, low_memory=False)
        for label, relative_path in RAW_FILES.items()
    }
    processed = {
        name: pd.read_csv(run_dir / filename, low_memory=False)
        for name, filename in OUTPUT_FILES.items()
    }
    return {
        "root": repo,
        "pointer": pointer,
        "run_dir": run_dir,
        "raw": raw,
        "processed": processed,
        "manifest": json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")),
        "quality": json.loads((run_dir / "quality_report.json").read_text(encoding="utf-8")),
    }


def source_inventory(bundle: dict[str, Any]) -> pd.DataFrame:
    """Summarize file size, shape, blank cells, exact duplicate rows, and date spans."""
    rows = []
    for label, frame in bundle["raw"].items():
        path = bundle["root"] / RAW_FILES[label]
        date_column = "effective_date" if label == "Fuel prices" else "date"
        dates = pd.to_datetime(frame[date_column], errors="coerce")
        rows.append({
            "source": label,
            "rows": len(frame),
            "columns": len(frame.columns),
            "file_mb": round(path.stat().st_size / (1024**2), 3),
            "blank_cells": int(frame.isna().sum().sum()),
            "blank_cell_pct": round(frame.isna().sum().sum() / max(frame.size, 1) * 100, 3),
            "exact_duplicate_rows": int(frame.duplicated().sum()),
            "date_min": dates.min().date() if dates.notna().any() else None,
            "date_max": dates.max().date() if dates.notna().any() else None,
        })
    return pd.DataFrame(rows).set_index("source")


def column_profile(bundle: dict[str, Any]) -> pd.DataFrame:
    """Profile every raw column, including nulls, cardinality, and numeric outliers."""
    records: list[dict[str, Any]] = []
    for source, frame in bundle["raw"].items():
        for column in frame.columns:
            series = frame[column]
            missing = int(series.isna().sum())
            record: dict[str, Any] = {
                "source": source,
                "column": column,
                "dtype": str(series.dtype),
                "rows": len(series),
                "non_null": int(series.notna().sum()),
                "missing": missing,
                "missing_pct": round(missing / max(len(series), 1) * 100, 3),
                "distinct": int(series.nunique(dropna=True)),
                "min": None,
                "q1": None,
                "median": None,
                "mean": None,
                "std": None,
                "q3": None,
                "max": None,
                "zero_count": None,
                "negative_count": None,
                "iqr_outlier_count": None,
            }
            if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
                values = pd.to_numeric(series, errors="coerce").dropna()
                if not values.empty:
                    q1, median, q3 = values.quantile([0.25, 0.5, 0.75])
                    iqr = q3 - q1
                    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                    record.update({
                        "min": values.min(),
                        "q1": q1,
                        "median": median,
                        "mean": values.mean(),
                        "std": values.std(),
                        "q3": q3,
                        "max": values.max(),
                        "zero_count": int(values.eq(0).sum()),
                        "negative_count": int(values.lt(0).sum()),
                        "iqr_outlier_count": int(values.lt(lower).sum() + values.gt(upper).sum()),
                    })
            records.append(record)
    return pd.DataFrame(records).set_index(["source", "column"])


def key_profile(bundle: dict[str, Any]) -> pd.DataFrame:
    """Check raw and canonical keys for missing and repeated key values."""
    specs = [
        ("Raw Brent", bundle["raw"]["Brent"], ("date",)),
        ("Raw USD/VND", bundle["raw"]["USD/VND"], ("date",)),
        ("Raw fuel", bundle["raw"]["Fuel prices"], ("product_name", "effective_date", "effective_time")),
        ("Canonical fuel", bundle["processed"]["fuel_price_observation"],
         ("product_id", "effective_at_local", "source_id", "snapshot_id")),
        ("Canonical market", bundle["processed"]["market_observation"],
         ("series_id", "observation_date", "source_id", "snapshot_id")),
        ("Canonical panel", bundle["processed"]["fuel_market_panel"],
         ("product_id", "effective_at_local", "target_snapshot_id")),
    ]
    records = []
    for name, frame, keys in specs:
        key_values = frame[list(keys)]
        missing_mask = key_values.isna()
        for column in keys:
            if pd.api.types.is_object_dtype(key_values[column]) or pd.api.types.is_string_dtype(key_values[column]):
                missing_mask[column] |= key_values[column].astype("string").str.strip().eq("").fillna(False)
        missing_key_rows = int(missing_mask.any(axis=1).sum())
        repeated_key_rows = int(frame.duplicated(list(keys), keep=False).sum())
        records.append({
            "table": name,
            "key": ", ".join(keys),
            "rows": len(frame),
            "rows_with_missing_key": missing_key_rows,
            "rows_in_repeated_keys": repeated_key_rows,
            "unique_key_count": int(frame.drop_duplicates(list(keys)).shape[0]),
        })
    return pd.DataFrame(records).set_index("table")


def fuel_coverage(bundle: dict[str, Any]) -> pd.DataFrame:
    """Summarize event coverage and irregular intervals by accepted fuel product."""
    panel = bundle["processed"]["fuel_market_panel"].copy()
    panel["event_date"] = pd.to_datetime(panel["effective_at_local"], errors="coerce").dt.date
    raw_names = bundle["raw"]["Fuel prices"]["product_name"].dropna().unique()
    id_to_name = {PRODUCT_IDS[name]: name for name in raw_names if name in PRODUCT_IDS}
    rows = []
    for product in sorted(id_to_name):
        group = panel.loc[panel["product_id"].eq(product)]
        if group.empty:
            rows.append({
                "product_id": product,
                "source_product_name": id_to_name[product],
                "events": 0,
                "distinct_event_dates": 0,
                "first_event": None,
                "last_event": None,
                "median_gap_days": np.nan,
                "q1_gap_days": np.nan,
                "q3_gap_days": np.nan,
                "max_gap_days": np.nan,
                "price_min": np.nan,
                "price_median": np.nan,
                "price_max": np.nan,
                "missing_price_changes": 0,
                "missing_brent": 0,
                "missing_fx": 0,
            })
            continue
        dates = pd.Series(pd.to_datetime(group["event_date"].dropna().unique())).sort_values()
        gaps = dates.diff().dt.days.dropna()
        prices = pd.to_numeric(group["price_value"], errors="coerce").dropna()
        rows.append({
            "product_id": product,
            "source_product_name": id_to_name[product],
            "events": len(group),
            "distinct_event_dates": int(group["event_date"].nunique()),
            "first_event": dates.min().date() if not dates.empty else None,
            "last_event": dates.max().date() if not dates.empty else None,
            "median_gap_days": gaps.median() if not gaps.empty else np.nan,
            "q1_gap_days": gaps.quantile(0.25) if not gaps.empty else np.nan,
            "q3_gap_days": gaps.quantile(0.75) if not gaps.empty else np.nan,
            "max_gap_days": gaps.max() if not gaps.empty else np.nan,
            "price_min": prices.min() if not prices.empty else np.nan,
            "price_median": prices.median() if not prices.empty else np.nan,
            "price_max": prices.max() if not prices.empty else np.nan,
            "missing_price_changes": int(pd.to_numeric(group["price_change_value"], errors="coerce").isna().sum()),
            "missing_brent": int(pd.to_numeric(group["brent_usd_per_barrel"], errors="coerce").isna().sum()),
            "missing_fx": int(pd.to_numeric(group["usd_vnd_rate"], errors="coerce").isna().sum()),
        })
    return pd.DataFrame(rows).set_index("product_id")


def annotation_coverage(bundle: dict[str, Any]) -> pd.DataFrame:
    """Count verified, unverified, positive, and zero fund annotations."""
    panel = bundle["processed"]["fuel_market_panel"]
    labels = pd.to_numeric(panel["stabilization_fund_subsidy"], errors="coerce")
    return pd.DataFrame([
        {"label": "verified spending", "count": int(labels.eq(1).sum())},
        {"label": "verified no spending", "count": int(labels.eq(0).sum())},
        {"label": "unverified / missing", "count": int(labels.isna().sum())},
    ]).set_index("label")


def quality_gates(bundle: dict[str, Any]) -> pd.DataFrame:
    """Recompute manifest, key, panel, missingness, and point-in-time checks."""
    root = bundle["root"]
    run_dir = bundle["run_dir"]
    report = bundle["quality"]
    manifest = bundle["manifest"]
    results: list[dict[str, str]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        results.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    for source in manifest.get("sources", []):
        filename = source["raw_filename"]
        source_path = root / "data" / ("manual" if filename.startswith("manual/") else "raw") / filename.removeprefix("manual/")
        exists = source_path.is_file()
        exact_hash, normalized_hash = _text_sha256(source_path) if exists else ("missing", "missing")
        passed = exists and normalized_hash == source["sha256"]
        hash_detail = "matches manifest" if exact_hash == source["sha256"] else (
            "matches after Windows CRLF normalization" if passed else "checksum mismatch"
        )
        add(f"Source checksum: {source['raw_filename']}", passed,
            hash_detail if exists else "missing file")

    for filename, expected in manifest["outputs"].items():
        path = run_dir / filename
        exact_hash, normalized_hash = _text_sha256(path)
        observed_rows = len(pd.read_csv(path, low_memory=False))
        hash_ok = normalized_hash == expected["sha256"]
        passed = hash_ok and observed_rows == expected["rows"]
        hash_detail = "match" if exact_hash == expected["sha256"] else (
            "match after Windows CRLF normalization" if hash_ok else "MISMATCH"
        )
        add(f"Output manifest: {filename}", passed,
            f"rows {observed_rows}/{expected['rows']}; checksum {hash_detail}")

    keys = key_profile(bundle)
    keys_ok = keys["rows_with_missing_key"].eq(0).all() and keys["rows_in_repeated_keys"].eq(0).all()
    add("Raw and canonical keys", bool(keys_ok),
        "No missing or repeated natural keys" if keys_ok else "Review missing or repeated key rows")

    fuel = bundle["processed"]["fuel_price_observation"]
    panel = bundle["processed"]["fuel_market_panel"]
    fuel_keys = set(map(tuple, fuel[["product_id", "effective_at_local", "snapshot_id"]].to_numpy()))
    panel_keys = set(map(tuple, panel[["product_id", "effective_at_local", "target_snapshot_id"]].to_numpy()))
    add("Fuel to panel event coverage", fuel_keys == panel_keys,
        f"fuel={len(fuel_keys)}; panel={len(panel_keys)}")

    event_dates = pd.to_datetime(panel["effective_at_local"], utc=True, errors="coerce")
    point_in_time_ok = True
    for feature_date in ("brent_observation_date", "fx_observation_date"):
        observed = panel[feature_date].notna()
        feature_dates = pd.to_datetime(panel.loc[observed, feature_date], errors="coerce", utc=True)
        point_in_time_ok &= bool((feature_dates < event_dates.loc[observed]).all())
    add("Market features strictly prior", bool(point_in_time_ok),
        "All non-null Brent/FX observation dates precede the fuel event")

    market = bundle["processed"]["market_observation"]
    market_null_ok = (
        market.loc[market["observation_status"].eq("missing"), "value"].isna().all()
        and market.loc[market["observation_status"].eq("observed"), "value"].notna().all()
    )
    add("Market missing-value status", bool(market_null_ok),
        "NULL values are explicitly marked missing; observed values are present")

    row_report = report["integrated"]
    accepted_rows = len(panel) == row_report["fuel_market_panel_rows"] == row_report["fuel_price_observation_rows"]
    add("Accepted event row counts", bool(accepted_rows),
        f"panel={len(panel)}; reported={row_report['fuel_market_panel_rows']}")

    quarantine = bundle["processed"]["quarantine"]
    expected_quarantine = report.get("quarantine_reason_counts", {}).get(
        "domestic_zero_price_pending_source_confirmation", 0
    )
    expected_quarantine = report.get("integrated", {}).get("quarantine_reason_counts", {}).get(
        "domestic_zero_price_pending_source_confirmation", expected_quarantine
    )
    quarantine_ok = len(quarantine) == report.get("quarantined_rows", len(quarantine))
    if expected_quarantine:
        quarantine_ok &= expected_quarantine <= len(quarantine)
    add("Quarantine retained", bool(quarantine_ok),
        f"rows={len(quarantine)}; zero-price FO pending source confirmation={expected_quarantine}")

    reuse_unknown = any(item.get("license_reuse_terms", "unknown") == "unknown; verify with publisher"
                        for item in manifest.get("sources", []))
    results.append({"check": "Source reuse terms", "status": "WARN" if reuse_unknown else "PASS",
                    "detail": "At least one source reuse/license term remains unverified" if reuse_unknown
                    else "Source reuse terms documented"})
    return pd.DataFrame(results).set_index("check")


def make_quality_figures(bundle: dict[str, Any]) -> list[tuple[str, plt.Figure]]:
    """Create read-only diagnostic charts; statistical outliers are not auto-removed."""
    figures: list[tuple[str, plt.Figure]] = []
    raw_inventory = source_inventory(bundle)
    report = bundle["quality"]

    raw_rows = [len(bundle["raw"][source]) for source in RAW_FILES]
    canonical_rows = [report["source_checks"][key]["rows_canonical"]
                      for key in ("brent", "usd_vnd", "fuel_prices")]
    quarantined = [report["source_checks"][key]["quarantined_rows"]
                   for key in ("brent", "usd_vnd", "fuel_prices")]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = np.arange(len(RAW_FILES))
    ax.bar(x - 0.22, raw_rows, width=0.22, label="Raw rows", color="#5470c6")
    ax.bar(x, canonical_rows, width=0.22, label="Canonical rows", color="#91cc75")
    ax.bar(x + 0.22, quarantined, width=0.22, label="Quarantined", color="#ee6666")
    ax.set_xticks(x, list(RAW_FILES.keys()))
    ax.set_ylabel("Rows")
    ax.set_title("Raw-to-canonical row reconciliation")
    ax.legend()
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    figures.append(("Row reconciliation", fig))

    profiles = column_profile(bundle).reset_index()
    missing = profiles.loc[profiles["missing_pct"].gt(0)].sort_values("missing_pct")
    fig, ax = plt.subplots(figsize=(10, max(4, 0.32 * len(missing))))
    if missing.empty:
        ax.text(0.5, 0.5, "No raw columns contain missing values", ha="center", va="center")
        ax.set_axis_off()
    else:
        labels = missing["source"] + " · " + missing["column"]
        ax.barh(labels, missing["missing_pct"], color="#fac858")
        ax.set_xlabel("Missing values (%)")
        ax.set_title("Raw column missingness (not imputed)")
        ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    figures.append(("Raw missingness", fig))

    fuel = bundle["processed"]["fuel_price_observation"].copy()
    fuel["price_value"] = pd.to_numeric(fuel["price_value"], errors="coerce")
    products = sorted(fuel["product_id"].dropna().unique())
    data = [fuel.loc[fuel["product_id"].eq(product), "price_value"].dropna().to_numpy()
            for product in products]
    fig, ax = plt.subplots(figsize=(11, max(5, 0.52 * len(products))))
    ax.boxplot(data, vert=False, tick_labels=products, showfliers=True, patch_artist=True,
               boxprops={"facecolor": "#91cc75", "alpha": 0.75})
    ax.set_xlabel("VND per liter")
    ax.set_title("Accepted fuel-price distributions by product\n(Boxplot points are review flags, not automatic deletions)")
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    figures.append(("Fuel price distributions", fig))

    coverage = fuel_coverage(bundle).reset_index()
    fig, ax = plt.subplots(figsize=(12, max(5, 0.5 * len(coverage))))
    for y, row in coverage.iterrows():
        if row["events"] == 0:
            ax.text(0.01, y, "No valid rows", transform=ax.get_yaxis_transform(), va="center", color="#ee6666")
            continue
        start, end = pd.to_datetime([row["first_event"], row["last_event"]])
        ax.hlines(y, start, end, color="#5470c6", linewidth=5, alpha=0.8)
        ax.plot([start, end], [y, y], "o", color="#304b8a", markersize=4)
    ax.set_yticks(range(len(coverage)), coverage["product_id"])
    ax.set_xlabel("Effective date coverage")
    ax.set_title("Accepted fuel-event coverage by product (span only; gaps remain)" )
    ax.grid(axis="x", alpha=0.2)
    fig.autofmt_xdate()
    fig.tight_layout()
    figures.append(("Fuel coverage", fig))

    market = bundle["processed"]["market_observation"].copy()
    market["observation_date"] = pd.to_datetime(market["observation_date"], errors="coerce")
    market_rows = []
    for series_id, group in market.groupby("series_id", sort=True):
        dates = group["observation_date"].dropna().drop_duplicates().sort_values()
        gaps = dates.diff().dt.days.dropna()
        market_rows.extend({"series_id": series_id, "gap_days": int(gap)} for gap in gaps)
    gap_df = pd.DataFrame(market_rows)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    if not gap_df.empty:
        for series_id, group in gap_df.groupby("series_id"):
            ax.hist(group["gap_days"], bins=np.arange(0.5, max(10, gap_df["gap_days"].max()) + 1.5),
                    alpha=0.55, label=series_id)
        ax.set_xlabel("Calendar days between distinct market dates")
        ax.set_ylabel("Count of intervals")
        ax.legend()
        ax.grid(axis="y", alpha=0.2)
    ax.set_title("Market observation interval diagnostics (gaps are not filled)")
    fig.tight_layout()
    figures.append(("Market interval diagnostics", fig))

    numeric = profiles.loc[profiles["iqr_outlier_count"].notna()].copy()
    numeric = numeric.sort_values("iqr_outlier_count")
    fig, ax = plt.subplots(figsize=(10, max(4, 0.35 * len(numeric))))
    if numeric.empty:
        ax.text(0.5, 0.5, "No numeric fields", ha="center", va="center")
        ax.set_axis_off()
    else:
        labels = numeric["source"] + " · " + numeric["column"]
        ax.barh(labels, numeric["iqr_outlier_count"], color="#ee6666")
        ax.set_xlabel("Values outside 1.5 × IQR fences")
        ax.set_title("Statistical outlier review flags (no values removed)")
        ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    figures.append(("IQR review flags", fig))

    return figures
