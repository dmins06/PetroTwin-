from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pymysql

from petrotwin.data.mysql_loader import (
    KNOWN_PRODUCTS,
    KNOWN_SERIES,
    LoadError,
    _mysql_connection_config,
    prepare_bundle,
    publish,
    validate_bundle,
)


@pytest.fixture
def fake_pymysql():
    return SimpleNamespace(cursors=SimpleNamespace(DictCursor="dict-cursor"))


def clear_db_env(monkeypatch):
    for key in (
        "PETROTWIN_DB_HOST",
        "PETROTWIN_DB_PORT",
        "PETROTWIN_DB_NAME",
        "PETROTWIN_DB_USER",
        "PETROTWIN_DB_PASSWORD",
        "PETROTWIN_DB_SSL",
        "PETROTWIN_DB_SSL_CA",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="module")
def base_bundle(tmp_path_factory):
    """Self-contained bundle built from a minimal synthetic repo.

    Does NOT read from live data/processed/current.json, so this fixture
    works on a fresh clone with no processed data directory.
    """
    tmp = tmp_path_factory.mktemp("repo_bundle")
    repo = _setup_disk_repo(tmp, include_fx=True)
    return prepare_bundle(repo)


@pytest.fixture
def valid_bundle(base_bundle):
    return copy.deepcopy(base_bundle)


class MockDB:
    def __init__(self):
        self.queries: list[tuple[str, Any]] = []
        self.executemany_queries: list[tuple[str, list[Any]]] = []
        self.fetchone_queue: list[Any] = []
        self.fetchall_queue: list[Any] = []
        self.begin_called = 0
        self.commit_called = 0
        self.rollback_called = 0
        self.close_called = 0
        self.raise_on_query_substr: str | None = None
        self.error_to_raise: Exception = RuntimeError("Simulated DB error")
        self.fail_once_on_query_substr: str | None = None
        self.pointer_run_id: str | None = None
        self.pending_pointer_run_id: str | None = None

    def handle_execute(self, sql: str, params: Any) -> None:
        if self.fail_once_on_query_substr and self.fail_once_on_query_substr in sql:
            self.fail_once_on_query_substr = None
            raise self.error_to_raise
        if self.raise_on_query_substr and self.raise_on_query_substr in sql:
            raise self.error_to_raise
        if "INSERT INTO dataset_current_pointer" in sql:
            self.pending_pointer_run_id = params[0]

    def commit(self) -> None:
        if self.pending_pointer_run_id is not None:
            self.pointer_run_id = self.pending_pointer_run_id
        self.pending_pointer_run_id = None

    def rollback(self) -> None:
        self.pending_pointer_run_id = None


class MockCursor:
    def __init__(self, db: MockDB):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self.db.queries.append((sql.strip(), params))
        self.db.handle_execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Any) -> int:
        self.db.executemany_queries.append((sql.strip(), list(seq_of_params)))
        self.db.handle_execute(sql, seq_of_params)
        return len(seq_of_params)

    def fetchone(self) -> Any:
        if self.db.fetchone_queue:
            return self.db.fetchone_queue.pop(0)
        return None

    def fetchall(self) -> list[Any]:
        if self.db.fetchall_queue:
            return self.db.fetchall_queue.pop(0)
        return []


class MockConnection:
    def __init__(self, db: MockDB):
        self.db = db

    def cursor(self):
        return MockCursor(self.db)

    def begin(self):
        self.db.begin_called += 1

    def commit(self):
        self.db.commit_called += 1
        self.db.commit()

    def rollback(self):
        self.db.rollback_called += 1
        self.db.rollback()

    def close(self):
        self.db.close_called += 1


def test_local_mysql_config_keeps_tls_disabled(fake_pymysql, monkeypatch):
    clear_db_env(monkeypatch)
    config = _mysql_connection_config(fake_pymysql)
    assert config["host"] == "127.0.0.1"
    assert config["port"] == 3306
    assert config["database"] == "petrotwin"
    assert config["cursorclass"] == "dict-cursor"
    assert not any(key.startswith("ssl_") for key in config)


def test_tidb_config_enables_verified_tls(fake_pymysql, monkeypatch):
    clear_db_env(monkeypatch)
    monkeypatch.setenv("PETROTWIN_DB_SSL", "true")
    config = _mysql_connection_config(fake_pymysql)
    assert config["ssl_ca"] == "/etc/ssl/certs/ca-certificates.crt"
    assert config["ssl_verify_cert"] is True
    assert config["ssl_verify_identity"] is True


def test_tidb_config_accepts_custom_ca(fake_pymysql, monkeypatch, tmp_path):
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA")
    monkeypatch.setenv("PETROTWIN_DB_SSL", "1")
    monkeypatch.setenv("PETROTWIN_DB_SSL_CA", str(ca_file))
    config = _mysql_connection_config(fake_pymysql)
    assert config["ssl_ca"] == str(ca_file)


def test_tidb_config_rejects_missing_ca(fake_pymysql, monkeypatch, tmp_path):
    monkeypatch.setenv("PETROTWIN_DB_SSL", "yes")
    monkeypatch.setenv("PETROTWIN_DB_SSL_CA", str(tmp_path / "missing.pem"))
    with pytest.raises(LoadError, match="CA bundle does not exist"):
        _mysql_connection_config(fake_pymysql)


# --- 1. Panel provenance matches manifest ---
def test_panel_provenance_matches_manifest(valid_bundle):
    # Should validate cleanly without raising
    validate_bundle(valid_bundle)


