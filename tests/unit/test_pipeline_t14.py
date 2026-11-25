import csv
import json
from datetime import date
from pathlib import Path

import pytest

from petrotwin.data import pipeline


FX_HEADERS = pipeline.EXPECTED_HEADERS[pipeline.FX_FILE]


def write_fx(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(FX_HEADERS)
        writer.writerows(rows)


def valid_fx_row(day: str = "2026-11-05") -> list[str]:
    return [day, "USD", "VND", "25627", pipeline.UNIT if hasattr(pipeline, "UNIT") else "VND per 1 USD",
            pipeline.RATE_TYPE if hasattr(pipeline, "RATE_TYPE") else "SBV central reference rate",
            "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam",
            "provider=Algolab; source_id=algolab_sbv_central_rate; upstream=State Bank of Vietnam; provenance_status=provider_declared",
            "25,627", "2026-11-05T00:00:00Z"]


def parse_fx(path: Path):
    return pipeline._parse_market(
        path, "algolab_sbv_central_rate", "usd_vnd_sbv_reference",
        "rate_vnd_per_usd", "unit", "VND/USD", "VND per 1 USD", None,
        pipeline._sha256(path), FX_HEADERS,
    )


def test_fx_optional_empty_file_is_not_validated(tmp_path):
    path = tmp_path / pipeline.FX_FILE
    write_fx(path, [])
    rows, quarantine, quality = parse_fx(path)
    assert rows == []
    assert quarantine == []
    assert quality["rows_canonical"] == 0


def test_fx_required_empty_file_fails_required_series(tmp_path):
    path = tmp_path / pipeline.FX_FILE
    write_fx(path, [])
    rows, _, _ = parse_fx(path)
    brent = [{"series_id": "brent_dated_usd_bbl"}]
    with pytest.raises(pipeline.PipelineError, match="usd_vnd_sbv_reference"):
        pipeline._validate_integrated_outputs([], brent + rows, [], fx_required=True)


def test_fx_optional_missing_file_has_no_required_series(tmp_path):
    brent = [{"series_id": "brent_dated_usd_bbl"}]
    pipeline._validate_integrated_outputs([], brent, [], fx_required=False)


def test_fx_valid_file_is_canonical(tmp_path):
    path = tmp_path / pipeline.FX_FILE
    write_fx(path, [valid_fx_row()])
    rows, quarantine, quality = parse_fx(path)
    assert len(rows) == 1
    assert quarantine == []
    assert quality["rows_canonical"] == 1


def test_fx_all_records_quarantined(tmp_path):
    path = tmp_path / pipeline.FX_FILE
    row = valid_fx_row()
    row[3] = "0"
    write_fx(path, [row])
    rows, quarantine, quality = parse_fx(path)
    assert rows == []
    assert len(quarantine) == 1
    assert quality["rows_canonical"] == 0
    assert quality["quarantined_rows"] == 1


def test_explicit_nonexistent_collectors_config_raises_pipeline_error(tmp_path):
    cfg = tmp_path / "default.yaml"
    cfg.write_text("data:\n  schema_version: petrotwin-data-v3\n", encoding="utf-8")
    missing_collectors_config = tmp_path / "missing_collectors.yaml"

    with pytest.raises(pipeline.PipelineError, match="collectors config file not found"):
        pipeline.run_pipeline(
            tmp_path / "raw",
            tmp_path / "processed",
            tmp_path / "logs",
            cfg,
            collectors_config_path=missing_collectors_config,
        )


def test_omitted_collectors_config_uses_repo_default(tmp_path):
    cfg = tmp_path / "default.yaml"
    cfg.write_text("data:\n  schema_version: petrotwin-data-v3\n", encoding="utf-8")

    # When omitted (None), it loads repo default configs/collectors.yaml (where fx is optional)
    # and proceeds to check required raw source files.
    with pytest.raises(pipeline.PipelineError, match="missing required raw source files"):
        pipeline.run_pipeline(
            tmp_path / "raw",
            tmp_path / "processed",
            tmp_path / "logs",
            cfg,
            collectors_config_path=None,
        )


def test_fx_config_optional_string_rejected(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text('fx:\n  optional: "false"\n  max_age_days: 7\n', encoding="utf-8")

    with pytest.raises(pipeline.PipelineError, match="collectors_config.fx.optional must be a boolean"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )


def test_fx_config_optional_number_rejected(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"

    coll_cfg.write_text("fx:\n  optional: 0\n  max_age_days: 7\n", encoding="utf-8")
    with pytest.raises(pipeline.PipelineError, match="collectors_config.fx.optional must be a boolean"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )

    coll_cfg.write_text("fx:\n  optional: 1\n  max_age_days: 7\n", encoding="utf-8")
    with pytest.raises(pipeline.PipelineError, match="collectors_config.fx.optional must be a boolean"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )


def test_fx_config_max_age_days_negative_rejected(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: -1\n", encoding="utf-8")

    with pytest.raises(pipeline.PipelineError, match="collectors_config.fx.max_age_days must be a non-negative integer"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )


def test_fx_config_max_age_days_float_rejected(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7.5\n", encoding="utf-8")

    with pytest.raises(pipeline.PipelineError, match="collectors_config.fx.max_age_days must be a non-negative integer"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )


def test_fx_config_malformed_rejected(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"

    coll_cfg.write_text("fx: not_a_mapping\n", encoding="utf-8")
    with pytest.raises(pipeline.PipelineError, match="collectors_config.fx must be a mapping"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )

    coll_cfg.write_text("fx: 12345\n", encoding="utf-8")
    with pytest.raises(pipeline.PipelineError, match="collectors_config.fx must be a mapping"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )


def test_fx_config_omitted_fields_preserve_defaults(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"

    # fx mapping without optional -> defaults to optional=False (required), missing FX fails
    coll_cfg.write_text("fx:\n  max_age_days: 10\n", encoding="utf-8")
    with pytest.raises(pipeline.PipelineError, match="missing required raw source files"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
        )

    # fx mapping without max_age_days -> defaults to max_age_days=7
    write_fx(raw_dir / pipeline.FX_FILE, [valid_fx_row("2026-11-04")])
    coll_cfg.write_text("fx:\n  optional: true\n", encoding="utf-8")
    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )
    assert quality["source_checks"]["usd_vnd"]["max_age_days"] == 7


def _setup_base_pipeline(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = tmp_path / "processed"
    log_dir = tmp_path / "logs"

    cfg = tmp_path / "default.yaml"
    cfg.write_text("""
project:
  timezone: Asia/Ho_Chi_Minh
  target_column: price_value
  target_key: product_id
  date_column: effective_at_local
data:
  schema_version: petrotwin-data-v3
  duplicate_policy: collapse_exact_fail_conflict
  timestamps:
    source_dates: calendar_date
    extraction_timezone: UTC
    domestic_effective_timezone: Asia/Ho_Chi_Minh
    allow_end_of_day_24_00: true
    end_of_day_normalization: next_day_midnight
  units:
    brent_dated_usd_bbl: USD/bbl
    usd_vnd_sbv_reference: VND/USD
    domestic_fuel_price: VND/liter
""", encoding="utf-8")

    with (raw_dir / pipeline.BRENT_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.BRENT_FILE])
        w.writerow([
            "2026-11-01", "Crude Oil Prices: Brent - Europe", "100.5", "Dollars per Barrel",
            "https://fred.stlouisfed.org/data/DCOILBRENTEU", "DCOILBRENTEU",
            "U.S. Energy Information Administration (EIA), distributed by FRED",
            "100.5", "observed", "2026-11-01T00:00:00Z",
        ])

    with (raw_dir / pipeline.FUEL_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE])
        w.writerow([
            "2026-11-05", "2026-11-05", "15:00", "Xăng RON 95-III", "21000", "21.000 đ", "0",
            "", "Đồng/lít thực tế", "true", "https://comeco.vn/", "2026-11-05T08:00:00Z",
        ])

    return raw_dir, processed_dir, log_dir, cfg


def test_fx_stale_optional_records_metadata_and_continues(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 5\n", encoding="utf-8")

    write_fx(raw_dir / pipeline.FX_FILE, [valid_fx_row("2026-10-25")])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )

    fx_check = quality["source_checks"]["usd_vnd"]
    assert fx_check["status"] == "optional_source_stale"
    assert fx_check["latest_observation_date"] == "2026-10-25"
    assert fx_check["age_days"] == 12
    assert fx_check["max_age_days"] == 5
    assert quality["publication_status"] == "validated_with_optional_source_stale"
    assert quality["optional_sources_stale"] == [pipeline.FX_FILE]
    assert quality["integrated"]["market_observation_rows"] == 1


def test_fx_stale_required_raises_clear_error(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: false\n  max_age_days: 5\n", encoding="utf-8")

    write_fx(raw_dir / pipeline.FX_FILE, [valid_fx_row("2026-10-25")])

    with pytest.raises(pipeline.PipelineError, match="required FX source is stale") as exc:
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
            current_date=date(2026, 11, 6),
        )
    assert "2026-10-25" in str(exc.value)
    assert "12 days old" in str(exc.value)
    assert "max_age_days=5" in str(exc.value)


def test_fx_missing_optional_records_status(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )

    fx_check = quality["source_checks"]["usd_vnd"]
    assert fx_check["status"] == "optional_source_missing"
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_missing"] == [pipeline.FX_FILE]
    assert quality["optional_sources_stale"] == []


def test_fx_empty_optional_records_status(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    write_fx(raw_dir / pipeline.FX_FILE, [])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )

    fx_check = quality["source_checks"]["usd_vnd"]
    assert fx_check["status"] == "optional_source_empty"
    assert fx_check["condition"] == "empty"
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_empty"] == [pipeline.FX_FILE]
    assert quality["optional_sources_missing"] == []
    assert quality["optional_sources_unusable"] == []
    assert quality["optional_sources_stale"] == []


def test_fx_unusable_all_quarantined_optional_records_status(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    row = valid_fx_row()
    row[3] = "-500"  # Non-positive value triggers quarantine
    row[8] = "-500"
    write_fx(raw_dir / pipeline.FX_FILE, [row])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )

    fx_check = quality["source_checks"]["usd_vnd"]
    assert "status" not in fx_check
    assert fx_check["condition"] == "unusable"
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_unusable"] == [pipeline.FX_FILE]
    assert quality["optional_sources_missing"] == []
    assert quality["optional_sources_empty"] == []
    assert quality["optional_sources_stale"] == []

    quality_reports = list(processed_dir.glob("runs/*/quality_report.json"))
    assert len(quality_reports) == 1
    json_quality = json.loads(quality_reports[0].read_text(encoding="utf-8"))
    assert "status" not in json_quality["source_checks"]["usd_vnd"]
    assert json_quality["optional_sources_unusable"] == [pipeline.FX_FILE]


def test_fx_valid_fresh_optional(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    write_fx(raw_dir / pipeline.FX_FILE, [valid_fx_row("2026-11-04")])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )

    fx_check = quality["source_checks"]["usd_vnd"]
    assert fx_check["status"] == "validated"
    assert fx_check["condition"] == "validated"
    assert fx_check["age_days"] == 2
    assert quality["publication_status"] == "validated"
    assert quality["optional_sources_missing"] == []
    assert quality["optional_sources_empty"] == []
    assert quality["optional_sources_unusable"] == []
    assert quality["optional_sources_stale"] == []
    assert quality["integrated"]["market_observation_rows"] == 2


def test_fx_max_age_days_exact_boundary_and_over_boundary(tmp_path):
    # Boundary: max_age_days = 7
    # Date 2026-11-05 with current_date 2026-11-12: age = 7 (exact boundary -> fresh / validated)
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    write_fx(raw_dir / pipeline.FX_FILE, [valid_fx_row("2026-11-05")])

    q_boundary = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 12),
    )
    fx_boundary = q_boundary["source_checks"]["usd_vnd"]
    assert fx_boundary["status"] == "validated"
    assert fx_boundary["condition"] == "validated"
    assert fx_boundary["age_days"] == 7
    assert q_boundary["publication_status"] == "validated"
    assert q_boundary["optional_sources_stale"] == []

    # One day over boundary: current_date 2026-11-13: age = 8 -> stale
    processed_dir_stale = tmp_path / "processed_stale"
    q_stale = pipeline.run_pipeline(
        raw_dir, processed_dir_stale, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 13),
    )
    fx_stale = q_stale["source_checks"]["usd_vnd"]
    assert fx_stale["status"] == "optional_source_stale"
    assert fx_stale["condition"] == "stale"
    assert fx_stale["age_days"] == 8
    assert q_stale["publication_status"] == "validated_with_optional_source_stale"
    assert q_stale["optional_sources_stale"] == [pipeline.FX_FILE]


def test_fx_required_missing_file_fails(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: false\n  max_age_days: 7\n", encoding="utf-8")
    # FX_FILE is missing

    with pytest.raises(pipeline.PipelineError, match="missing required raw source files"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
            current_date=date(2026, 11, 6),
        )


def test_fx_required_empty_file_fails(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: false\n  max_age_days: 7\n", encoding="utf-8")
    write_fx(raw_dir / pipeline.FX_FILE, [])

    with pytest.raises(pipeline.PipelineError, match="required FX source is empty"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
            current_date=date(2026, 11, 6),
        )


def test_fx_required_unusable_all_quarantined_fails(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: false\n  max_age_days: 7\n", encoding="utf-8")
    row = valid_fx_row()
    row[3] = "-1"
    row[8] = "-1"
    write_fx(raw_dir / pipeline.FX_FILE, [row])

    with pytest.raises(pipeline.PipelineError, match="required FX source is unusable"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg,
            collectors_config_path=coll_cfg,
            current_date=date(2026, 11, 6),
        )


def test_fx_optional_missing_with_unrelated_quarantine(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    # Append an invalid Brent row that will trigger quarantine
    with (raw_dir / pipeline.BRENT_FILE).open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "2026-11-02", "Crude Oil Prices: Brent - Europe", "-10.0", "Dollars per Barrel",
            "https://fred.stlouisfed.org/data/DCOILBRENTEU", "DCOILBRENTEU",
            "U.S. Energy Information Administration (EIA), distributed by FRED",
            "-10.0", "observed", "2026-11-02T00:00:00Z",
        ])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_missing"] == [pipeline.FX_FILE]
    assert quality["integrated"]["quarantined_rows"] == 1
    assert quality["integrated"]["quarantine_reason_counts"] == {"market_value_not_positive": 1}
    assert (processed_dir / "runs" / quality["run_id"] / "quarantine.csv").is_file()


def test_fx_optional_empty_with_unrelated_quarantine(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    write_fx(raw_dir / pipeline.FX_FILE, [])
    # Append invalid Brent row
    with (raw_dir / pipeline.BRENT_FILE).open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "2026-11-02", "Crude Oil Prices: Brent - Europe", "-10.0", "Dollars per Barrel",
            "https://fred.stlouisfed.org/data/DCOILBRENTEU", "DCOILBRENTEU",
            "U.S. Energy Information Administration (EIA), distributed by FRED",
            "-10.0", "observed", "2026-11-02T00:00:00Z",
        ])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_empty"] == [pipeline.FX_FILE]
    assert quality["optional_sources_missing"] == []
    assert quality["integrated"]["quarantined_rows"] == 1


def test_fx_optional_unusable_with_unrelated_quarantine(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    # 1 quarantined FX row
    row = valid_fx_row()
    row[3] = "-100"
    row[8] = "-100"
    write_fx(raw_dir / pipeline.FX_FILE, [row])
    # 1 quarantined Brent row
    with (raw_dir / pipeline.BRENT_FILE).open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "2026-11-02", "Crude Oil Prices: Brent - Europe", "-10.0", "Dollars per Barrel",
            "https://fred.stlouisfed.org/data/DCOILBRENTEU", "DCOILBRENTEU",
            "U.S. Energy Information Administration (EIA), distributed by FRED",
            "-10.0", "observed", "2026-11-02T00:00:00Z",
        ])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_unusable"] == [pipeline.FX_FILE]
    assert quality["integrated"]["quarantined_rows"] == 2
    assert quality["source_checks"]["brent"]["quarantined_rows"] == 1
    assert quality["source_checks"]["usd_vnd"]["quarantined_rows"] == 1


def test_fx_optional_stale_with_unrelated_quarantine(tmp_path):
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 5\n", encoding="utf-8")
    # FX is stale (12 days old)
    write_fx(raw_dir / pipeline.FX_FILE, [valid_fx_row("2026-10-25")])
    # Brent has 1 quarantined row
    with (raw_dir / pipeline.BRENT_FILE).open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "2026-11-02", "Crude Oil Prices: Brent - Europe", "-10.0", "Dollars per Barrel",
            "https://fred.stlouisfed.org/data/DCOILBRENTEU", "DCOILBRENTEU",
            "U.S. Energy Information Administration (EIA), distributed by FRED",
            "-10.0", "observed", "2026-11-02T00:00:00Z",
        ])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )
    assert quality["publication_status"] == "validated_with_optional_source_stale"
    assert quality["optional_sources_stale"] == [pipeline.FX_FILE]
    assert quality["integrated"]["quarantined_rows"] == 1
    assert quality["source_checks"]["brent"]["quarantined_rows"] == 1
    assert quality["source_checks"]["usd_vnd"]["quarantined_rows"] == 0


