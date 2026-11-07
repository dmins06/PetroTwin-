"""Validate and publish one accepted processed snapshot to the M2 MySQL schema.

The module consumes the existing immutable CSV handoff. It does not clean or
rewrite source data. Run without ``--load`` to perform an offline preflight;
database writes require the explicit ``--load`` flag and PETROTWIN_DB_* values.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class LoadError(Exception):
    """Raised when the accepted snapshot cannot be safely published."""


KNOWN_PRODUCTS: frozenset[str] = frozenset({
    "diesel_0005s_iv",
    "diesel_005s_ii",
    "diesel_0001s_v",
    "fuel_oil_35s",
    "fuel_oil_3s",
    "kerosene_ko",
    "gasoline_e5_ron92_ii",
    "gasoline_e10_ron95_iii",
    "gasoline_ron95_iii",
    "gasoline_ron95_iv",
})

KNOWN_SERIES: frozenset[str] = frozenset({
    "brent_dated_usd_bbl",
    "usd_vnd_sbv_reference",
})


def _sha256(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    exact = hashlib.sha256(data).hexdigest()
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return exact, hashlib.sha256(normalized).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise LoadError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def _expected_file(root: Path, path: Path, expected_hash: str, label: str) -> None:
    if not path.is_file():
        raise LoadError(f"Missing {label}: {path}")
    exact, normalized = _sha256(path)
    if expected_hash not in {exact, normalized}:
        raise LoadError(f"SHA-256 mismatch for {label}: expected {expected_hash}, got {exact}")


def _nonempty(value: str | None, field: str, row_number: int) -> str:
    result = (value or "").strip()
    if not result:
        raise LoadError(f"row {row_number}: required value {field!r} is blank")
    return result


def _date(value: str | None, field: str, row_number: int) -> date:
    try:
        return date.fromisoformat(_nonempty(value, field, row_number))
    except ValueError as exc:
        raise LoadError(f"row {row_number}: invalid date in {field!r}: {value!r}") from exc


def _datetime(value: str | None, field: str, row_number: int) -> datetime:
    text = _nonempty(value, field, row_number)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LoadError(f"row {row_number}: invalid datetime in {field!r}: {text!r}") from exc
    # The SQL contract stores local event wall-time in DATETIME, with timezone
    # recorded in a separate column. UTC timestamps are normalized to naive UTC.
    if parsed.tzinfo is None:
        raise LoadError(f"row {row_number}: {field!r} must include an offset")
    return parsed.replace(tzinfo=None) if field == "effective_at_local" else (
        parsed.astimezone(timezone.utc).replace(tzinfo=None)
    )


def _decimal(value: str | None) -> Decimal | None:
    text = (value or "").strip()
    if not text:
        return None
    if "," in text:
        if not re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?", text):
            raise LoadError(f"invalid decimal grouping: {value!r}")
        text = text.replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise LoadError(f"invalid decimal value: {value!r}") from exc


def _integer(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text else None


def _boolean(value: str | None) -> bool | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise LoadError(f"invalid boolean value: {value!r}")


def _row_count(path: Path) -> int:
    return len(_csv_rows(path)[1])


def prepare_bundle(repo: Path | None = None) -> dict[str, Any]:
    """Resolve current.json and validate manifests, CSVs, key grains, and joins."""
    root = (repo or Path.cwd()).resolve()
    pointer_path = root / "data/processed/current.json"
    if not pointer_path.is_file():
        raise LoadError(f"Open the repository root; not found: {pointer_path}")
    pointer = _read_json(pointer_path)
    run_dir = (root / "data/processed" / pointer["path"]).resolve()
    if not run_dir.is_relative_to((root / "data/processed").resolve()):
        raise LoadError("current.json path resolves outside data/processed")
    manifest_path = run_dir / "manifest.json"
    quality_path = run_dir / "quality_report.json"
    manifest, quality = _read_json(manifest_path), _read_json(quality_path)
    run_id = pointer["run_id"]
    if manifest.get("run_id") != run_id or quality.get("run_id") != run_id:
        raise LoadError("current.json, manifest, and quality report run IDs differ")
    if pointer.get("schema_version") != manifest.get("schema_version"):
        raise LoadError("current.json and manifest schema versions differ")
    status = quality.get("publication_status")
    if status not in {
        "validated",
        "validated_with_quarantine",
        "validated_with_optional_source_missing",
        "validated_with_optional_source_stale",
    }:
        raise LoadError(f"run is not accepted for publication (status={status!r})")

    outputs: dict[str, list[dict[str, str]]] = {}
    for filename, info in manifest.get("outputs", {}).items():
        path = run_dir / filename
        _expected_file(root, path, info["sha256"], filename)
        rows = _csv_rows(path)[1]
        if len(rows) != int(info["rows"]):
            raise LoadError(f"{filename}: {len(rows)} rows, manifest expects {info['rows']}")
        outputs[filename] = rows
    if set(outputs) != {
        "fuel_market_panel.csv", "fuel_price_observation.csv",
        "market_observation.csv", "quarantine.csv",
    }:
        raise LoadError(f"unexpected output set in manifest: {sorted(outputs)}")

    source_by_id: dict[str, dict[str, Any]] = {}
    source_by_filename: dict[str, dict[str, Any]] = {}
    source_by_snapshot: dict[str, dict[str, Any]] = {}
    for source in manifest.get("sources", []):
        raw_name = source["raw_filename"]
        source_path = root / "data" / ("manual" if raw_name.startswith("manual/") else "raw")
        if raw_name.startswith("manual/"):
            source_path /= raw_name.removeprefix("manual/")
        else:
            source_path /= raw_name
        _expected_file(root, source_path, source["sha256"], f"source {raw_name}")
        source_headers, source_rows = _csv_rows(source_path)
        if len(source_rows) != int(source["row_count"]):
            raise LoadError(f"source row count mismatch for {raw_name}")
        fingerprint_input = (
            ",".join(source_headers)
            if raw_name.startswith("manual/")
            else "\x1f".join(source_headers)
        )
        fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        if fingerprint != source["schema_fingerprint_sha256"]:
            raise LoadError(f"source schema fingerprint mismatch for {raw_name}")
        if source["snapshot_id"] != source["sha256"]:
            raise LoadError(f"source snapshot id/hash mismatch for {raw_name}")
        if source["source_id"] in source_by_id:
            raise LoadError(f"duplicate source_id in manifest: {source['source_id']}")
        source_by_id[source["source_id"]] = source
        source_by_filename[Path(raw_name).name] = source
        source_by_snapshot[source["snapshot_id"]] = source

    if "pvoil" not in source_by_id:
        raise LoadError("missing required domestic fuel source 'pvoil' in manifest")
    if "eia_fred" not in source_by_id:
        raise LoadError("missing required Brent source 'eia_fred' in manifest")

    annotation_path = root / "data/manual/stabilization_fund_annotations.csv"
    annotation_header, annotations = _csv_rows(annotation_path)
    if annotation_header != ["product_id", "source_record_date", "amount", "unit", "source_url"]:
        raise LoadError("manual fund annotation schema differs from pipeline contract")
    annotation_source = source_by_id.get("moit_manual_annotations")

    published_at = _date_or_naive_utc(_nonempty(pointer.get("published_at_utc"), "published_at_utc", 0), 0)
    if published_at is None:
        raise LoadError("published_at_utc must be a valid UTC timestamp")

    bundle = {
        "root": root, "pointer": pointer, "run_dir": run_dir, "manifest": manifest,
        "quality": quality, "outputs": outputs, "sources": source_by_id,
        "annotations": annotations, "annotation_source": annotation_source,
        "published_at_utc": published_at,
    }
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle: dict[str, Any]) -> None:
    """Validate cross-dataset integrity, provenance, reference seeds, and constraints."""
    manifest = bundle.get("manifest") or {}
    quality = bundle.get("quality") or {}
    outputs = bundle.get("outputs") or {}
    sources = bundle.get("sources") or {}
    source_by_snapshot = {s["snapshot_id"]: s for s in sources.values()}
    source_by_filename = {Path(s["raw_filename"]).name: s for s in sources.values()}

    if "pvoil" not in sources:
        raise LoadError("missing required domestic fuel source 'pvoil' in manifest")
    if "eia_fred" not in sources:
        raise LoadError("missing required Brent source 'eia_fred' in manifest")

    expected_counts = quality.get("outputs", {})
    for filename, rows in outputs.items():
        quality_entry = expected_counts.get(filename)
        if not quality_entry or int(quality_entry["rows"]) != len(rows):
            raise LoadError(f"quality report count mismatch for {filename}")

    fuel = outputs.get("fuel_price_observation.csv", [])
    market = outputs.get("market_observation.csv", [])
    panel = outputs.get("fuel_market_panel.csv", [])
    quarantine = outputs.get("quarantine.csv", [])
    keys = {
        "fuel": set(), "market": set(), "panel": set(), "quarantine": set(),
    }
    for index, row in enumerate(fuel, start=2):
        pid = _nonempty(row.get("product_id"), "product_id", index)
        if pid not in KNOWN_PRODUCTS:
            raise LoadError(f"fuel row {index}: unknown product_id {pid!r} not in reference seeds")
        snap_id = _nonempty(row.get("snapshot_id"), "snapshot_id", index)
        if snap_id not in source_by_snapshot:
            raise LoadError(f"fuel row {index} references an unknown snapshot: {snap_id}")
        keys["fuel"].add((pid, _nonempty(row.get("effective_at_local"), "effective_at_local", index), snap_id))
        _date(row.get("source_record_date"), "source_record_date", index)
        _datetime(row.get("effective_at_local"), "effective_at_local", index)
        _date_or_naive_utc(row.get("retrieved_at_utc"), index)
        for field in ("price_value", "price_change_value", "stabilization_fund_subsidy_amount"):
            _decimal(row.get(field))

    for index, row in enumerate(market, start=2):
        series_id = _nonempty(row.get("series_id"), "series_id", index)
        if series_id not in KNOWN_SERIES:
            raise LoadError(f"market row {index}: unknown series_id {series_id!r} not in reference seeds")
        snap_id = _nonempty(row.get("snapshot_id"), "snapshot_id", index)
        if snap_id not in source_by_snapshot:
            raise LoadError(f"market row {index} references an unknown snapshot: {snap_id}")
        keys["market"].add((series_id, _nonempty(row.get("observation_date"), "observation_date", index), snap_id))
        _date(row.get("observation_date"), "observation_date", index)
        if row.get("observation_status") not in {"observed", "missing"}:
            raise LoadError(f"market row {index}: invalid observation_status")
        if row["observation_status"] == "missing" and (row.get("value") or "").strip():
            raise LoadError(f"market row {index}: missing observation must retain NULL value")
        if row["observation_status"] == "observed" and not (row.get("value") or "").strip():
            raise LoadError(f"market row {index}: observed value is blank")
        _decimal(row.get("value"))
        _decimal(row.get("source_value"))

    for index, row in enumerate(panel, start=2):
        pid = _nonempty(row.get("product_id"), "product_id", index)
        if pid not in KNOWN_PRODUCTS:
            raise LoadError(f"panel row {index}: unknown product_id {pid!r} not in reference seeds")
        effective_at = _nonempty(row.get("effective_at_local"), "effective_at_local", index)
        target_snapshot = _nonempty(row.get("target_snapshot_id"), "target_snapshot_id", index)
        target_source = _nonempty(row.get("target_source_id"), "target_source_id", index)

        # Target provenance check
        target_src_entry = source_by_snapshot.get(target_snapshot)
        if target_src_entry is None:
            raise LoadError(f"panel row {index}: target_snapshot_id {target_snapshot!r} is not in manifest")
        if target_src_entry["source_id"] != target_source:
            raise LoadError(
                f"panel row {index}: target provenance mismatch: "
                f"target_source_id {target_source!r} does not match snapshot source {target_src_entry['source_id']!r}"
            )

        keys["panel"].add((pid, effective_at, target_snapshot))
        event_dt = _datetime(effective_at, "effective_at_local", index)
        _date(row.get("source_record_date"), "source_record_date", index)
        for field in ("price_value", "price_change_value", "stabilization_fund_subsidy_amount"):
            _decimal(row.get(field))

        # Brent and FX provenance and observation validation
        for prefix in ("brent", "fx"):
            src_id = (row.get(f"{prefix}_source_id") or "").strip()
            snapshot_id = (row.get(f"{prefix}_snapshot_id") or "").strip()
            obs_date = (row.get(f"{prefix}_observation_date") or "").strip()
            age_raw = (row.get(f"{prefix}_age_days") or "").strip()
            value_field = "brent_usd_per_barrel" if prefix == "brent" else "usd_vnd_rate"
            value_raw = (row.get(value_field) or "").strip()

            if not snapshot_id:
                if obs_date:
                    raise LoadError(f"panel row {index}: {prefix}_snapshot_id is missing but {prefix}_observation_date is present")
                if value_raw:
                    raise LoadError(f"panel row {index}: {prefix}_snapshot_id is missing but {value_field} is present")
                if age_raw:
                    raise LoadError(f"panel row {index}: {prefix}_snapshot_id is missing but {prefix}_age_days is present")
            else:
                src_entry = source_by_snapshot.get(snapshot_id)
                if src_entry is None:
                    raise LoadError(f"panel row {index}: {prefix}_snapshot_id {snapshot_id!r} is not in manifest")
                if not src_id:
                    raise LoadError(f"panel row {index}: {prefix}_source_id is blank for present snapshot")
                if src_entry["source_id"] != src_id:
                    raise LoadError(
                        f"panel row {index}: {prefix} provenance mismatch: "
                        f"source_id {src_id!r} does not match snapshot source {src_entry['source_id']!r}"
                    )
                if not obs_date:
                    raise LoadError(f"panel row {index}: {prefix}_snapshot_id is present but {prefix}_observation_date is blank")
                parsed_date = _date(obs_date, f"{prefix}_observation_date", index)
                if parsed_date >= event_dt.date():
                    raise LoadError(
                        f"panel row {index}: {prefix} observation date {parsed_date} "
                        f"is not strictly prior to fuel event date {event_dt.date()}"
                    )
                if not age_raw:
                    raise LoadError(f"panel row {index}: {prefix}_snapshot_id is present but {prefix}_age_days is blank")
                age = _integer(age_raw)
                expected_age = (event_dt.date() - parsed_date).days
                if age is None or age != expected_age or age < 1:
                    raise LoadError(f"panel row {index}: invalid {prefix}_age_days: {age_raw!r} (expected {expected_age})")
                if not value_raw:
                    raise LoadError(f"panel row {index}: {prefix}_snapshot_id is present but {value_field} is blank")
                val = _decimal(value_raw)
                if val is None or val <= 0:
                    raise LoadError(f"panel row {index}: invalid {value_field}: {value_raw!r}")

    for index, row in enumerate(quarantine, start=2):
        source = source_by_filename.get(Path(_nonempty(row.get("source_file"), "source_file", index)).name)
        if source is None:
            raise LoadError(f"quarantine row {index}: source file is not in manifest")
        raw = json.loads(_nonempty(row.get("raw_row_json"), "raw_row_json", index))
        if not isinstance(raw, dict):
            raise LoadError(f"quarantine row {index}: raw_row_json must be an object")
        keys["quarantine"].add((source["source_id"], _nonempty(row.get("source_row_number"), "source_row_number", index)))
    if any(len(keys[name]) != expected for name, expected in {
        "fuel": len(fuel), "market": len(market), "panel": len(panel),
        "quarantine": len(quarantine),
    }.items()):
        raise LoadError("duplicate primary keys detected in accepted outputs")
    if len(fuel) != len(panel):
        raise LoadError(f"panel cardinality mismatch: {len(panel)} vs {len(fuel)} fuel events")
    if len(quarantine) != int(quality.get("integrated", {}).get("quarantined_rows", -1)):
        raise LoadError("quarantine row count differs from quality report")

    annotations = bundle.get("annotations") or []
    annotation_source = bundle.get("annotation_source")
    if annotation_source is None or len(annotations) != int(annotation_source["row_count"]):
        raise LoadError("fund annotation rows differ from manifest")
    annotation_keys: set[tuple[str, str]] = set()
    fuel_keys = {(r["product_id"], r["source_record_date"]) for r in fuel}
    for index, row in enumerate(annotations, start=2):
        pid = _nonempty(row.get("product_id"), "product_id", index)
        if pid not in KNOWN_PRODUCTS:
            raise LoadError(f"annotation row {index}: invalid product_id {pid!r} not in reference seeds")
        rec_date = _date(row.get("source_record_date"), "source_record_date", index)
        key = (pid, rec_date.isoformat())
        if key in annotation_keys:
            raise LoadError(f"annotation row {index}: duplicate key grain (run_id, product_id, source_record_date): {key!r}")
        annotation_keys.add(key)
        amount = _decimal(row.get("amount"))
        if amount is None or amount < 0 or (row.get("unit") or "").strip() not in {"VND/kg", "VND/liter"}:
            raise LoadError(f"annotation row {index}: invalid amount or unit")
        if not (row.get("source_url") or "").startswith("https://") or key not in fuel_keys:
            raise LoadError(f"annotation row {index}: invalid evidence URL or orphan key")

    published_at = bundle.get("published_at_utc")
    if published_at is None:
        raise LoadError("published_at_utc must be a valid UTC timestamp")


def _date_or_naive_utc(value: str | None, row_number: int) -> datetime | None:
    if not value:
        return None
    return _datetime(value, "retrieved_at_utc", row_number)


def _source_values(source: dict[str, Any]) -> tuple[Any, ...]:
    retrievals = source["retrieved_at_utc"]
    if not isinstance(retrievals, list):
        retrievals = [retrievals]
    parsed = [_datetime(str(v), "retrieved_at_utc", 0) for v in retrievals]
    parsed = [v for v in parsed if v is not None]
    if not parsed:
        raise LoadError(f"source {source['source_id']} has no retrieval timestamp")
    return (
        source["snapshot_id"], source["source_id"], source["raw_filename"],
        source.get("source_url") or "", source.get("publisher_or_attribution"),
        source.get("license_reuse_terms"), source["sha256"],
        source["schema_fingerprint_sha256"], int(source["row_count"]),
        json.dumps(retrievals), min(parsed), max(parsed),
    )


def _insert_many(cursor: Any, sql: str, rows: list[tuple[Any, ...]]) -> None:
    if rows:
        cursor.executemany(sql, rows)


def _mysql_connection_config(pymysql: Any) -> dict[str, Any]:
    config = {"host": os.getenv("PETROTWIN_DB_HOST", "127.0.0.1"), "port": int(os.getenv("PETROTWIN_DB_PORT", "3306")), "database": os.getenv("PETROTWIN_DB_NAME", "petrotwin"), "user": os.getenv("PETROTWIN_DB_USER", "root"), "password": os.getenv("PETROTWIN_DB_PASSWORD", ""), "charset": "utf8mb4", "autocommit": False, "cursorclass": pymysql.cursors.DictCursor}
    if os.getenv("PETROTWIN_DB_SSL", "false").strip().lower() not in {"true", "1", "yes"}: return config
    ca_path = os.getenv("PETROTWIN_DB_SSL_CA", "/etc/ssl/certs/ca-certificates.crt")
    if not Path(ca_path).is_file(): raise LoadError(f"TiDB TLS CA bundle does not exist: {ca_path}")
    config.update({"ssl_ca": ca_path, "ssl_verify_cert": True, "ssl_verify_identity": True})
    return config


def publish(bundle: dict[str, Any]) -> str:
    """Publish a validated run atomically, without printing credential values."""
    try:
        import pymysql
    except ImportError as exc:
        raise LoadError("Install the optional connector with: python -m pip install -e '.[mysql]'") from exc
    config = _mysql_connection_config(pymysql)
    if not config["password"]:
        raise LoadError("Set PETROTWIN_DB_PASSWORD in your local environment before --load")
    manifest, quality = bundle["manifest"], bundle["quality"]
    run_id, status = manifest["run_id"], quality["publication_status"]
    outputs, sources = bundle["outputs"], bundle["sources"]
    fuel, market, panel, quarantine = (outputs[n] for n in (
        "fuel_price_observation.csv", "market_observation.csv",
        "fuel_market_panel.csv", "quarantine.csv",
    ))
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cur:
            connection.begin()
            cur.execute(
                "SELECT run_id, schema_version, pipeline_version, pipeline_sha256, "
                "publication_status, manifest_json FROM dataset_run WHERE run_id=%s FOR UPDATE",
                (run_id,)
            )
            existing = cur.fetchone()
            if existing:
                if existing.get("publication_status") != status:
                    raise LoadError("run_id exists with a different publication status; refusing repair")
                if existing.get("schema_version") != manifest["schema_version"]:
                    raise LoadError("existing run schema_version does not match current bundle; refusing repair")
                if existing.get("pipeline_version") != manifest["pipeline_version"]:
                    raise LoadError("existing run pipeline_version does not match current bundle; refusing repair")
                if existing.get("pipeline_sha256") != manifest["pipeline_sha256"]:
                    raise LoadError("existing run pipeline_sha256 does not match current bundle; refusing repair")

                db_manifest = existing.get("manifest_json")
                if isinstance(db_manifest, str):
                    db_manifest = json.loads(db_manifest)
                if not isinstance(db_manifest, dict):
                    raise LoadError("existing run manifest_json is invalid; refusing repair")
                if db_manifest.get("outputs") != manifest["outputs"] or db_manifest.get("sources") != manifest["sources"]:
                    raise LoadError("existing run manifest outputs or sources do not match current bundle; refusing repair")

                cur.execute("SELECT report_json FROM quality_report WHERE run_id=%s", (run_id,))
                q_row = cur.fetchone()
                if not q_row:
                    raise LoadError("existing run has no quality_report; refusing repair")
                db_quality = q_row.get("report_json")
                if isinstance(db_quality, str):
                    db_quality = json.loads(db_quality)
                if not isinstance(db_quality, dict) or db_quality.get("publication_status") != status or db_quality.get("run_id") != run_id:
                    raise LoadError("existing run quality_report does not match current bundle; refusing repair")

                cur.execute("SELECT snapshot_id FROM dataset_run_source WHERE run_id=%s", (run_id,))
                existing_snapshots = {r["snapshot_id"] for r in cur.fetchall()}
                expected_snapshots = {s["snapshot_id"] for s in sources.values()}
                if existing_snapshots != expected_snapshots:
                    raise LoadError("existing run snapshots do not match current bundle; refusing repair")

                for table, expected in (
                    ("fuel_price_observation", len(fuel)), ("market_observation", len(market)),
                    ("fuel_market_panel", len(panel)), ("quarantine_record", len(quarantine)),
                    ("fund_annotation", len(bundle["annotations"])),
                ):
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE run_id=%s", (run_id,))
                    if int(cur.fetchone()["n"]) != expected:
                        raise LoadError(f"existing run is incomplete: {table} count mismatch; no auto-repair")
                connection.rollback()
                return f"run {run_id} already exists and its data/metadata reconcile; no rows changed"

            try:
                cur.execute("""INSERT INTO dataset_run
                    (run_id,schema_version,pipeline_version,pipeline_sha256,publication_status,
                     started_at_utc,completed_at_utc,manifest_json,created_at_utc)
                    VALUES (%s,%s,%s,%s,'building',%s,%s,%s,%s)""",
                    (run_id, manifest["schema_version"], manifest["pipeline_version"],
                     manifest["pipeline_sha256"], bundle["published_at_utc"],
                     bundle["published_at_utc"], json.dumps(manifest, ensure_ascii=False),
                     bundle["published_at_utc"]))
            except Exception as exc:
                msg = str(exc).lower()
                if isinstance(exc, getattr(pymysql, "IntegrityError", ())) or "duplicate" in msg or "1062" in msg or "unique" in msg:
                    raise LoadError(f"concurrent publication conflict: run_id {run_id} is already being published") from exc
                raise

            _insert_many(cur, """INSERT INTO source_snapshot
                (snapshot_id,source_id,raw_filename,source_url,publisher_or_attribution,
                 license_reuse_terms,sha256,schema_fingerprint_sha256,source_row_count,
                 retrieval_times_utc,retrieved_at_utc_min,retrieved_at_utc_max)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE snapshot_id=snapshot_id""",
                [_source_values(s) for s in sources.values()])
            _insert_many(cur, "INSERT INTO dataset_run_source (run_id,snapshot_id) VALUES (%s,%s)",
                         [(run_id, s["snapshot_id"]) for s in sources.values()])
            cur.execute("SELECT product_id FROM fuel_product")
            known_products = {r["product_id"] for r in cur.fetchall()}
            cur.execute("SELECT series_id FROM market_series")
            known_series = {r["series_id"] for r in cur.fetchall()}
            products = {r["product_id"] for r in fuel + panel}
            series_ids = {r["series_id"] for r in market}
            if not products <= known_products or not series_ids <= known_series:
                raise LoadError("accepted CSV uses product/series IDs missing from MySQL reference seeds")

            _insert_many(cur, """INSERT INTO market_observation
                (run_id,series_id,observation_date,snapshot_id,value,unit_code,observation_status,
                 source_value,source_url,retrieved_at_utc) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(run_id, r["series_id"], _date(r["observation_date"], "observation_date", i),
                  r["snapshot_id"], _decimal(r["value"]), r["unit_code"], r["observation_status"],
                  _decimal(r["source_value"]), r["source_url"] or None,
                  _date_or_naive_utc(r["retrieved_at_utc"], i)) for i, r in enumerate(market, 2)])
            _insert_many(cur, """INSERT INTO fuel_price_observation
                (run_id,product_id,source_record_date,effective_at_local,local_timezone,
                 effective_time_24_00_normalized,snapshot_id,source_product_name,price_value,
                 unit_code,price_change_value,stabilization_fund_subsidy,
                 stabilization_fund_subsidy_amount,stabilization_fund_subsidy_unit,
                 stabilization_fund_source_url,tax_included,source_url,retrieved_at_utc)
                VALUES (%s,%s,%s,%s,'Asia/Ho_Chi_Minh',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(run_id, r["product_id"], _date(r["source_record_date"], "source_record_date", i),
                  _datetime(r["effective_at_local"], "effective_at_local", i),
                  _boolean(r["effective_time_24_00_normalized"]), r["snapshot_id"],
                  r["source_product_name"], _decimal(r["price_value"]), r["unit_code"],
                  _decimal(r["price_change_value"]), _integer(r["stabilization_fund_subsidy"]),
                  _decimal(r["stabilization_fund_subsidy_amount"]),
                  r["stabilization_fund_subsidy_unit"] or None,
                  r["stabilization_fund_source_url"] or None, _boolean(r["tax_included"]),
                  r["source_url"] or None, _date_or_naive_utc(r["retrieved_at_utc"], i))
                 for i, r in enumerate(fuel, 2)])
            _insert_many(cur, """INSERT INTO fund_annotation
                (run_id,product_id,source_record_date,snapshot_id,fund_amount,unit_code,evidence_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                [(run_id, r["product_id"], _date(r["source_record_date"], "source_record_date", i),
                  bundle["annotation_source"]["snapshot_id"], _decimal(r["amount"]), r["unit"], r["source_url"])
                 for i, r in enumerate(bundle["annotations"], 2)])
            _insert_many(cur, """INSERT INTO fuel_market_panel
                (run_id,product_id,effective_at_local,local_timezone,target_source_id,target_snapshot_id,
                 source_record_date,price_value,price_change_value,unit_code,stabilization_fund_subsidy,
                 stabilization_fund_subsidy_amount,stabilization_fund_subsidy_unit,
                 stabilization_fund_source_url,brent_source_id,brent_snapshot_id,brent_observation_date,
                 brent_usd_per_barrel,brent_age_days,brent_unit,fx_source_id,fx_snapshot_id,
                 fx_observation_date,usd_vnd_rate,fx_age_days,fx_unit)
                VALUES (%s,%s,%s,'Asia/Ho_Chi_Minh',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(run_id, r["product_id"], _datetime(r["effective_at_local"], "effective_at_local", i),
                  r["target_source_id"], r["target_snapshot_id"], _date(r["source_record_date"], "source_record_date", i),
                  _decimal(r["price_value"]), _decimal(r["price_change_value"]), r["unit_code"],
                  _integer(r["stabilization_fund_subsidy"]), _decimal(r["stabilization_fund_subsidy_amount"]),
                  r["stabilization_fund_subsidy_unit"] or None, r["stabilization_fund_source_url"] or None,
                  r["brent_source_id"], r["brent_snapshot_id"] or None,
                  _date(r["brent_observation_date"], "brent_observation_date", i) if r["brent_observation_date"] else None,
                  _decimal(r["brent_usd_per_barrel"]), _integer(r["brent_age_days"]), r["brent_usd_per_barrel_unit"],
                  r["fx_source_id"], r["fx_snapshot_id"] or None,
                  _date(r["fx_observation_date"], "fx_observation_date", i) if r["fx_observation_date"] else None,
                  _decimal(r["usd_vnd_rate"]), _integer(r["fx_age_days"]), r["usd_vnd_rate_unit"])
                 for i, r in enumerate(panel, 2)])
            source_for_file = {Path(s["raw_filename"]).name: s for s in sources.values()}
            quarantine_rows = []
            for i, r in enumerate(quarantine, 2):
                s = source_for_file[Path(r["source_file"]).name]
                raw_json = json.loads(r["raw_row_json"])
                quarantine_rows.append((run_id, int(r["source_row_number"]), s["source_id"],
                                        s["snapshot_id"], r["reason_code"], None,
                                        json.dumps(raw_json, ensure_ascii=False)))
            _insert_many(cur, """INSERT INTO quarantine_record
                (run_id,record_number,source_id,snapshot_id,reason_code,reason_detail,raw_row_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", quarantine_rows)
            cur.execute("INSERT INTO quality_report (run_id,report_json) VALUES (%s,%s)",
                        (run_id, json.dumps(quality, ensure_ascii=False)))
            for table, expected in (
                ("fuel_price_observation", len(fuel)), ("market_observation", len(market)),
                ("fuel_market_panel", len(panel)), ("quarantine_record", len(quarantine)),
                ("fund_annotation", len(bundle["annotations"])),
            ):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE run_id=%s", (run_id,))
                if int(cur.fetchone()["n"]) != expected:
                    raise LoadError(f"post-insert reconciliation failed for {table}")
            cur.execute("UPDATE dataset_run SET publication_status=%s WHERE run_id=%s", (status, run_id))
            cur.execute("SELECT c.run_id, r.created_at_utc FROM dataset_current_pointer c "
                        "JOIN dataset_run r ON r.run_id=c.run_id WHERE c.singleton_id=1 FOR UPDATE")
            current = cur.fetchone()
            if current and current["created_at_utc"] > bundle["published_at_utc"]:
                raise LoadError("accepted run is older than the current DB pointer; refusing to move backwards")
            cur.execute("""INSERT INTO dataset_current_pointer (singleton_id,run_id,updated_at_utc)
                VALUES (1,%s,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE
                run_id=%s,updated_at_utc=UTC_TIMESTAMP(6)""", (run_id, run_id))
        connection.commit()
        return (f"published run {run_id}: fuel={len(fuel)}, market={len(market)}, "
                f"panel={len(panel)}, fund_annotations={len(bundle['annotations'])}, "
                f"quarantine={len(quarantine)}; current pointer advanced")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="PetroTwin repository root")
    parser.add_argument("--load", action="store_true", help="publish the validated run to MySQL")
    args = parser.parse_args(argv)
    try:
        bundle = prepare_bundle(args.repo)
        counts = {name.removesuffix(".csv"): len(rows) for name, rows in bundle["outputs"].items()}
        print(f"Preflight OK — run {bundle['manifest']['run_id']} ({bundle['quality']['publication_status']})")
        print("Rows: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        print(f"Sources: {len(bundle['sources'])}; verified manual annotations: {len(bundle['annotations'])}")
        if args.load:
            print(publish(bundle))
        else:
            print("No database writes performed. Add --load to publish after local credentials are configured.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
