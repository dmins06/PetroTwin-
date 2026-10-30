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
    if status not in {"validated", "validated_with_quarantine"}:
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

    expected_counts = quality.get("outputs", {})
    for filename, rows in outputs.items():
        quality_entry = expected_counts.get(filename)
        if not quality_entry or int(quality_entry["rows"]) != len(rows):
            raise LoadError(f"quality report count mismatch for {filename}")

    fuel = outputs["fuel_price_observation.csv"]
    market = outputs["market_observation.csv"]
    panel = outputs["fuel_market_panel.csv"]
    quarantine = outputs["quarantine.csv"]
    keys = {
        "fuel": set(), "market": set(), "panel": set(), "quarantine": set(),
    }
    for index, row in enumerate(fuel, start=2):
        keys["fuel"].add(tuple(_nonempty(row.get(k), k, index) for k in
                               ("product_id", "effective_at_local", "snapshot_id")))
        _date(row.get("source_record_date"), "source_record_date", index)
        _datetime(row.get("effective_at_local"), "effective_at_local", index)
        _date_or_naive_utc(row.get("retrieved_at_utc"), index)
        for field in ("price_value", "price_change_value", "stabilization_fund_subsidy_amount"):
            _decimal(row.get(field))
        if row["snapshot_id"] not in {s["snapshot_id"] for s in source_by_id.values()}:
            raise LoadError(f"fuel row {index} references an unknown snapshot")
    for index, row in enumerate(market, start=2):
        keys["market"].add(tuple(_nonempty(row.get(k), k, index) for k in
                                 ("series_id", "observation_date", "snapshot_id")))
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
        keys["panel"].add(tuple(_nonempty(row.get(k), k, index) for k in
                                ("product_id", "effective_at_local", "target_snapshot_id")))
        event_dt = _datetime(row.get("effective_at_local"), "effective_at_local", index)
        _date(row.get("source_record_date"), "source_record_date", index)
        for field in ("price_value", "price_change_value", "stabilization_fund_subsidy_amount",
                      "brent_usd_per_barrel", "usd_vnd_rate"):
            _decimal(row.get(field))
        for prefix in ("brent", "fx"):
            obs_date = (row.get(f"{prefix}_observation_date") or "").strip()
            age = _integer(row.get(f"{prefix}_age_days"))
            value = (row.get("brent_usd_per_barrel") if prefix == "brent" else
                     row.get("usd_vnd_rate"))
            snapshot = (row.get(f"{prefix}_snapshot_id") or "").strip()
            if obs_date:
                parsed_date = _date(obs_date, f"{prefix}_observation_date", index)
                if parsed_date >= event_dt.date() or age != (event_dt.date() - parsed_date).days:
                    raise LoadError(f"panel row {index}: {prefix} feature is not strict-prior/age mismatch")
                if not (value or "").strip() or not snapshot:
                    raise LoadError(f"panel row {index}: incomplete {prefix} feature provenance")
            elif age is not None or (value or "").strip() or snapshot:
                raise LoadError(f"panel row {index}: incomplete missing {prefix} feature")
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
    annotation_path = root / "data/manual/stabilization_fund_annotations.csv"
    annotation_header, annotations = _csv_rows(annotation_path)
    if annotation_header != ["product_id", "source_record_date", "amount", "unit", "source_url"]:
        raise LoadError("manual fund annotation schema differs from pipeline contract")
    annotation_source = source_by_id.get("moit_manual_annotations")
    if annotation_source is None or len(annotations) != int(annotation_source["row_count"]):
        raise LoadError("fund annotation rows differ from manifest")
    annotation_keys: set[tuple[str, str]] = set()
    fuel_keys = {(r["product_id"], r["source_record_date"]) for r in fuel}
    for index, row in enumerate(annotations, start=2):
        key = (_nonempty(row.get("product_id"), "product_id", index),
               _nonempty(row.get("source_record_date"), "source_record_date", index))
        _date(key[1], "source_record_date", index)
        amount = _decimal(row.get("amount"))
        if amount is None or amount < 0 or (row.get("unit") or "").strip() not in {"VND/kg", "VND/liter"}:
            raise LoadError(f"annotation row {index}: invalid amount or unit")
        if not (row.get("source_url") or "").startswith("https://") or key not in fuel_keys:
            raise LoadError(f"annotation row {index}: invalid evidence URL or orphan key")
        annotation_keys.add(key)
    if len(annotation_keys) != len(annotations):
        raise LoadError("duplicate fund annotation keys detected")
    return {
        "root": root, "pointer": pointer, "run_dir": run_dir, "manifest": manifest,
        "quality": quality, "outputs": outputs, "sources": source_by_id,
        "annotations": annotations, "annotation_source": annotation_source,
        "published_at_utc": _date_or_naive_utc(pointer.get("published_at_utc"), 0),
    }


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


def publish(bundle: dict[str, Any]) -> str:
    """Publish a validated run atomically, without printing credential values."""
    try:
        import pymysql
    except ImportError as exc:
        raise LoadError("Install the optional connector with: python -m pip install -e '.[mysql]'") from exc
    config = {
        "host": os.getenv("PETROTWIN_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("PETROTWIN_DB_PORT", "3306")),
        "database": os.getenv("PETROTWIN_DB_NAME", "petrotwin"),
        "user": os.getenv("PETROTWIN_DB_USER", "root"),
        "password": os.getenv("PETROTWIN_DB_PASSWORD", ""),
        "charset": "utf8mb4", "autocommit": False,
        "cursorclass": pymysql.cursors.DictCursor,
    }
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
            cur.execute("SELECT publication_status FROM dataset_run WHERE run_id=%s FOR UPDATE", (run_id,))
            existing = cur.fetchone()
            if existing:
                if existing["publication_status"] != status:
                    raise LoadError("run_id exists with a different publication status; refusing repair")
                for table, expected in (
                    ("fuel_price_observation", len(fuel)), ("market_observation", len(market)),
                    ("fuel_market_panel", len(panel)), ("quarantine_record", len(quarantine)),
                    ("fund_annotation", len(bundle["annotations"])),
                ):
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE run_id=%s", (run_id,))
                    if int(cur.fetchone()["n"]) != expected:
                        raise LoadError(f"existing run is incomplete: {table} count mismatch; no auto-repair")
                connection.rollback()
                return f"run {run_id} already exists and its row counts reconcile; no rows changed"

            cur.execute("""INSERT INTO dataset_run
                (run_id,schema_version,pipeline_version,pipeline_sha256,publication_status,
                 started_at_utc,completed_at_utc,manifest_json,created_at_utc)
                VALUES (%s,%s,%s,%s,'building',%s,%s,%s,%s)""",
                (run_id, manifest["schema_version"], manifest["pipeline_version"],
                 manifest["pipeline_sha256"], bundle["published_at_utc"],
                 bundle["published_at_utc"], json.dumps(manifest, ensure_ascii=False),
                 bundle["published_at_utc"]))
            _insert_many(cur, """INSERT INTO source_snapshot
                (snapshot_id,source_id,raw_filename,source_url,publisher_or_attribution,
                 license_reuse_terms,sha256,schema_fingerprint_sha256,source_row_count,
                 retrieval_times_utc,retrieved_at_utc_min,retrieved_at_utc_max)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE snapshot_id=VALUES(snapshot_id)""",
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
                run_id=VALUES(run_id),updated_at_utc=VALUES(updated_at_utc)""", (run_id,))
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