def test_iss03_event_level_fx_freshness_detects_stale_event_when_latest_fx_is_fresh(tmp_path):
    """Regression (ISS-03): When FX has a gap (e.g. 2026-10-22 then 2026-11-16) and a fuel event
    occurs on 2026-11-12, the pipeline must:
    1. Join 2026-11-12 strictly to 2026-10-22 (no look-ahead leakage from 2026-11-16).
    2. Preserve fx_observation_date, fx_age_days (21), fx_source_id, and fx_snapshot_id in fuel_market_panel.csv.
    3. Identify the 2026-11-12 event as having stale FX (21 > max_age_days=7) in event_freshness even though
       the latest FX file date (2026-11-16) has age_days=0.
    """
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")

    # Write fuel event on 2026-11-12
    with (raw_dir / pipeline.FUEL_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE])
        w.writerow([
            "2026-11-12", "2026-11-12", "15:00", "Xăng RON 95-III", "21000", "21.000 đ", "",
            "", "Đồng/lít thực tế", "true", "https://comeco.vn/", "2026-11-12T08:00:00Z",
        ])

    # Write FX with a 25-day gap: 2026-10-22 and 2026-11-16
    write_fx(raw_dir / pipeline.FX_FILE, [
        valid_fx_row("2026-10-22"),
        valid_fx_row("2026-11-16"),
    ])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 16),
    )

    fx_check = quality["source_checks"]["usd_vnd"]
    assert fx_check["latest_observation_date"] == "2026-11-16"
    assert fx_check["age_days"] == 0
    assert fx_check["stale_fx_events_count"] == 1
    assert fx_check["max_event_fx_age_days"] == 21

    ev_fresh = fx_check["event_freshness"]
    assert ev_fresh["max_age_days"] == 7
    assert ev_fresh["events_evaluated"] == 1
    assert ev_fresh["fresh_events_count"] == 0
    assert ev_fresh["stale_events_count"] == 1
    assert ev_fresh["has_stale_events"] is True
    assert len(ev_fresh["stale_events"]) == 1
    assert ev_fresh["stale_events"][0]["fx_observation_date"] == "2026-10-22"
    assert ev_fresh["stale_events"][0]["fx_age_days"] == 21
    assert ev_fresh["stale_events"][0]["fx_source_id"] == "algolab_sbv_central_rate"
    assert ev_fresh["stale_events"][0]["fx_snapshot_id"] != ""

    # Verify panel row preserves fx_observation_date, fx_age_days, fx_source_id, fx_snapshot_id and has no look-ahead
    panel_path = processed_dir / "runs" / quality["run_id"] / "fuel_market_panel.csv"
    with panel_path.open(newline="", encoding="utf-8") as fh:
        panel_rows = list(csv.DictReader(fh))
    assert len(panel_rows) == 1
    assert panel_rows[0]["fx_observation_date"] == "2026-10-22"
    assert panel_rows[0]["fx_age_days"] == "21"
    assert panel_rows[0]["fx_source_id"] == "algolab_sbv_central_rate"
    assert panel_rows[0]["fx_snapshot_id"] == pipeline._sha256(raw_dir / pipeline.FX_FILE)


