"""Reproducible raw-CSV to canonical dataset pipeline."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time as time_module
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


SCHEMA_VERSION = "petrotwin-data-v3"
PIPELINE_VERSION = "1.2.0"
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc

BRENT_FILE = "brent_crude_prices.csv"
FX_FILE = "usd_vnd_rates.csv"
FUEL_FILE = "data_price_petro.csv"

PRODUCT_IDS = {
    "Dầu DO 0,005S-IV": "diesel_0005s_iv",
    "Dầu DO 0,05S-II": "diesel_005s_ii",
    "Dầu DO 0,001S-V": "diesel_0001s_v",
    "Dầu FO 3,5%S": "fuel_oil_35s",
    "Dầu FO 3%S": "fuel_oil_3s",
    "Dầu KO": "kerosene_ko",
    "Xăng E5 RON 92-II": "gasoline_e5_ron92_ii",
    "Xăng E10 RON 95-III": "gasoline_e10_ron95_iii",
    "Xăng RON 95-III": "gasoline_ron95_iii",
    "Xăng RON 95-IV": "gasoline_ron95_iv",
}

EXPECTED_HEADERS = {
    BRENT_FILE: [
        "date", "product_name", "price_usd_per_barrel", "unit", "source_url",
        "series_id", "publisher", "source_value", "observation_status", "extracted_at_utc",
    ],
    FX_FILE: [
        "date", "base_currency", "quote_currency", "rate_vnd_per_usd", "unit",
        "rate_type", "source_url", "source_attribution", "source_value", "extracted_at_utc",
    ],
    FUEL_FILE: [
        "date", "effective_date", "effective_time", "product_name", "price_vnd_per_liter",
        "price_display", "price_change_vnd", "price_change_display", "unit",
        "includes_vat_and_environmental_tax", "source_url", "extracted_at_utc",
    ],
}


class PipelineError(Exception):
    """Raised when a batch cannot safely be published."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_write(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _configure_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("petrotwin.data.pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time_module.gmtime
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def _load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if config.get("data", {}).get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise PipelineError(f"Unsupported schema version in {config_path}")
    return config


def _parse_date(value: str, field: str, source: str, row_number: int) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise PipelineError(
            f"{source} row {row_number}: invalid key date in {field}: {value!r}"
        ) from exc
    if parsed.isoformat() != value:
        raise PipelineError(f"{source} row {row_number}: non-ISO date in {field}: {value!r}")
    return parsed


def _parse_retrieved_at(value: str, source: str, row_number: int) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineError(
            f"{source} row {row_number}: invalid extracted_at_utc: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PipelineError(
            f"{source} row {row_number}: extracted_at_utc must include UTC offset: {value!r}"
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: str, field: str, source: str, row_number: int) -> Decimal | None:
    if value is None or value.strip() == "":
        return None
    try:
        result = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{field}_not_numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field}_not_finite")
    return result


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _read_csv(path: Path, expected_headers: list[str]) -> tuple[list[dict[str, str]], str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if headers != expected_headers:
            missing = [field for field in expected_headers if field not in headers]
            unexpected = [field for field in headers if field not in expected_headers]
            raise PipelineError(
                f"{path.name}: schema mismatch; missing={missing}, unexpected={unexpected}, "
                f"order_or_rename_changed={headers != expected_headers}"
            )
        rows = list(reader)
    fingerprint = hashlib.sha256("\x1f".join(headers).encode("utf-8")).hexdigest()
    return rows, fingerprint


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    columns: list[str],
    lineterminator: str = "\n",
) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=columns,
            extrasaction="ignore",
            lineterminator=lineterminator,
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})


def _dedupe_or_fail(
    rows: list[dict[str, Any]], key_fields: tuple[str, ...], source_name: str
) -> tuple[list[dict[str, Any]], int]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    exact_duplicates = 0
    for row in rows:
        key = tuple(row[field] for field in key_fields)
        if key not in unique:
            unique[key] = row
        elif unique[key] == row:
            exact_duplicates += 1
        else:
            raise PipelineError(
                f"{source_name}: conflicting duplicate key {key!r}; refusing publication"
            )
    return list(unique.values()), exact_duplicates


