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
            "", "Đồng/lít thực tế", "true", "https://pvoil.com.vn", "2026-11-05T08:00:00Z",
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