def test_iss03_event_fx_freshness_rejects_look_ahead_observation():
    """Regression (ISS-03): _assess_event_fx_freshness must fail closed if fx_observation_date >= event_date."""
    with pytest.raises(pipeline.PipelineError, match="look-ahead FX observation"):
        pipeline._assess_event_fx_freshness([
            {
                "product_id": "gasoline_e10_ron95_iii",
                "effective_at_local": "2026-11-12T15:00:00+07:00",
                "fx_observation_date": "2026-11-12",
                "fx_age_days": 0,
                "fx_source_id": "algolab_sbv_central_rate",
                "fx_snapshot_id": "abc",
            }
        ], fx_max_age_days=7)


def test_iss01_gitignore_whitelists_canonical_raw_files_and_loader_rejects_dangling_current_json(tmp_path):
    """Regression (ISS-01): Canonical raw CSVs (including comeco_fuel_prices.csv) must not be ignored by .gitignore,
    temp/untracked run artifacts remain ignored, and prepare_bundle fails closed if current.json points to a missing run."""
    import subprocess
    from petrotwin.data import mysql_loader

    repo_root = pipeline._repo_root()
    must_not_ignore = [
        "data/raw/README.md",
        "data/raw/brent_crude_prices.csv",
        "data/raw/usd_vnd_rates.csv",
        "data/raw/data_price_petro.csv",
        "data/raw/comeco_fuel_prices.csv",
        "data/manual/stabilization_fund_annotations.csv",
        "tests/fixtures/sample.csv",
    ]
    for rel_path in must_not_ignore:
        res = subprocess.run(
            ["git", "check-ignore", "-q", rel_path],
            cwd=repo_root,
            check=False,
        )
        assert res.returncode == 1, f"{rel_path} should NOT be ignored by .gitignore"

    must_ignore = [
        "data/raw/comeco_fuel_prices_tmp_12345.csv",
        "data/raw/unrelated_scratch.csv",
        "data/processed/runs/.staging_run/manifest.json",
    ]
    for rel_path in must_ignore:
        res = subprocess.run(
            ["git", "check-ignore", "-q", rel_path],
            cwd=repo_root,
            check=False,
        )
        assert res.returncode == 0, f"{rel_path} SHOULD be ignored by .gitignore"

    # Verify prepare_bundle fails closed with LoadError when current.json references a missing run directory
    proc_dir = tmp_path / "data" / "processed"
    proc_dir.mkdir(parents=True)
    (proc_dir / "current.json").write_text(
        json.dumps({
            "run_id": "deadbeef1234567890ab",
            "path": "runs/deadbeef1234567890ab",
            "schema_version": "petrotwin-data-v3",
            "published_at_utc": "2026-11-16T08:55:01Z",
        }),
        encoding="utf-8",
    )
    with pytest.raises(mysql_loader.LoadError, match="current.json references missing run directory"):
        mysql_loader.prepare_bundle(tmp_path)