def _parse_market(
    path: Path,
    source_id: str,
    series_id: str,
    value_field: str,
    unit_field: str,
    expected_unit: str,
    source_unit: str,
    status_field: str | None,
    snapshot_id: str,
    headers: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    raw_rows, schema_fingerprint = _read_csv(path, headers)
    output: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    missing_values = 0
    seen_source_rows: set[tuple[str, ...]] = set()
    exact_duplicates = 0

    for row_number, raw in enumerate(raw_rows, start=2):
        raw_key = tuple(raw[field] for field in headers)
        if raw_key in seen_source_rows:
            exact_duplicates += 1
            continue
        seen_source_rows.add(raw_key)
        observation_date = _parse_date(raw["date"], "date", path.name, row_number)
        retrieved_at = _parse_retrieved_at(raw["extracted_at_utc"], path.name, row_number)
        if not raw["source_url"].strip():
            raise PipelineError(f"{path.name} row {row_number}: source_url is required")
        if series_id == "brent_dated_usd_bbl":
            if raw["series_id"] != "DCOILBRENTEU":
                raise PipelineError(f"{path.name} row {row_number}: unexpected Brent series_id")
            if raw["product_name"] != "Crude Oil Prices: Brent - Europe":
                raise PipelineError(f"{path.name} row {row_number}: unexpected Brent product_name")
        elif (raw["base_currency"], raw["quote_currency"], raw["rate_type"]) != (
            "USD", "VND", "SBV central reference rate"
        ):
            raise PipelineError(f"{path.name} row {row_number}: unexpected FX series definition")
        if raw[unit_field] != source_unit:
            raise PipelineError(
                f"{path.name} row {row_number}: unsupported unit {raw[unit_field]!r}"
            )
        status = raw[status_field] if status_field else "observed"
        if status not in {"observed", "missing"}:
            raise PipelineError(f"{path.name} row {row_number}: invalid observation status {status!r}")
        try:
            value = _decimal(raw[value_field], value_field, path.name, row_number)
        except ValueError as exc:
            quarantine.append({
                "source_file": path.name,
                "source_row_number": row_number,
                "reason_code": str(exc),
                "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
            })
            continue
        if status == "missing":
            if value is not None:
                raise PipelineError(
                    f"{path.name} row {row_number}: missing observation must have blank value"
                )
            missing_values += 1
        elif value is None:
            raise PipelineError(
                f"{path.name} row {row_number}: observed value is missing without missing status"
            )
        elif value <= 0:
            quarantine.append({
                "source_file": path.name,
                "source_row_number": row_number,
                "reason_code": "market_value_not_positive",
                "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
            })
            continue

        output.append({
            "series_id": series_id,
            "observation_date": observation_date.isoformat(),
            "value": _decimal_text(value),
            "unit_code": expected_unit,
            "observation_status": status,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "source_value": raw.get("source_value", ""),
            "source_url": raw["source_url"],
            "retrieved_at_utc": retrieved_at,
            "schema_version": SCHEMA_VERSION,
        })

    key = ("series_id", "observation_date", "source_id", "snapshot_id")
    output, duplicate_keys = _dedupe_or_fail(output, key, path.name)
    exact_duplicates += duplicate_keys
    dates = [date.fromisoformat(row["observation_date"]) for row in output]
    quality = {
        "source_file": path.name,
        "rows_read": len(raw_rows),
        "rows_canonical": len(output),
        "exact_duplicates_collapsed": exact_duplicates,
        "missing_values": missing_values,
        "quarantined_rows": len(quarantine),
        "first_observation_date": min(dates).isoformat() if dates else None,
        "last_observation_date": max(dates).isoformat() if dates else None,
        "distinct_dates": len(set(dates)),
        "interval_days": dict(sorted(Counter(
            (right - left).days for left, right in zip(sorted(set(dates)), sorted(set(dates))[1:])
        ).items())),
        "schema_fingerprint_sha256": schema_fingerprint,
    }
    return output, quarantine, quality


def _parse_effective_at(value_date: str, value_time: str, source: str, row_number: int) -> tuple[str, bool]:
    effective_date = _parse_date(value_date, "effective_date", source, row_number)
    normalized_24 = value_time == "24:00"
    if normalized_24:
        effective_date += timedelta(days=1)
        parsed_time = time(0, 0)
    else:
        try:
            parsed_time = datetime.strptime(value_time, "%H:%M").time()
        except ValueError as exc:
            raise PipelineError(
                f"{source} row {row_number}: invalid effective_time: {value_time!r}"
            ) from exc
        if parsed_time.strftime("%H:%M") != value_time:
            raise PipelineError(
                f"{source} row {row_number}: effective_time must use HH:MM: {value_time!r}"
            )
    instant = datetime.combine(effective_date, parsed_time, tzinfo=LOCAL_TZ)
    return instant.isoformat(timespec="seconds"), normalized_24


def _parse_fuel(
    path: Path, snapshot_id: str, headers: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    raw_rows, schema_fingerprint = _read_csv(path, headers)
    output: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    seen_source_rows: set[tuple[str, ...]] = set()
    exact_duplicates = 0
    missing_changes = 0
    zero_prices = 0
    normalized_24_source_rows = 0
    normalized_24_canonical_rows = 0
    price_change_missing_by_product: Counter[str] = Counter()
    product_input_counts: Counter[str] = Counter()
    product_quarantine_counts: Counter[str] = Counter()
    product_effective_dates: dict[str, set[date]] = {}

    for row_number, raw in enumerate(raw_rows, start=2):
        raw_key = tuple(raw[field] for field in headers)
        if raw_key in seen_source_rows:
            exact_duplicates += 1
            continue
        seen_source_rows.add(raw_key)

        source_record_date = _parse_date(raw["date"], "date", path.name, row_number)
        effective_at, normalized = _parse_effective_at(
            raw["effective_date"], raw["effective_time"], path.name, row_number
        )
        normalized_24_source_rows += int(normalized)
        product_name = raw["product_name"]
        product_id = PRODUCT_IDS.get(product_name)
        if not product_id:
            raise PipelineError(
                f"{path.name} row {row_number}: unmapped product label {product_name!r}"
            )
        if raw["unit"] != "Đồng/lít thực tế":
            raise PipelineError(f"{path.name} row {row_number}: unsupported fuel unit {raw['unit']!r}")
        if not raw["source_url"].strip():
            raise PipelineError(f"{path.name} row {row_number}: source_url is required")
        retrieved_at = _parse_retrieved_at(raw["extracted_at_utc"], path.name, row_number)
        product_input_counts[product_id] += 1
        product_effective_dates.setdefault(product_id, set()).add(
            datetime.fromisoformat(effective_at).date()
        )
        if raw["price_vnd_per_liter"].strip() == "":
            raise PipelineError(
                f"{path.name} row {row_number}: missing domestic target price"
            )

        try:
            price = _decimal(raw["price_vnd_per_liter"], "price_vnd_per_liter", path.name, row_number)
            change = _decimal(raw["price_change_vnd"], "price_change_vnd", path.name, row_number)
        except ValueError as exc:
            product_quarantine_counts[product_id] += 1
            quarantine.append({
                "source_file": path.name,
                "source_row_number": row_number,
                "reason_code": str(exc),
                "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
            })
            continue
        if change is None:
            missing_changes += 1
            price_change_missing_by_product[product_id] += 1

        tax_raw = raw["includes_vat_and_environmental_tax"].strip().lower()
        if tax_raw not in {"true", "false"}:
            raise PipelineError(
                f"{path.name} row {row_number}: invalid tax boolean {tax_raw!r}"
            )
        if price == 0:
            zero_prices += 1
            product_quarantine_counts[product_id] += 1
            quarantine.append({
                "source_file": path.name,
                "source_row_number": row_number,
                "reason_code": "domestic_zero_price_pending_source_confirmation",
                "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
            })
            continue
        if price < 0:
            product_quarantine_counts[product_id] += 1
            quarantine.append({
                "source_file": path.name,
                "source_row_number": row_number,
                "reason_code": "domestic_price_not_positive",
                "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
            })
            continue

        output.append({
            "product_id": product_id,
            "source_product_name": product_name,
            "source_record_date": source_record_date.isoformat(),
            "effective_at_local": effective_at,
            "effective_time_24_00_normalized": str(normalized).lower(),
            "price_value": _decimal_text(price),
            "unit_code": "VND/liter",
            "price_change_value": _decimal_text(change),
            # PVOIL does not report stabilization-fund data; the annotation overlay fills it.
            "stabilization_fund_subsidy": "",
            "stabilization_fund_subsidy_amount": "",
            "stabilization_fund_subsidy_unit": "VND/liter" if product_id != "fuel_oil_35s" else "VND/kg",
            "stabilization_fund_source_url": "",
            "tax_included": tax_raw,
            "source_id": "pvoil",
            "snapshot_id": snapshot_id,
            "source_url": raw["source_url"],
            "retrieved_at_utc": retrieved_at,
            "schema_version": SCHEMA_VERSION,
        })
        normalized_24_canonical_rows += int(normalized)

    key = ("product_id", "effective_at_local", "source_id", "snapshot_id")
    output, duplicate_keys = _dedupe_or_fail(output, key, path.name)
    exact_duplicates += duplicate_keys
    per_product: dict[str, dict[str, Any]] = {}
    for product_id in sorted(product_input_counts):
        product_dates = sorted(product_effective_dates[product_id])
        observed_dates = sorted({
            datetime.fromisoformat(row["effective_at_local"]).date()
            for row in output if row["product_id"] == product_id
        })
        interval_counts = Counter(
            (right - left).days for left, right in zip(product_dates, product_dates[1:])
        )
        per_product[product_id] = {
            "source_rows": product_input_counts[product_id],
            "canonical_rows": sum(row["product_id"] == product_id for row in output),
            "quarantined_rows": product_quarantine_counts[product_id],
            "first_effective_date": product_dates[0].isoformat(),
            "last_effective_date": product_dates[-1].isoformat(),
            "missing_price_change_values": price_change_missing_by_product[product_id],
            "interval_days": dict(sorted(interval_counts.items())),
            "canonical_first_effective_date": observed_dates[0].isoformat() if observed_dates else None,
            "canonical_last_effective_date": observed_dates[-1].isoformat() if observed_dates else None,
        }
    quality = {
        "source_file": path.name,
        "rows_read": len(raw_rows),
        "rows_canonical": len(output),
        "exact_duplicates_collapsed": exact_duplicates,
        "quarantined_rows": len(quarantine),
        "zero_prices_quarantined": zero_prices,
        "missing_price_change_values": missing_changes,
        "effective_24_00_source_rows": normalized_24_source_rows,
        "effective_24_00_normalized": normalized_24_canonical_rows,
        "products": per_product,
        "schema_fingerprint_sha256": schema_fingerprint,
    }
    return output, quarantine, quality


def _apply_fund_annotations(fuel_rows: list[dict[str, Any]], annotation_path: Path) -> int:
    """Apply reviewed, date-and-product keyed MOIT fund-use annotations."""
    if not annotation_path.is_file():
        return 0
    annotations: dict[tuple[str, str], dict[str, str]] = {}
    with annotation_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        expected = ["product_id", "source_record_date", "amount", "unit", "source_url"]
        if reader.fieldnames != expected:
            raise PipelineError(f"invalid fund annotation headers in {annotation_path}")
        for row in reader:
            key = (row["product_id"], row["source_record_date"])
            if key in annotations:
                raise PipelineError(f"duplicate fund annotation key: {key}")
            if row["amount"] and (not row["amount"].isdigit() or row["unit"] not in {"VND/liter", "VND/kg"}):
                raise PipelineError(f"invalid fund annotation amount or unit for {key}")
            annotations[key] = row
    count = 0
    for row in fuel_rows:
        annotation = annotations.get((row["product_id"], row["source_record_date"]))
        if annotation is None:
            continue
        row["stabilization_fund_subsidy_amount"] = annotation["amount"]
        row["stabilization_fund_subsidy_unit"] = annotation["unit"]
        row["stabilization_fund_source_url"] = annotation["source_url"]
        row["stabilization_fund_subsidy"] = "1" if annotation["amount"] and int(annotation["amount"]) > 0 else "0"
        count += 1
    return count


def _manifest_entry(path: Path, source_id: str, publisher: str, fingerprint: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    extracted = sorted({row.get("extracted_at_utc", "") for row in rows if row.get("extracted_at_utc")})
    return {
        "snapshot_id": _sha256(path),
        "source_id": source_id,
        "source_url": rows[0].get("source_url", "") if rows else "",
        "publisher_or_attribution": publisher,
        "license_reuse_terms": "unknown; verify with publisher",
        "retrieved_at_utc": extracted[0] if len(extracted) == 1 else extracted,
        "raw_filename": path.name,
        "sha256": _sha256(path),
        "row_count": len(rows),
        "schema_fingerprint_sha256": fingerprint,
    }


def _save_quarantine(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["source_file", "source_row_number", "reason_code", "raw_row_json"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _integrate_fuel_market(
    fuel_rows: list[dict[str, Any]], market_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Attach the latest observed market values strictly before each fuel event date."""
    market_by_series: dict[str, list[dict[str, Any]]] = {}
    for row in market_rows:
        if row["observation_status"] == "observed" and row["value"] != "":
            market_by_series.setdefault(row["series_id"], []).append(row)
    for series_id, series_rows in market_by_series.items():
        series_rows.sort(key=lambda row: row["observation_date"])
        market_by_series[series_id] = {
            "rows": series_rows,
            "dates": [row["observation_date"] for row in series_rows],
        }

    series_specs = {
        "brent_dated_usd_bbl": (
            "brent_observation_date", "brent_usd_per_barrel", "brent_age_days", "USD/bbl", "eia_fred"
        ),
        "usd_vnd_sbv_reference": (
            "fx_observation_date", "usd_vnd_rate", "fx_age_days", "VND/USD", "sbv_reference_via_algolab"
        ),
    }
    integrated: list[dict[str, Any]] = []
    missing_features = Counter()
    for fuel in fuel_rows:
        event_date = datetime.fromisoformat(fuel["effective_at_local"]).date()
        row: dict[str, Any] = {
            "product_id": fuel["product_id"],
            "source_product_name": fuel["source_product_name"],
            "source_record_date": fuel["source_record_date"],
            "effective_at_local": fuel["effective_at_local"],
            "price_value": fuel["price_value"],
            "price_change_value": fuel["price_change_value"],
            "stabilization_fund_subsidy": fuel["stabilization_fund_subsidy"],
            "stabilization_fund_subsidy_amount": fuel["stabilization_fund_subsidy_amount"],
            "stabilization_fund_subsidy_unit": fuel["stabilization_fund_subsidy_unit"],
            "stabilization_fund_source_url": fuel["stabilization_fund_source_url"],
            "unit_code": fuel["unit_code"],
            "target_source_id": fuel["source_id"],
            "target_snapshot_id": fuel["snapshot_id"],
            "schema_version": SCHEMA_VERSION,
        }
        for series_id, (date_field, value_field, age_field, unit, source_id) in series_specs.items():
            source_series = market_by_series.get(series_id, {"rows": [], "dates": []})
            available = source_series["rows"]
            available_dates = source_series["dates"]
            index = bisect.bisect_left(available_dates, event_date.isoformat()) - 1
            prefix = "brent" if series_id == "brent_dated_usd_bbl" else "fx"
            row[f"{prefix}_source_id"] = source_id
            if index < 0:
                row[date_field] = ""
                row[value_field] = ""
                row[age_field] = ""
                row[f"{prefix}_snapshot_id"] = ""
                missing_features[series_id] += 1
            else:
                selected = available[index]
                observed_date = date.fromisoformat(selected["observation_date"])
                row[date_field] = selected["observation_date"]
                row[value_field] = selected["value"]
                row[age_field] = (event_date - observed_date).days
                row[f"{prefix}_snapshot_id"] = selected["snapshot_id"]
            row[f"{value_field}_unit"] = unit
        integrated.append(row)
    return integrated, dict(sorted(missing_features.items()))


def run_pipeline(
    raw_dir: Path,
    processed_dir: Path,
    log_dir: Path,
    config_path: Path,
) -> dict[str, Any]:
    logger = _configure_logger(log_dir / "pipeline.log")
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        config = _load_config(config_path)
        files = [raw_dir / BRENT_FILE, raw_dir / FX_FILE, raw_dir / FUEL_FILE]
        missing_files = [path.name for path in files if not path.is_file()]
        if missing_files:
            raise PipelineError(f"missing required raw source files: {missing_files}")

        annotation_path = raw_dir.parent / "manual" / "stabilization_fund_annotations.csv"
        file_hashes = {path.name: _sha256(path) for path in files}
        if annotation_path.is_file():
            file_hashes["manual/stabilization_fund_annotations.csv"] = _sha256(annotation_path)
        pipeline_hash = _sha256(Path(__file__))
        run_seed = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "pipeline_sha256": pipeline_hash,
            "config": config,
            "files": file_hashes,
        }, sort_keys=True, separators=(",", ":"))
        run_id = hashlib.sha256(run_seed.encode("utf-8")).hexdigest()[:20]
        logger.info("run_id=%s start schema=%s", run_id, SCHEMA_VERSION)

        brent, brent_q, brent_quality = _parse_market(
            files[0], "eia_fred", "brent_dated_usd_bbl", "price_usd_per_barrel",
            "unit", "USD/bbl", "Dollars per Barrel", "observation_status",
            file_hashes[files[0].name], EXPECTED_HEADERS[BRENT_FILE],
        )
        fx, fx_q, fx_quality = _parse_market(
            files[1], "sbv_reference_via_algolab", "usd_vnd_sbv_reference",
            "rate_vnd_per_usd", "unit", "VND/USD", "VND per 1 USD", None,
            file_hashes[files[1].name], EXPECTED_HEADERS[FX_FILE],
        )
        fuel, fuel_q, fuel_quality = _parse_fuel(
            files[2], file_hashes[files[2].name], EXPECTED_HEADERS[FUEL_FILE]
        )
        fund_annotations_applied = _apply_fund_annotations(fuel, annotation_path)

        market_rows, market_duplicates = _dedupe_or_fail(
            brent + fx,
            ("series_id", "observation_date", "source_id", "snapshot_id"),
            "market_observation",
        )
        fuel, fuel_duplicates = _dedupe_or_fail(
            fuel,
            ("product_id", "effective_at_local", "source_id", "snapshot_id"),
            "fuel_price_observation",
        )
        panel_rows, missing_prior_market = _integrate_fuel_market(fuel, market_rows)
        all_quarantine = brent_q + fx_q + fuel_q

        # Load source row counts and schema fingerprints for a complete provenance manifest.
        manifests = []
        manifest_specs = [
            (files[0], "eia_fred", "EIA, distributed by FRED", brent_quality["schema_fingerprint_sha256"]),
            (files[1], "sbv_reference_via_algolab", "Algolab attributes values to GSO / SBV", fx_quality["schema_fingerprint_sha256"]),
            (files[2], "pvoil", "PVOIL", fuel_quality["schema_fingerprint_sha256"]),
        ]
        for path, source_id, publisher, fingerprint in manifest_specs:
            manifests.append(_manifest_entry(path, source_id, publisher, fingerprint))
        if annotation_path.is_file():
            with annotation_path.open("r", encoding="utf-8-sig", newline="") as stream:
                annotation_count = sum(1 for _ in csv.DictReader(stream))
            manifests.append({
                "snapshot_id": file_hashes["manual/stabilization_fund_annotations.csv"],
                "source_id": "moit_manual_annotations",
                "source_url": "https://moit.gov.vn/",
                "publisher_or_attribution": "Ministry of Industry and Trade; manually transcribed adjustment notices",
                "license_reuse_terms": "official-source attribution required",
                "retrieved_at_utc": started_at,
                "raw_filename": "manual/stabilization_fund_annotations.csv",
                "sha256": file_hashes["manual/stabilization_fund_annotations.csv"],
                "row_count": annotation_count,
                "schema_fingerprint_sha256": hashlib.sha256(
                    b"product_id,source_record_date,amount,unit,source_url"
                ).hexdigest(),
            })

        quality = {
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "pipeline_sha256": pipeline_hash,
            "started_at_utc": started_at,
            "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source_checks": {
                "brent": brent_quality,
                "usd_vnd": fx_quality,
                "fuel_prices": fuel_quality,
            },
            "integrated": {
                "market_observation_rows": len(market_rows),
                "fuel_price_observation_rows": len(fuel),
                "fuel_market_panel_rows": len(panel_rows),
                "stabilization_fund_annotations_applied": fund_annotations_applied,
                "fuel_events_without_prior_market_observation": missing_prior_market,
                "exact_duplicate_keys_collapsed": (
                    brent_quality["exact_duplicates_collapsed"]
                    + fx_quality["exact_duplicates_collapsed"]
                    + fuel_quality["exact_duplicates_collapsed"]
                    + market_duplicates
                    + fuel_duplicates
                ),
                "quarantined_rows": len(all_quarantine),
                "quarantine_reason_counts": dict(sorted(Counter(
                    row["reason_code"] for row in all_quarantine
                ).items())),
                "market_coverage_by_series": {
                    series: {
                        "first": min(row["observation_date"] for row in market_rows if row["series_id"] == series),
                        "last": max(row["observation_date"] for row in market_rows if row["series_id"] == series),
                        "rows": sum(row["series_id"] == series for row in market_rows),
                    }
                    for series in sorted({row["series_id"] for row in market_rows})
                },
            },
            "publication_status": "validated_with_quarantine" if all_quarantine else "validated",
            "sources": manifests,
        }

        processed_dir.mkdir(parents=True, exist_ok=True)
        runs_dir = processed_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_dir = runs_dir / run_id
        staging_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=runs_dir))
        try:
            _write_csv(staging_dir / "market_observation.csv", market_rows, [
                "series_id", "observation_date", "value", "unit_code", "observation_status",
                "source_id", "snapshot_id", "source_value", "source_url", "retrieved_at_utc",
                "schema_version",
            ])
            _write_csv(staging_dir / "fuel_price_observation.csv", fuel, [
                "product_id", "source_product_name", "source_record_date", "effective_at_local",
                "effective_time_24_00_normalized", "price_value", "unit_code",
                "price_change_value", "stabilization_fund_subsidy", "tax_included",
                "stabilization_fund_subsidy_amount", "stabilization_fund_subsidy_unit",
                "stabilization_fund_source_url",
                "source_id", "snapshot_id", "source_url",
                "retrieved_at_utc", "schema_version",
            ])
            _write_csv(staging_dir / "fuel_market_panel.csv", panel_rows, [
                "product_id", "source_product_name", "source_record_date", "effective_at_local",
                "price_value", "price_change_value", "stabilization_fund_subsidy",
                "stabilization_fund_subsidy_amount", "stabilization_fund_subsidy_unit",
                "stabilization_fund_source_url",
                "unit_code", "target_source_id",
                "target_snapshot_id", "brent_source_id", "brent_snapshot_id",
                "brent_observation_date", "brent_usd_per_barrel", "brent_age_days",
                "brent_usd_per_barrel_unit", "fx_source_id", "fx_snapshot_id",
                "fx_observation_date", "usd_vnd_rate", "fx_age_days", "usd_vnd_rate_unit",
                "schema_version",
            ], lineterminator="\n")
            _save_quarantine(staging_dir / "quarantine.csv", all_quarantine)

            csv_outputs = [
                ("market_observation.csv", len(market_rows)),
                ("fuel_price_observation.csv", len(fuel)),
                ("fuel_market_panel.csv", len(panel_rows)),
                ("quarantine.csv", len(all_quarantine)),
            ]
            output_manifest = {
                filename: {"rows": row_count, "sha256": _sha256(staging_dir / filename)}
                for filename, row_count in csv_outputs
            }
            quality["outputs"] = output_manifest
            _json_write(staging_dir / "quality_report.json", quality)
            _json_write(staging_dir / "manifest.json", {
                "run_id": run_id,
                "schema_version": SCHEMA_VERSION,
                "pipeline_version": PIPELINE_VERSION,
                "pipeline_sha256": pipeline_hash,
                "sources": manifests,
                "outputs": output_manifest,
            })

            if run_dir.exists():
                for filename, expected in output_manifest.items():
                    existing = run_dir / filename
                    if not existing.is_file() or _sha256(existing) != expected["sha256"]:
                        raise PipelineError(
                            f"existing run {run_id} is not reproducible: output mismatch in {filename}"
                        )
                shutil.rmtree(staging_dir)
            else:
                os.replace(staging_dir, run_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        _atomic_json(processed_dir / "current.json", {
            "run_id": run_id,
            "path": f"runs/{run_id}",
            "schema_version": SCHEMA_VERSION,
            "published_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        })
        logger.info(
            "run_id=%s published market_rows=%d fuel_rows=%d quarantined=%d",
            run_id, len(market_rows), len(fuel), len(all_quarantine),
        )
        return quality
    except Exception as exc:
        logger.exception("pipeline failed: %s", exc)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Build canonical PetroTwin datasets from raw CSV snapshots")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--log-dir", type=Path, default=Path("data/interim/logs"))
    parser.add_argument("--config", type=Path, default=Path("configs/default.yaml"))
    args = parser.parse_args()
    try:
        result = run_pipeline(args.raw_dir, args.processed_dir, args.log_dir, args.config)
    except PipelineError:
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