# --- 2. Panel provenance mismatch (target, brent, fx) ---
def test_panel_target_provenance_mismatch_raises_load_error(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    panel[0]["target_source_id"] = "algolab_sbv_central_rate"
    with pytest.raises(LoadError, match="target provenance mismatch"):
        validate_bundle(valid_bundle)


def test_panel_brent_provenance_mismatch_raises_load_error(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    row_with_brent = next(r for r in panel if r["brent_snapshot_id"])
    row_with_brent["brent_source_id"] = "pvoil_domestic_prices"
    with pytest.raises(LoadError, match="brent provenance mismatch"):
        validate_bundle(valid_bundle)


def test_panel_fx_provenance_mismatch_raises_load_error(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    row_with_fx = next(r for r in panel if r["fx_snapshot_id"])
    row_with_fx["fx_source_id"] = "eia_europe_brent_daily"
    with pytest.raises(LoadError, match="fx provenance mismatch"):
        validate_bundle(valid_bundle)


# --- 3. Panel NULL snapshots with NULL observation fields allowed ---
def test_panel_null_snapshots_with_null_observations_allowed(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    row = panel[0]
    row["brent_snapshot_id"] = ""
    row["brent_observation_date"] = ""
    row["brent_usd_per_barrel"] = ""
    row["brent_age_days"] = ""
    row["fx_snapshot_id"] = ""
    row["fx_observation_date"] = ""
    row["usd_vnd_rate"] = ""
    row["fx_age_days"] = ""
    validate_bundle(valid_bundle)


# --- 4. Panel NULL snapshot with present observation date/value/age ---
def test_panel_null_snapshot_with_present_obs_date_raises_load_error(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    row = panel[0]
    row["brent_snapshot_id"] = ""
    row["brent_observation_date"] = "2020-01-01"
    row["brent_usd_per_barrel"] = ""
    row["brent_age_days"] = ""
    with pytest.raises(LoadError, match="brent_snapshot_id is missing but brent_observation_date is present"):
        validate_bundle(valid_bundle)


def test_panel_null_snapshot_with_present_value_raises_load_error(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    row = panel[0]
    row["fx_snapshot_id"] = ""
    row["fx_observation_date"] = ""
    row["usd_vnd_rate"] = "23000"
    row["fx_age_days"] = ""
    with pytest.raises(LoadError, match="fx_snapshot_id is missing but usd_vnd_rate is present"):
        validate_bundle(valid_bundle)


def test_panel_null_snapshot_with_present_age_raises_load_error(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    row = panel[0]
    row["brent_snapshot_id"] = ""
    row["brent_observation_date"] = ""
    row["brent_usd_per_barrel"] = ""
    row["brent_age_days"] = "1"
    with pytest.raises(LoadError, match="brent_snapshot_id is missing but brent_age_days is present"):
        validate_bundle(valid_bundle)


# --- 5. Snapshot exists in panel but not in manifest ---
def test_panel_snapshot_not_in_manifest_raises_load_error(valid_bundle):
    panel = valid_bundle["outputs"]["fuel_market_panel.csv"]
    panel[0]["target_snapshot_id"] = "a" * 64
    with pytest.raises(LoadError, match="target_snapshot_id '.*' is not in manifest"):
        validate_bundle(valid_bundle)


# --- 6. Annotation with invalid product_id ---
def test_annotation_invalid_product_id_raises_load_error(valid_bundle):
    annotations = valid_bundle["annotations"]
    annotations[0]["product_id"] = "unrecognized_petro_blend"
    with pytest.raises(LoadError, match="invalid product_id '.*' not in reference seeds"):
        validate_bundle(valid_bundle)


# --- 7. Annotation with duplicate key grain (product_id, source_record_date) ---
def test_annotation_duplicate_grain_raises_load_error(valid_bundle):
    annotations = valid_bundle["annotations"]
    dup = dict(annotations[0])
    annotations.append(dup)
    valid_bundle["annotation_source"]["row_count"] = len(annotations)
    with pytest.raises(LoadError, match="duplicate key grain"):
        validate_bundle(valid_bundle)


# --- 8. Retry publish same run and same data reconciles without changes ---
def test_publish_idempotent_reconciled_existing_run(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    manifest = valid_bundle["manifest"]
    quality = valid_bundle["quality"]
    run_id = manifest["run_id"]
    outputs = valid_bundle["outputs"]

    db.fetchone_queue.append({
        "run_id": run_id,
        "schema_version": manifest["schema_version"],
        "pipeline_version": manifest["pipeline_version"],
        "pipeline_sha256": manifest["pipeline_sha256"],
        "publication_status": quality["publication_status"],
        "manifest_json": manifest,
    })
    db.fetchone_queue.append({"report_json": quality})
    db.fetchall_queue.append([{"snapshot_id": s["snapshot_id"]} for s in valid_bundle["sources"].values()])
    for count in (
        len(outputs["fuel_price_observation.csv"]),
        len(outputs["market_observation.csv"]),
        len(outputs["fuel_market_panel.csv"]),
        len(outputs["quarantine.csv"]),
        len(valid_bundle["annotations"]),
    ):
        db.fetchone_queue.append({"n": count})

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    msg = publish(valid_bundle)
    assert f"run {run_id} already exists and its data/metadata reconcile; no rows changed" in msg
    assert db.rollback_called == 1
    assert db.commit_called == 0


# --- 9. Retry same run but manifest / counts differ raises LoadError ---
def test_publish_mismatched_status_raises_load_error(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    manifest = valid_bundle["manifest"]
    run_id = manifest["run_id"]

    db.fetchone_queue.append({
        "run_id": run_id,
        "schema_version": manifest["schema_version"],
        "pipeline_version": manifest["pipeline_version"],
        "pipeline_sha256": manifest["pipeline_sha256"],
        "publication_status": "failed",
        "manifest_json": manifest,
    })
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    with pytest.raises(LoadError, match="run_id exists with a different publication status; refusing repair"):
        publish(valid_bundle)
    assert db.rollback_called == 1


def test_publish_mismatched_counts_raises_load_error(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    manifest = valid_bundle["manifest"]
    quality = valid_bundle["quality"]
    run_id = manifest["run_id"]

    db.fetchone_queue.append({
        "run_id": run_id,
        "schema_version": manifest["schema_version"],
        "pipeline_version": manifest["pipeline_version"],
        "pipeline_sha256": manifest["pipeline_sha256"],
        "publication_status": quality["publication_status"],
        "manifest_json": manifest,
    })
    db.fetchone_queue.append({"report_json": quality})
    db.fetchall_queue.append([{"snapshot_id": s["snapshot_id"]} for s in valid_bundle["sources"].values()])
    db.fetchone_queue.append({"n": 999999})  # Mismatched fuel count

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    with pytest.raises(LoadError, match="count mismatch; no auto-repair"):
        publish(valid_bundle)
    assert db.rollback_called == 1


# --- 10. Transaction rollback when error occurs mid-publish ---
def test_publish_transaction_rollback_on_failure(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    # New run
    db.fetchone_queue.append(None)
    db.fetchall_queue.append([{"product_id": pid} for pid in KNOWN_PRODUCTS])
    db.fetchall_queue.append([{"series_id": sid} for sid in KNOWN_SERIES])
    db.raise_on_query_substr = "INSERT INTO market_observation"
    db.error_to_raise = RuntimeError("Disk full / connection drop")

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    with pytest.raises(RuntimeError, match="Disk full / connection drop"):
        publish(valid_bundle)
    assert db.rollback_called == 1
    assert db.commit_called == 0
    assert db.close_called == 1


# --- 11. Disallow current pointer moving backwards to older run ---
def test_publish_regressing_current_pointer_raises_load_error(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    outputs = valid_bundle["outputs"]
    # New run
    db.fetchone_queue.append(None)
    db.fetchall_queue.append([{"product_id": pid} for pid in KNOWN_PRODUCTS])
    db.fetchall_queue.append([{"series_id": sid} for sid in KNOWN_SERIES])
    for count in (
        len(outputs["fuel_price_observation.csv"]),
        len(outputs["market_observation.csv"]),
        len(outputs["fuel_market_panel.csv"]),
        len(outputs["quarantine.csv"]),
        len(valid_bundle["annotations"]),
    ):
        db.fetchone_queue.append({"n": count})
    # Pointer query returns a run in the future
    db.fetchone_queue.append({
        "run_id": "future_run_2099",
        "created_at_utc": datetime(2099, 1, 1, 0, 0, 0),
    })

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    with pytest.raises(LoadError, match="accepted run is older than the current DB pointer; refusing to move backwards"):
        publish(valid_bundle)
    assert db.rollback_called == 1
    assert db.commit_called == 0


# --- 12. Concurrent publish with same run ID raises LoadError ---
def test_publish_concurrent_conflict_raises_load_error(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    manifest = valid_bundle["manifest"]
    run_id = manifest["run_id"]
    # Check returns None because neither process finished inserting
    db.fetchone_queue.append(None)
    db.raise_on_query_substr = "INSERT INTO dataset_run"
    db.error_to_raise = pymysql.IntegrityError(1062, f"Duplicate entry '{run_id}' for key 'PRIMARY'")

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    with pytest.raises(LoadError, match=f"concurrent publication conflict: run_id {run_id} is already being published"):
        publish(valid_bundle)
    assert db.rollback_called == 1
    assert db.commit_called == 0


# --- 13. Optional FX and Required Sources Manifest Compatibility ---
def _file_sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup_disk_repo(tmp_path: Path, include_fx: bool = True, include_brent: bool = True, include_fuel: bool = True) -> Path:
    import csv
    import hashlib
    import json

    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manual_dir = tmp_path / "data" / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = tmp_path / "data" / "processed"
    run_dir = processed_dir / "runs" / "test_run_1"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Manual annotations
    annot_path = manual_dir / "stabilization_fund_annotations.csv"
    annot_header = ["product_id", "source_record_date", "amount", "unit", "source_url"]
    annot_rows = [["gasoline_ron95_iii", "2026-11-05", "0", "VND/liter", "https://moit.gov.vn/notice1"]]
    with annot_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(annot_header)
        w.writerows(annot_rows)
    annot_hash = _file_sha256(annot_path)
    annot_fp = hashlib.sha256(",".join(annot_header).encode("utf-8")).hexdigest()

    # 2. Brent raw
    brent_path = raw_dir / "brent_crude_prices.csv"
    brent_header = [
        "date", "product_name", "price_usd_per_barrel", "unit", "source_url",
        "series_id", "publisher", "source_value", "observation_status", "extracted_at_utc"
    ]
    brent_rows = [
        ["2026-11-01", "Crude Oil Prices: Brent - Europe", "100.5", "Dollars per Barrel",
         "https://fred.stlouisfed.org", "DCOILBRENTEU", "EIA", "100.5", "observed", "2026-11-01T00:00:00Z"]
    ]
    with brent_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(brent_header)
        w.writerows(brent_rows)
    brent_hash = _file_sha256(brent_path)
    brent_fp = hashlib.sha256("\x1f".join(brent_header).encode("utf-8")).hexdigest()

    # 3. Fuel raw
    fuel_path = raw_dir / "data_price_petro.csv"
    fuel_header = [
        "date", "effective_date", "effective_time", "product_name", "price_vnd_per_liter",
        "price_display", "price_change_vnd", "price_change_display", "unit",
        "includes_vat_and_environmental_tax", "source_url", "extracted_at_utc"
    ]
    fuel_rows = [
        ["2026-11-05", "2026-11-05", "15:00", "Xăng RON 95-III", "21000", "21.000 đ", "0",
         "", "Đồng/lít thực tế", "true", "https://comeco.vn/", "2026-11-05T08:00:00Z"]
    ]
    with fuel_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fuel_header)
        w.writerows(fuel_rows)
    fuel_hash = _file_sha256(fuel_path)
    fuel_fp = hashlib.sha256("\x1f".join(fuel_header).encode("utf-8")).hexdigest()

    # 4. Optional FX raw
    if include_fx:
        fx_path = raw_dir / "usd_vnd_rates.csv"
        fx_header = [
            "date", "base_currency", "quote_currency", "rate_vnd_per_usd", "unit",
            "rate_type", "source_url", "source_attribution", "source_value", "extracted_at_utc"
        ]
        fx_rows = [
            ["2026-11-01", "USD", "VND", "25627", "VND per 1 USD", "SBV central reference rate",
             "https://algolab.vn", "Algolab", "25,627", "2026-11-01T00:00:00Z"]
        ]
        with fx_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(fx_header)
            w.writerows(fx_rows)
        fx_hash = _file_sha256(fx_path)
        fx_fp = hashlib.sha256("\x1f".join(fx_header).encode("utf-8")).hexdigest()

    # 5. Output CSVs
    fuel_out = run_dir / "fuel_price_observation.csv"
    with fuel_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "product_id", "source_product_name", "source_record_date", "effective_at_local",
            "effective_time_24_00_normalized", "price_value", "unit_code", "price_change_value",
            "stabilization_fund_subsidy", "tax_included", "stabilization_fund_subsidy_amount",
            "stabilization_fund_subsidy_unit", "stabilization_fund_source_url", "source_id",
            "snapshot_id", "source_url", "retrieved_at_utc", "schema_version"
        ])
        w.writerow([
            "gasoline_ron95_iii", "Xăng RON 95-III", "2026-11-05", "2026-11-05T15:00:00+07:00",
            "false", "21000", "VND/liter", "0", "0", "true", "0", "VND/liter",
            "https://moit.gov.vn/notice1", "comeco", fuel_hash, "https://comeco.vn/",
            "2026-11-05T08:00:00Z", "petrotwin-data-v3"
        ])

    market_out = run_dir / "market_observation.csv"
    with market_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "series_id", "observation_date", "value", "unit_code", "observation_status",
            "source_id", "snapshot_id", "source_value", "source_url", "retrieved_at_utc",
            "schema_version"
        ])
        if include_brent:
            w.writerow([
                "brent_dated_usd_bbl", "2026-11-01", "100.5", "USD/bbl", "observed",
                "eia_fred", brent_hash, "100.5", "https://fred.stlouisfed.org",
                "2026-11-01T00:00:00Z", "petrotwin-data-v3"
            ])
        if include_fx:
            w.writerow([
                "usd_vnd_sbv_reference", "2026-11-01", "25627", "VND/USD", "observed",
                "algolab_sbv_central_rate", fx_hash, "25,627", "https://algolab.vn",
                "2026-11-01T00:00:00Z", "petrotwin-data-v3"
            ])

    panel_out = run_dir / "fuel_market_panel.csv"
    with panel_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "product_id", "source_product_name", "source_record_date", "effective_at_local",
            "price_value", "price_change_value", "stabilization_fund_subsidy",
            "stabilization_fund_subsidy_amount", "stabilization_fund_subsidy_unit",
            "stabilization_fund_source_url", "unit_code", "target_source_id", "target_snapshot_id",
            "brent_source_id", "brent_snapshot_id", "brent_observation_date", "brent_usd_per_barrel",
            "brent_age_days", "brent_usd_per_barrel_unit", "fx_source_id", "fx_snapshot_id",
            "fx_observation_date", "usd_vnd_rate", "fx_age_days", "usd_vnd_rate_unit",
            "schema_version"
        ])
        w.writerow([
            "gasoline_ron95_iii", "Xăng RON 95-III", "2026-11-05", "2026-11-05T15:00:00+07:00",
            "21000", "0", "0", "0", "VND/liter", "https://moit.gov.vn/notice1", "VND/liter",
            "comeco", fuel_hash, "eia_fred", brent_hash, "2026-11-01", "100.5", "4", "USD/bbl",
            "algolab_sbv_central_rate", (fx_hash if include_fx else ""),
            ("2026-11-01" if include_fx else ""), ("25627" if include_fx else ""),
            ("4" if include_fx else ""), "VND/USD", "petrotwin-data-v3"
        ])

    quar_out = run_dir / "quarantine.csv"
    with quar_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_file", "source_row_number", "reason_code", "raw_row_json"])

    outputs_map = {
        "fuel_price_observation.csv": {"rows": 1, "sha256": _file_sha256(fuel_out)},
        "market_observation.csv": {"rows": (2 if include_fx and include_brent else 1 if (include_fx or include_brent) else 0), "sha256": _file_sha256(market_out)},
        "fuel_market_panel.csv": {"rows": 1, "sha256": _file_sha256(panel_out)},
        "quarantine.csv": {"rows": 0, "sha256": _file_sha256(quar_out)},
    }

    sources_list = []
    if include_brent:
        sources_list.append({
            "snapshot_id": brent_hash, "source_id": "eia_fred", "source_url": "https://fred.stlouisfed.org",
            "publisher_or_attribution": "EIA", "license_reuse_terms": "terms",
            "retrieved_at_utc": "2026-11-01T00:00:00Z", "raw_filename": "brent_crude_prices.csv",
            "sha256": brent_hash, "row_count": 1, "schema_fingerprint_sha256": brent_fp,
        })
    if include_fuel:
        sources_list.append({
            "snapshot_id": fuel_hash, "source_id": "comeco", "source_url": "https://comeco.vn/",
            "publisher_or_attribution": "COMECO", "license_reuse_terms": "terms",
            "retrieved_at_utc": "2026-11-05T08:00:00Z", "raw_filename": "data_price_petro.csv",
            "sha256": fuel_hash, "row_count": 1, "schema_fingerprint_sha256": fuel_fp,
        })
    if include_fx:
        sources_list.append({
            "snapshot_id": fx_hash, "source_id": "algolab_sbv_central_rate", "source_url": "https://algolab.vn",
            "publisher_or_attribution": "Algolab", "license_reuse_terms": "terms",
            "retrieved_at_utc": "2026-11-01T00:00:00Z", "raw_filename": "usd_vnd_rates.csv",
            "sha256": fx_hash, "row_count": 1, "schema_fingerprint_sha256": fx_fp,
        })
    sources_list.append({
        "snapshot_id": annot_hash, "source_id": "moit_manual_annotations", "source_url": "https://moit.gov.vn",
        "publisher_or_attribution": "MOIT", "license_reuse_terms": "terms",
        "retrieved_at_utc": "2026-11-06T12:00:00Z", "raw_filename": "manual/stabilization_fund_annotations.csv",
        "sha256": annot_hash, "row_count": 1, "schema_fingerprint_sha256": annot_fp,
    })

    manifest_payload = {
        "run_id": "test_run_1",
        "schema_version": "petrotwin-data-v3",
        "pipeline_version": "1.3.0",
        "pipeline_sha256": "0" * 64,
        "sources": sources_list,
        "outputs": outputs_map,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

    quality_payload = {
        "run_id": "test_run_1",
        "schema_version": "petrotwin-data-v3",
        "publication_status": ("validated" if include_fx else "validated_with_optional_source_missing"),
        "outputs": outputs_map,
        "integrated": {"quarantined_rows": 0},
    }
    (run_dir / "quality_report.json").write_text(json.dumps(quality_payload, indent=2), encoding="utf-8")

    pointer_payload = {
        "run_id": "test_run_1",
        "path": "runs/test_run_1",
        "schema_version": "petrotwin-data-v3",
        "published_at_utc": "2026-11-06T12:00:00Z",
    }
    (processed_dir / "current.json").write_text(json.dumps(pointer_payload, indent=2), encoding="utf-8")

    return tmp_path


def test_prepare_bundle_accepts_valid_optional_fx_missing(tmp_path):
    repo = _setup_disk_repo(tmp_path, include_fx=False)
    bundle = prepare_bundle(repo)
    assert bundle["manifest"]["run_id"] == "test_run_1"
    assert "algolab_sbv_central_rate" not in bundle["sources"]
    assert "eia_fred" in bundle["sources"]
    assert "comeco" in bundle["sources"]
    assert bundle["quality"]["publication_status"] == "validated_with_optional_source_missing"


def test_prepare_bundle_rejects_missing_required_brent_provenance(tmp_path):
    repo = _setup_disk_repo(tmp_path, include_fx=False, include_brent=False)
    with pytest.raises(LoadError, match="missing required Brent source 'eia_fred' in manifest"):
        prepare_bundle(repo)


def test_prepare_bundle_rejects_missing_required_fuel_provenance(tmp_path):
    repo = _setup_disk_repo(tmp_path, include_fx=False, include_fuel=False)
    with pytest.raises(LoadError, match="missing required domestic fuel source"):
        prepare_bundle(repo)


def test_existing_valid_fx_bundle_remains_accepted(valid_bundle):
    # Validate the synthetic fixture directly; no repository working-directory
    # or production current.json dependency is needed.
    validate_bundle(valid_bundle)
    assert valid_bundle["manifest"]["run_id"] == "test_run_1"
    assert set(valid_bundle["sources"]).__contains__("algolab_sbv_central_rate")
    assert "eia_fred" in valid_bundle["sources"]
    assert "comeco" in valid_bundle["sources"]


def queue_new_run(db: MockDB, bundle: dict[str, Any], current: dict[str, Any] | None = None) -> None:
    outputs = bundle["outputs"]
    db.fetchone_queue.extend([
        None,
        {"n": len(outputs["fuel_price_observation.csv"])},
        {"n": len(outputs["market_observation.csv"])},
        {"n": len(outputs["fuel_market_panel.csv"])},
        {"n": len(outputs["quarantine.csv"])},
        {"n": len(bundle["annotations"])},
        current,
    ])
    db.fetchall_queue.extend([
        [{"product_id": pid} for pid in KNOWN_PRODUCTS],
        [{"series_id": sid} for sid in KNOWN_SERIES],
    ])


def test_publish_retry_after_rollback_succeeds(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    queue_new_run(db, valid_bundle)
    db.fail_once_on_query_substr = "INSERT INTO market_observation"
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    with pytest.raises(RuntimeError, match="Simulated DB error"):
        publish(valid_bundle)

    db.fetchone_queue.clear()
    db.fetchall_queue.clear()
    queue_new_run(db, valid_bundle)
    message = publish(valid_bundle)

    assert "published run" in message
    assert db.rollback_called == 1
    assert db.commit_called == 1
    assert db.pointer_run_id == valid_bundle["manifest"]["run_id"]


def test_current_pointer_unchanged_when_publish_fails(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    db.pointer_run_id = "existing_current_run"
    queue_new_run(db, valid_bundle)
    db.raise_on_query_substr = "INSERT INTO dataset_current_pointer"
    db.error_to_raise = RuntimeError("pointer update failed")
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    with pytest.raises(RuntimeError, match="pointer update failed"):
        publish(valid_bundle)

    assert db.rollback_called == 1
    assert db.commit_called == 0
    assert db.pointer_run_id == "existing_current_run"


def test_historical_pvoil_bundle_passes_validation(valid_bundle):
    """Historical runs with PVOIL source_id must remain valid without alteration."""
    pvoil_bundle = copy.deepcopy(valid_bundle)
    # Convert fuel source to historical PVOIL
    comeco_src = pvoil_bundle["sources"].pop("comeco")
    comeco_src["source_id"] = "pvoil"
    comeco_src["publisher_or_attribution"] = "PVOIL"
    comeco_src["source_url"] = "https://www.pvoil.com.vn/"
    pvoil_bundle["sources"]["pvoil"] = comeco_src
    pvoil_bundle["manifest"]["sources"] = list(pvoil_bundle["sources"].values())

    # Both fuel observation and panel target_source_id match PVOIL
    for row in pvoil_bundle["outputs"]["fuel_price_observation.csv"]:
        row["source_id"] = "pvoil"
        row["source_url"] = "https://www.pvoil.com.vn/"
    for row in pvoil_bundle["outputs"]["fuel_market_panel.csv"]:
        row["target_source_id"] = "pvoil"

    validate_bundle(pvoil_bundle)
    assert "pvoil" in pvoil_bundle["sources"]


def test_pvoil_observation_linked_to_comeco_snapshot_is_rejected(valid_bundle):
    """A PVOIL observation linked to a COMECO snapshot must be rejected."""
    # Case A: in fuel_market_panel.csv
    bad_panel_bundle = copy.deepcopy(valid_bundle)
    assert "comeco" in bad_panel_bundle["sources"]
    for row in bad_panel_bundle["outputs"]["fuel_market_panel.csv"]:
        row["target_source_id"] = "pvoil"  # Snapshot belongs to 'comeco'

    with pytest.raises(
        LoadError,
        match="target provenance mismatch: target_source_id 'pvoil' does not match snapshot source 'comeco'",
    ):
        validate_bundle(bad_panel_bundle)

    # Case B: in fuel_price_observation.csv
    bad_fuel_bundle = copy.deepcopy(valid_bundle)
    for row in bad_fuel_bundle["outputs"]["fuel_price_observation.csv"]:
        row["source_id"] = "pvoil"  # Snapshot belongs to 'comeco'

    with pytest.raises(
        LoadError,
        match="source provenance mismatch: source_id 'pvoil' does not match snapshot source 'comeco'",
    ):
        validate_bundle(bad_fuel_bundle)


def test_comeco_observation_linked_to_pvoil_snapshot_is_rejected(valid_bundle):
    """A COMECO observation linked to a PVOIL snapshot must be rejected."""
    base_pvoil = copy.deepcopy(valid_bundle)
    comeco_src = base_pvoil["sources"].pop("comeco")
    comeco_src["source_id"] = "pvoil"
    comeco_src["publisher_or_attribution"] = "PVOIL"
    comeco_src["source_url"] = "https://www.pvoil.com.vn/"
    base_pvoil["sources"]["pvoil"] = comeco_src
    base_pvoil["manifest"]["sources"] = list(base_pvoil["sources"].values())
    for row in base_pvoil["outputs"]["fuel_price_observation.csv"]:
        row["source_id"] = "pvoil"
    for row in base_pvoil["outputs"]["fuel_market_panel.csv"]:
        row["target_source_id"] = "pvoil"

    # Case A: COMECO observation in panel linked to PVOIL snapshot
    bad_panel = copy.deepcopy(base_pvoil)
    bad_panel["outputs"]["fuel_market_panel.csv"][0]["target_source_id"] = "comeco"
    with pytest.raises(
        LoadError,
        match="target provenance mismatch: target_source_id 'comeco' does not match snapshot source 'pvoil'",
    ):
        validate_bundle(bad_panel)

    # Case B: COMECO observation in fuel_price_observation linked to PVOIL snapshot
    bad_fuel = copy.deepcopy(base_pvoil)
    bad_fuel["outputs"]["fuel_price_observation.csv"][0]["source_id"] = "comeco"
    with pytest.raises(
        LoadError,
        match="source provenance mismatch: source_id 'comeco' does not match snapshot source 'pvoil'",
    ):
        validate_bundle(bad_fuel)


def test_mixed_source_bundle_with_source_specific_snapshots_passes_validation(valid_bundle):
    """A mixed-source run with distinct PVOIL and COMECO snapshots in manifest passes validation when each observation links to its matching snapshot."""
    mixed_bundle = copy.deepcopy(valid_bundle)
    comeco_snap = mixed_bundle["sources"]["comeco"]["snapshot_id"]
    pvoil_snap = "b" * 64

    # Add PVOIL source snapshot to manifest & sources
    pvoil_src = copy.deepcopy(mixed_bundle["sources"]["comeco"])
    pvoil_src["source_id"] = "pvoil"
    pvoil_src["snapshot_id"] = pvoil_snap
    pvoil_src["sha256"] = pvoil_snap
    pvoil_src["raw_filename"] = "data_price_petro.csv"
    pvoil_src["source_url"] = "https://pvoil.com.vn/tin-gia-xang-dau"
    pvoil_src["publisher_or_attribution"] = "PVOIL"
    mixed_bundle["sources"]["pvoil"] = pvoil_src
    mixed_bundle["manifest"]["sources"].append(pvoil_src)

    # Keep COMECO row pointing to COMECO snapshot, add PVOIL row pointing to PVOIL snapshot
    pvoil_fuel_row = copy.deepcopy(mixed_bundle["outputs"]["fuel_price_observation.csv"][0])
    pvoil_fuel_row["effective_at_local"] = "2026-10-22T15:00:00+07:00"
    pvoil_fuel_row["source_record_date"] = "2026-10-22"
    pvoil_fuel_row["source_id"] = "pvoil"
    pvoil_fuel_row["snapshot_id"] = pvoil_snap
    pvoil_fuel_row["source_url"] = "https://pvoil.com.vn/tin-gia-xang-dau"
    mixed_bundle["outputs"]["fuel_price_observation.csv"].append(pvoil_fuel_row)

    pvoil_panel_row = copy.deepcopy(mixed_bundle["outputs"]["fuel_market_panel.csv"][0])
    pvoil_panel_row["effective_at_local"] = "2026-10-22T15:00:00+07:00"
    pvoil_panel_row["source_record_date"] = "2026-10-22"
    pvoil_panel_row["target_source_id"] = "pvoil"
    pvoil_panel_row["target_snapshot_id"] = pvoil_snap
    # Clear market features since 2026-10-22 is before 2026-11-01 brent/fx in fixture
    pvoil_panel_row["brent_snapshot_id"] = ""
    pvoil_panel_row["brent_observation_date"] = ""
    pvoil_panel_row["brent_usd_per_barrel"] = ""
    pvoil_panel_row["brent_age_days"] = ""
    pvoil_panel_row["fx_snapshot_id"] = ""
    pvoil_panel_row["fx_observation_date"] = ""
    pvoil_panel_row["usd_vnd_rate"] = ""
    pvoil_panel_row["fx_age_days"] = ""
    mixed_bundle["outputs"]["fuel_market_panel.csv"].append(pvoil_panel_row)

    mixed_bundle["quality"]["outputs"]["fuel_price_observation.csv"]["rows"] = 2
    mixed_bundle["quality"]["outputs"]["fuel_market_panel.csv"]["rows"] = 2

    validate_bundle(mixed_bundle)
    assert mixed_bundle["outputs"]["fuel_market_panel.csv"][0]["target_snapshot_id"] == comeco_snap
    assert mixed_bundle["outputs"]["fuel_market_panel.csv"][1]["target_snapshot_id"] == pvoil_snap


def test_mysql_loader_accepts_and_publishes_multiple_source_observations_for_single_event(valid_bundle, monkeypatch):
    """When PVOIL and COMECO both report identical observations for the same (product_id, effective_at_local),
    fuel_price_observation.csv has 2 rows (with distinct snapshots) while fuel_market_panel.csv has 1 row.
    Both validate_bundle() and publish() must accept and persist this relationship."""
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    bundle = copy.deepcopy(valid_bundle)
    comeco_snap = bundle["sources"]["comeco"]["snapshot_id"]
    pvoil_snap = "c" * 64

    # Add PVOIL source snapshot to manifest & sources
    pvoil_src = copy.deepcopy(bundle["sources"]["comeco"])
    pvoil_src["source_id"] = "pvoil"
    pvoil_src["snapshot_id"] = pvoil_snap
    pvoil_src["sha256"] = pvoil_snap
    pvoil_src["raw_filename"] = "data_price_petro.csv"
    pvoil_src["source_url"] = "https://pvoil.com.vn/tin-gia-xang-dau"
    pvoil_src["publisher_or_attribution"] = "PVOIL"
    bundle["sources"]["pvoil"] = pvoil_src
    bundle["manifest"]["sources"].append(pvoil_src)

    # Add a second observation for the EXACT SAME (product_id, effective_at_local) from PVOIL
    pvoil_fuel_row = copy.deepcopy(bundle["outputs"]["fuel_price_observation.csv"][0])
    pvoil_fuel_row["source_id"] = "pvoil"
    pvoil_fuel_row["snapshot_id"] = pvoil_snap
    pvoil_fuel_row["source_url"] = "https://pvoil.com.vn/tin-gia-xang-dau"
    bundle["outputs"]["fuel_price_observation.csv"].append(pvoil_fuel_row)

    # fuel_price_observation.csv now has 2 rows, while fuel_market_panel.csv has 1 row!
    assert len(bundle["outputs"]["fuel_price_observation.csv"]) == 2
    assert len(bundle["outputs"]["fuel_market_panel.csv"]) == 1
    bundle["quality"]["outputs"]["fuel_price_observation.csv"]["rows"] = 2
    bundle["manifest"]["outputs"]["fuel_price_observation.csv"]["rows"] = 2

    # 1. Offline validation passes
    validate_bundle(bundle)

    # 2. Transactional publish inserts 2 fuel_price_observation rows and 1 fuel_market_panel row
    db = MockDB()
    queue_new_run(db, bundle)
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    msg = publish(bundle)
    assert "fuel=2" in msg
    assert "panel=1" in msg
    assert db.commit_called == 1


def test_publish_idempotent_when_already_published(valid_bundle, monkeypatch):
    """When a run is already published and reconciling, publishing again is idempotent."""
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    manifest = valid_bundle["manifest"]
    quality = valid_bundle["quality"]
    # Mock existing dataset_run
    db.fetchone_queue.append({
        "run_id": manifest["run_id"],
        "schema_version": manifest["schema_version"],
        "pipeline_version": manifest["pipeline_version"],
        "pipeline_sha256": manifest["pipeline_sha256"],
        "publication_status": quality["publication_status"],
        "manifest_json": json.dumps(manifest),
    })
    # Mock existing quality_report
    db.fetchone_queue.append({
        "report_json": json.dumps(quality),
    })
    # Mock existing snapshots
    db.fetchall_queue.append([
        {"snapshot_id": s["snapshot_id"]} for s in valid_bundle["sources"].values()
    ])
    # Mock table count reconciliations
    outputs = valid_bundle["outputs"]
    for table, count in [
        ("fuel_price_observation", len(outputs["fuel_price_observation.csv"])),
        ("market_observation", len(outputs["market_observation.csv"])),
        ("fuel_market_panel", len(outputs["fuel_market_panel.csv"])),
        ("quarantine_record", len(outputs["quarantine.csv"])),
        ("fund_annotation", len(valid_bundle["annotations"])),
    ]:
        db.fetchone_queue.append({"n": count})

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    message = publish(valid_bundle)
    assert "already exists and its data/metadata reconcile; no rows changed" in message
    assert db.rollback_called == 1


def test_mysql_connection_config_sets_explicit_timeouts(fake_pymysql, monkeypatch):
    clear_db_env(monkeypatch)
    monkeypatch.setenv("PETROTWIN_DB_CONNECT_TIMEOUT", "12")
    monkeypatch.setenv("PETROTWIN_DB_READ_TIMEOUT", "45")
    monkeypatch.setenv("PETROTWIN_DB_WRITE_TIMEOUT", "50")
    config = _mysql_connection_config(fake_pymysql)
    assert config["connect_timeout"] == 12
    assert config["read_timeout"] == 45
    assert config["write_timeout"] == 50


def test_publish_emits_all_timing_logs_without_credentials_and_matches_pymysql_batching(
    valid_bundle, monkeypatch, capsys
):
    """Verify all 17 required timing log stages are emitted with run_id/elapsed_seconds/rows,
    no credentials are logged, and every executemany SQL matches PyMySQL RE_INSERT_VALUES."""
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "super_secret_password_123")
    db = MockDB()
    # Queue empty existing run, reference seeds, and post-insert counts
    db.fetchone_queue.append(None)
    db.fetchall_queue.append([{"product_id": p} for p in KNOWN_PRODUCTS])
    db.fetchall_queue.append([{"series_id": s} for s in KNOWN_SERIES])
    outputs = valid_bundle["outputs"]
    for count in [
        len(outputs["fuel_price_observation.csv"]),
        len(outputs["market_observation.csv"]),
        len(outputs["fuel_market_panel.csv"]),
        len(outputs["quarantine.csv"]),
        len(valid_bundle["annotations"]),
    ]:
        db.fetchone_queue.append({"n": count})
    db.fetchone_queue.append(None)  # dataset_current_pointer

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    publish(valid_bundle)
    captured = capsys.readouterr().out
    assert "super_secret_password_123" not in captured

    events = [json.loads(line) for line in captured.splitlines() if line.strip()]
    stages = [ev["stage"] for ev in events]
    expected_stages = [
        "mysql_loader.connect.start",
        "mysql_loader.connect.completed",
        "mysql_loader.transaction.started",
        "mysql_loader.dataset_run.insert.completed",
        "mysql_loader.source_snapshot.insert.completed",
        "mysql_loader.reference_validation.completed",
        "mysql_loader.market_observation.insert.completed",
        "mysql_loader.fuel_price_observation.insert.completed",
        "mysql_loader.fund_annotation.insert.completed",
        "mysql_loader.fuel_market_panel.insert.completed",
        "mysql_loader.quarantine_record.insert.completed",
        "mysql_loader.quality_report.insert.completed",
        "mysql_loader.reconciliation.completed",
        "mysql_loader.pointer_update.completed",
        "mysql_loader.commit.started",
        "mysql_loader.commit.completed",
        "mysql_loader.completed",
    ]
    assert stages == expected_stages
    for ev in events:
        assert ev["run_id"] == valid_bundle["manifest"]["run_id"]
        assert isinstance(ev["elapsed_seconds"], float)
        assert ev["ts"].endswith("Z")

    # Every executemany INSERT must match PyMySQL's RE_INSERT_VALUES so it batches instead of 1-by-1
    for sql, _params in db.executemany_queries:
        if sql.lstrip().upper().startswith("INSERT"):
            assert pymysql.cursors.RE_INSERT_VALUES.match(sql), f"SQL will not batch in PyMySQL: {sql}"


def test_publish_chunks_large_inserts_into_500_row_batches(valid_bundle, monkeypatch):
    """Verify _insert_many splits large row sets (e.g. 1,200 rows) into 500-row batches within one transaction."""
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    large_bundle = copy.deepcopy(valid_bundle)
    base_market_row = large_bundle["outputs"]["market_observation.csv"][0]
    many_rows = []
    for idx in range(1200):
        row = copy.deepcopy(base_market_row)
        # Distinct observation_date per row
        day_num = idx % 28 + 1
        month_num = (idx // 28) % 12 + 1
        year_num = 2000 + (idx // (28 * 12))
        row["observation_date"] = f"{year_num:04d}-{month_num:02d}-{day_num:02d}"
        many_rows.append(row)
    large_bundle["outputs"]["market_observation.csv"] = many_rows

    db = MockDB()
    db.fetchone_queue.append(None)
    db.fetchall_queue.append([{"product_id": p} for p in KNOWN_PRODUCTS])
    db.fetchall_queue.append([{"series_id": s} for s in KNOWN_SERIES])
    outputs = large_bundle["outputs"]
    for count in [
        len(outputs["fuel_price_observation.csv"]),
        len(outputs["market_observation.csv"]),
        len(outputs["fuel_market_panel.csv"]),
        len(outputs["quarantine.csv"]),
        len(large_bundle["annotations"]),
    ]:
        db.fetchone_queue.append({"n": count})
    db.fetchone_queue.append(None)

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    msg = publish(large_bundle)
    assert "market=1200" in msg
    assert db.commit_called == 1

    market_batches = [
        len(params)
        for sql, params in db.executemany_queries
        if "INSERT INTO market_observation" in sql
    ]
    assert market_batches == [500, 500, 200]


def test_publish_rolls_back_and_closes_on_connect_or_query_timeout(valid_bundle, monkeypatch):
    """Verify connection timeout and query timeout propagate cleanly and close opened connections."""
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")

    # 1. Connect timeout
    monkeypatch.setattr(
        pymysql,
        "connect",
        lambda **kwargs: (_ for _ in ()).throw(pymysql.err.OperationalError(2003, "connect timeout")),
    )
    with pytest.raises(pymysql.err.OperationalError, match="connect timeout"):
        publish(valid_bundle)

    # 2. Query timeout mid-insert -> rollback + close
    db = MockDB()
    db.fetchone_queue.append(None)
    db.fetchall_queue.append([{"product_id": p} for p in KNOWN_PRODUCTS])
    db.fetchall_queue.append([{"series_id": s} for s in KNOWN_SERIES])
    db.raise_on_query_substr = "INSERT INTO fuel_market_panel"
    db.error_to_raise = pymysql.err.OperationalError(2013, "Lost connection during query (read timeout)")
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    with pytest.raises(pymysql.err.OperationalError, match="read timeout"):
        publish(valid_bundle)
    assert db.rollback_called == 1
    assert db.close_called == 1
    assert db.commit_called == 0
    assert db.pointer_run_id is None


def test_publish_existing_run_row_count_mismatch_fails_closed(valid_bundle, monkeypatch):
    """When an existing run has a mismatched row count during reconciliation, loader refuses repair."""
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    manifest = valid_bundle["manifest"]
    quality = valid_bundle["quality"]
    db.fetchone_queue.append({
        "run_id": manifest["run_id"],
        "schema_version": manifest["schema_version"],
        "pipeline_version": manifest["pipeline_version"],
        "pipeline_sha256": manifest["pipeline_sha256"],
        "publication_status": quality["publication_status"],
        "manifest_json": json.dumps(manifest),
    })
    db.fetchone_queue.append({"report_json": json.dumps(quality)})
    db.fetchall_queue.append([
        {"snapshot_id": s["snapshot_id"]} for s in valid_bundle["sources"].values()
    ])
    # First count (fuel_price_observation) mismatches by 1
    db.fetchone_queue.append({"n": len(valid_bundle["outputs"]["fuel_price_observation.csv"]) + 1})

    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))
    with pytest.raises(LoadError, match="existing run is incomplete: fuel_price_observation count mismatch"):
        publish(valid_bundle)
    assert db.rollback_called == 1
    assert db.close_called == 1