def test_pvoil_rows_without_source_url_in_data_price_petro_are_accepted_as_pvoil(tmp_path):
    """Rows in data_price_petro.csv that have an empty source_url (when no specific URL was provided)
    are assigned source_id='pvoil' and keep source_url='', while comeco_fuel_prices.csv still requires source_url."""
    pvoil_file = tmp_path / pipeline.FUEL_FILE
    with pvoil_file.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE])
        w.writerow([
            "2026-10-29", "2026-10-29", "15:00", "Xăng E10 RON 95-III", "25730", "25.730 đ",
            "870", "+870", "Đồng/lít thực tế", "true", "", "2026-11-16T17:13:24Z",
        ])

    rows, quarantine, _ = pipeline._parse_fuel(
        pvoil_file, pipeline._sha256(pvoil_file), pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE]
    )
    assert len(rows) == 1
    assert quarantine == []
    assert rows[0]["source_id"] == "pvoil"
    assert rows[0]["source_url"] == ""

    comeco_file = tmp_path / pipeline.COMECO_FUEL_FILE
    with comeco_file.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.COMECO_FUEL_FILE])
        w.writerow([
            "2026-11-12", "2026-11-12", "15:00", "Xăng E10 RON 95-III", "19840", "19.840 đ",
            "", "", "Đồng/lít thực tế", "true", "", "2026-11-16T08:33:01Z",
        ])
    with pytest.raises(pipeline.PipelineError, match="source_url is required"):
        pipeline._parse_fuel(
            comeco_file, pipeline._sha256(comeco_file), pipeline.EXPECTED_HEADERS[pipeline.COMECO_FUEL_FILE]
        )


