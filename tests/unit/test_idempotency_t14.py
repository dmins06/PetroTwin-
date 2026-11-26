"""Regression tests for T14 same-day idempotency and immutable manifest reconciliation."""

from __future__ import annotations

import copy
import json

import pymysql
import pytest

from petrotwin.data.mysql_loader import LoadError, _immutable_manifest_identity, publish
from tests.unit.test_mysql_loader import MockConnection, MockDB

pytest_plugins = ["tests.unit.test_mysql_loader"]


def _queue_existing_complete_run(
    db: MockDB,
    bundle: dict,
    db_manifest: dict | str,
    *,
    fuel_count_override: int | None = None,
) -> None:
    manifest = bundle["manifest"]
    quality = bundle["quality"]
    outputs = bundle["outputs"]
    db.fetchone_queue.append(
        {
            "run_id": manifest["run_id"],
            "schema_version": manifest["schema_version"],
            "pipeline_version": manifest["pipeline_version"],
            "pipeline_sha256": manifest["pipeline_sha256"],
            "publication_status": quality["publication_status"],
            "manifest_json": db_manifest,
        }
    )
    db.fetchone_queue.append({"report_json": quality})
    db.fetchall_queue.append(
        [{"snapshot_id": s["snapshot_id"]} for s in bundle["sources"].values()]
    )
    counts = [
        len(outputs["fuel_price_observation.csv"])
        if fuel_count_override is None
        else fuel_count_override,
        len(outputs["market_observation.csv"]),
        len(outputs["fuel_market_panel.csv"]),
        len(outputs["quarantine.csv"]),
        len(bundle["annotations"]),
    ]
    for count in counts:
        db.fetchone_queue.append({"n": count})


def test_same_run_id_different_retrieval_timestamps_reconciles_safely(valid_bundle, monkeypatch):
    """Same-day rerun with identical immutable files but new retrieved_at_utc must reconcile."""
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()

    stored_manifest = copy.deepcopy(valid_bundle["manifest"])
    for src in stored_manifest["sources"]:
        src["retrieved_at_utc"] = "2026-11-25T01:00:00Z"

    for src in valid_bundle["manifest"]["sources"]:
        src["retrieved_at_utc"] = "2026-11-25T16:30:00Z"

    _queue_existing_complete_run(db, valid_bundle, json.dumps(stored_manifest))
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    msg = publish(valid_bundle)
    assert "already exists and its data/metadata reconcile; no rows changed" in msg
    assert db.rollback_called == 1
    assert db.commit_called == 0


def test_same_run_id_changed_source_sha256_is_rejected(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()

    stored_manifest = copy.deepcopy(valid_bundle["manifest"])
    stored_manifest["sources"][0]["sha256"] = "0" * 64

    db.fetchone_queue.append(
        {
            "run_id": stored_manifest["run_id"],
            "schema_version": stored_manifest["schema_version"],
            "pipeline_version": stored_manifest["pipeline_version"],
            "pipeline_sha256": stored_manifest["pipeline_sha256"],
            "publication_status": valid_bundle["quality"]["publication_status"],
            "manifest_json": stored_manifest,
        }
    )
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    with pytest.raises(LoadError, match="existing run manifest outputs or sources do not match"):
        publish(valid_bundle)
    assert db.rollback_called == 1


def test_same_run_id_changed_snapshot_or_provenance_is_rejected(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    for field_name, fake_val in (
        ("snapshot_id", "f" * 64),
        ("publisher_or_attribution", "Tampered Publisher"),
    ):
        db = MockDB()
        stored_manifest = copy.deepcopy(valid_bundle["manifest"])
        stored_manifest["sources"][0][field_name] = fake_val
        db.fetchone_queue.append(
            {
                "run_id": stored_manifest["run_id"],
                "schema_version": stored_manifest["schema_version"],
                "pipeline_version": stored_manifest["pipeline_version"],
                "pipeline_sha256": stored_manifest["pipeline_sha256"],
                "publication_status": valid_bundle["quality"]["publication_status"],
                "manifest_json": stored_manifest,
            }
        )
        monkeypatch.setattr(pymysql, "connect", lambda _db=db, **kwargs: MockConnection(_db))
        with pytest.raises(
            LoadError, match="existing run manifest outputs or sources do not match"
        ):
            publish(valid_bundle)
        assert db.rollback_called == 1


def test_same_run_id_changed_output_hash_is_rejected(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()

    stored_manifest = copy.deepcopy(valid_bundle["manifest"])
    stored_manifest["outputs"]["fuel_market_panel.csv"]["sha256"] = "e" * 64
    db.fetchone_queue.append(
        {
            "run_id": stored_manifest["run_id"],
            "schema_version": stored_manifest["schema_version"],
            "pipeline_version": stored_manifest["pipeline_version"],
            "pipeline_sha256": stored_manifest["pipeline_sha256"],
            "publication_status": valid_bundle["quality"]["publication_status"],
            "manifest_json": stored_manifest,
        }
    )
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    with pytest.raises(LoadError, match="existing run manifest outputs or sources do not match"):
        publish(valid_bundle)
    assert db.rollback_called == 1


def test_incomplete_existing_run_is_rejected(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    db = MockDB()
    stored_manifest = copy.deepcopy(valid_bundle["manifest"])
    _queue_existing_complete_run(db, valid_bundle, stored_manifest, fuel_count_override=0)
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: MockConnection(db))

    with pytest.raises(LoadError, match="count mismatch; no auto-repair"):
        publish(valid_bundle)
    assert db.rollback_called == 1


def test_invalid_or_malformed_manifest_json_is_rejected(valid_bundle, monkeypatch):
    monkeypatch.setenv("PETROTWIN_DB_PASSWORD", "mock_pass")
    for bad_manifest in ("{not-valid-json", {"sources": "not-a-list", "outputs": {}}):
        db = MockDB()
        manifest = valid_bundle["manifest"]
        db.fetchone_queue.append(
            {
                "run_id": manifest["run_id"],
                "schema_version": manifest["schema_version"],
                "pipeline_version": manifest["pipeline_version"],
                "pipeline_sha256": manifest["pipeline_sha256"],
                "publication_status": valid_bundle["quality"]["publication_status"],
                "manifest_json": bad_manifest,
            }
        )
        monkeypatch.setattr(pymysql, "connect", lambda _db=db, **kwargs: MockConnection(_db))
        with pytest.raises(LoadError, match="existing run manifest_json is invalid"):
            publish(valid_bundle)
        assert db.rollback_called == 1


def test_immutable_manifest_identity_excludes_only_retrieved_at_utc(valid_bundle):
    m1 = copy.deepcopy(valid_bundle["manifest"])
    m2 = copy.deepcopy(valid_bundle["manifest"])
    m2["sources"][0]["retrieved_at_utc"] = "2099-01-01T00:00:00Z"
    assert _immutable_manifest_identity(m1) == _immutable_manifest_identity(m2)