def test_current_repo_raw_datasets_validate_end_to_end_in_isolated_temp_dir(tmp_path):
    """Validate all current repository raw datasets end-to-end in an isolated tmp_path without
    touching repository data/processed/*."""
    import shutil
    from petrotwin.data import mysql_loader

    repo_root = pipeline._repo_root()
    iso_repo = tmp_path / "iso_repo"
    (iso_repo / "data" / "raw").mkdir(parents=True)
    (iso_repo / "data" / "manual").mkdir(parents=True)
    (iso_repo / "data" / "processed").mkdir(parents=True)
    (iso_repo / "configs").mkdir(parents=True)

    for fname in ("brent_crude_prices.csv", "usd_vnd_rates.csv", "data_price_petro.csv", "comeco_fuel_prices.csv"):
        shutil.copy2(repo_root / "data" / "raw" / fname, iso_repo / "data" / "raw" / fname)
    shutil.copy2(
        repo_root / "data" / "manual" / "stabilization_fund_annotations.csv",
        iso_repo / "data" / "manual" / "stabilization_fund_annotations.csv",
    )
    shutil.copy2(repo_root / "configs" / "default.yaml", iso_repo / "configs" / "default.yaml")
    shutil.copy2(repo_root / "configs" / "collectors.yaml", iso_repo / "configs" / "collectors.yaml")

    with (iso_repo / "data" / "raw" / "usd_vnd_rates.csv").open(newline="", encoding="utf-8") as fh:
        fx_dates = [date.fromisoformat(row["date"]) for row in csv.DictReader(fh) if row.get("date")]
    run_date = max([date(2026, 11, 16), *fx_dates])

    quality = pipeline.run_pipeline(
        iso_repo / "data" / "raw",
        iso_repo / "data" / "processed",
        iso_repo / "data" / "interim" / "logs",
        iso_repo / "configs" / "default.yaml",
        iso_repo / "configs" / "collectors.yaml",
        current_date=run_date,
    )
    bundle = mysql_loader.prepare_bundle(iso_repo)

    panel = bundle["outputs"]["fuel_market_panel.csv"]
    comeco_panel = [r for r in panel if r["effective_at_local"] == "2026-11-12T15:00:00+07:00"]
    assert len(comeco_panel) == 4
    assert {r["product_id"] for r in comeco_panel} == {
        "gasoline_e10_ron95_iii",
        "gasoline_e5_ron92_ii",
        "diesel_0001s_v",
        "diesel_005s_ii",
    }
    for r in comeco_panel:
        assert r["target_source_id"] == "comeco"
        assert r["price_change_value"] == ""
        assert r["brent_observation_date"] == "2026-11-11"
        assert r["brent_age_days"] == "1"
        assert r["fx_observation_date"] == "2026-11-11"
        assert r["fx_age_days"] == "1"
        assert r["fx_observation_date"] != "2026-11-16"

    pvoil_oct29 = [r for r in panel if r["effective_at_local"] == "2026-10-29T15:00:00+07:00"]
    pvoil_nov05 = [r for r in panel if r["effective_at_local"] == "2026-11-05T15:00:00+07:00"]
    assert len(pvoil_oct29) == 4
    assert len(pvoil_nov05) == 4
    assert all(r["target_source_id"] == "pvoil" for r in pvoil_oct29 + pvoil_nov05)
    assert quality["source_checks"]["brent"]["rows_canonical"] >= 2146
    assert quality["source_checks"]["usd_vnd"]["rows_canonical"] >= 2417
    assert quality["integrated"]["fuel_price_observation_rows"] >= 1402
    assert quality["integrated"]["fuel_market_panel_rows"] >= 1402


def test_fx_audit_multi_date_as_of_join_excludes_same_day_and_future_for_pvoil_and_comeco(tmp_path):
    """Audit T14: With multiple FX observations including same-day (2026-11-12) and future (2026-11-16),
    both PVOIL and COMECO receive the strictly prior FX observation (and extracted_at_utc does not
    alter observation_date)."""
    raw_dir, processed_dir, log_dir, cfg = _setup_base_pipeline(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")

    # PVOIL on 2026-11-05, COMECO on 2026-11-12
    with (raw_dir / pipeline.FUEL_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE])
        w.writerow([
            "2026-11-05", "2026-11-05", "15:00", "Xăng RON 95-III", "21150", "21.150 đ", "-2080",
            "-2080", "Đồng/lít thực tế", "true", "https://www.pvoil.com.vn/tin-gia-xang-dau", "2026-11-05T08:00:00Z",
        ])
    with (raw_dir / pipeline.COMECO_FUEL_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.COMECO_FUEL_FILE])
        w.writerow([
            "2026-11-16", "2026-11-12", "15:00", "Xăng E10 RON 95-III", "19840", "19.840 đ", "",
            "", "Đồng/lít thực tế", "true", "https://comeco.vn/", "2026-11-16T08:33:01Z",
        ])

    # FX rows: 2026-11-04 (prior to PVOIL), 2026-11-11 (extracted later on 2026-11-16!), 2026-11-12 (same-day), 2026-11-16 (future)
    fx_rows = [
        ["2026-11-04", "USD", "VND", "25627", "VND per 1 USD", "SBV central reference rate",
         "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam", "Algolab", "25,627", "2026-11-16T17:16:26Z"],
        ["2026-11-11", "USD", "VND", "25611", "VND per 1 USD", "SBV central reference rate",
         "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam", "Algolab", "25,611", "2026-11-16T17:16:26Z"],
        ["2026-11-12", "USD", "VND", "25623", "VND per 1 USD", "SBV central reference rate",
         "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam", "Algolab", "25,623", "2026-11-16T17:16:26Z"],
        ["2026-11-16", "USD", "VND", "25639", "VND per 1 USD", "SBV central reference rate",
         "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam", "Algolab", "25,639", "2026-11-16T08:53:55Z"],
    ]
    write_fx(raw_dir / pipeline.FX_FILE, fx_rows)

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 16),
    )

    panel_path = processed_dir / "runs" / quality["run_id"] / "fuel_market_panel.csv"
    with panel_path.open(newline="", encoding="utf-8") as fh:
        by_src = {r["target_source_id"]: r for r in csv.DictReader(fh)}

    assert by_src["pvoil"]["fx_observation_date"] == "2026-11-04"
    assert by_src["pvoil"]["usd_vnd_rate"] == "25627"
    assert by_src["pvoil"]["fx_age_days"] == "1"

    assert by_src["comeco"]["fx_observation_date"] == "2026-11-11"
    assert by_src["comeco"]["usd_vnd_rate"] == "25611"
    assert by_src["comeco"]["fx_age_days"] == "1"


def test_fx_duplicate_exact_collapsed_and_conflicting_duplicate_rejected(tmp_path):
    """Audit T14: Exact duplicate FX rows are collapsed; conflicting duplicates on the same date fail closed."""
    path_exact = tmp_path / "exact.csv"
    row = valid_fx_row("2026-11-11")
    write_fx(path_exact, [row, row])
    rows, quarantine, quality = parse_fx(path_exact)
    assert len(rows) == 1
    assert quarantine == []
    assert quality["exact_duplicates_collapsed"] == 1

    path_conflict = tmp_path / "conflict.csv"
    row2 = valid_fx_row("2026-11-11")
    row2[3] = "25999"
    row2[8] = "25,999"
    write_fx(path_conflict, [row, row2])
    with pytest.raises(pipeline.PipelineError, match="conflicting duplicate key"):
        parse_fx(path_conflict)
