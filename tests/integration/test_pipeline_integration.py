import csv
from pathlib import Path

import pytest

from petrotwin.collectors import runner
from petrotwin.data import pipeline


def setup_minimal_environment(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = tmp_path / "processed"
    log_dir = tmp_path / "logs"

    # Default pipeline config
    cfg_path = tmp_path / "default.yaml"
    cfg_path.write_text(
        """
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
""",
        encoding="utf-8",
    )

    # Valid Brent CSV
    with (raw_dir / pipeline.BRENT_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.BRENT_FILE])
        w.writerow([
            "2026-11-01",
            "Crude Oil Prices: Brent - Europe",
            "100.5",
            "Dollars per Barrel",
            "https://fred.stlouisfed.org/data/DCOILBRENTEU",
            "DCOILBRENTEU",
            "U.S. Energy Information Administration (EIA), distributed by FRED",
            "100.5",
            "observed",
            "2026-11-01T00:00:00Z",
        ])

    # Valid Domestic Fuel CSV (effective date: 2026-11-05, strictly after brent date 2026-11-01)
    with (raw_dir / pipeline.FUEL_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE])
        w.writerow([
            "2026-11-05",
            "2026-11-05",
            "15:00",
            "Xăng RON 95-III",
            "21000",
            "21.000 đ",
            "0",
            "",
            "Đồng/lít thực tế",
            "true",
            "https://pvoil.com.vn",
            "2026-11-05T08:00:00Z",
        ])

    return raw_dir, processed_dir, log_dir, cfg_path


# 1. FX optional không có file: pipeline vẫn xử lý được nếu các nguồn bắt buộc hợp lệ.
def test_integration_case1_fx_optional_no_file(tmp_path):
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n", encoding="utf-8")

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path, collectors_config_path=coll_cfg
    )
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_missing"] == [pipeline.FX_FILE]
    assert quality["integrated"]["market_observation_rows"] == 1
    assert quality["integrated"]["fuel_price_observation_rows"] == 1


# 2. FX optional có file rỗng: publication status phản ánh đúng tình trạng thiếu dữ liệu.
def test_integration_case2_fx_optional_empty_file(tmp_path):
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n", encoding="utf-8")

    # Empty FX file (header only)
    with (raw_dir / pipeline.FX_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FX_FILE])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path, collectors_config_path=coll_cfg
    )
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert quality["optional_sources_missing"] == []
    assert quality["optional_sources_empty"] == [pipeline.FX_FILE]
    assert quality["optional_sources_unusable"] == []
    assert quality["optional_sources_stale"] == []


# 3. FX optional có dữ liệu hợp lệ: được tích hợp bình thường.
def test_integration_case3_fx_optional_valid_data(tmp_path):
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n", encoding="utf-8")

    with (raw_dir / pipeline.FX_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FX_FILE])
        w.writerow([
            "2026-11-01",
            "USD",
            "VND",
            "25627",
            "VND per 1 USD",
            "SBV central reference rate",
            "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam",
            "Algolab",
            "25,627",
            "2026-11-01T00:00:00Z",
        ])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path, collectors_config_path=coll_cfg
    )
    assert quality["publication_status"] == "validated"
    assert quality["optional_sources_missing"] == []
    assert quality["integrated"]["market_observation_rows"] == 2


# 4. FX có schema không hợp lệ: pipeline thất bại.
def test_integration_case4_fx_invalid_schema_fails(tmp_path):
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n", encoding="utf-8")

    # Mismatched header
    with (raw_dir / pipeline.FX_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "wrong_column", "rate"])
        w.writerow(["2026-11-01", "USD", "25627"])

    with pytest.raises(pipeline.PipelineError, match="schema mismatch"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg_path, collectors_config_path=coll_cfg
        )


# 5. FX có dữ liệu nhưng toàn bộ bản ghi bị quarantine: trạng thái phải chính xác.
def test_integration_case5_fx_all_records_quarantined(tmp_path):
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n", encoding="utf-8")

    # Non-positive rate triggers quarantine
    with (raw_dir / pipeline.FX_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FX_FILE])
        w.writerow([
            "2026-11-01",
            "USD",
            "VND",
            "0",
            "VND per 1 USD",
            "SBV central reference rate",
            "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam",
            "Algolab",
            "0",
            "2026-11-01T00:00:00Z",
        ])

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path, collectors_config_path=coll_cfg
    )
    assert quality["publication_status"] == "validated_with_optional_source_missing"
    assert "status" not in quality["source_checks"]["usd_vnd"]
    assert quality["source_checks"]["usd_vnd"]["condition"] == "unusable"
    assert quality["optional_sources_missing"] == []
    assert quality["optional_sources_empty"] == []
    assert quality["optional_sources_unusable"] == [pipeline.FX_FILE]
    assert quality["optional_sources_stale"] == []
    assert quality["integrated"]["quarantined_rows"] == 1


# 6. FX chuyển từ optional sang required: pipeline phải kiểm tra bắt buộc.
def test_integration_case6_fx_required_fails_when_missing(tmp_path):
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: false\n", encoding="utf-8")

    with pytest.raises(pipeline.PipelineError, match="missing required raw source files"):
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg_path, collectors_config_path=coll_cfg
        )


# 7. Một collector bắt buộc thất bại: không được công bố dữ liệu như thành công.
def test_integration_case7_mandatory_collector_failure_skips_pipeline(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-06T00:00:00Z")
    # Mandatory Brent fails
    monkeypatch.setattr(
        runner,
        "_run_collector",
        lambda module, **kwargs: (-1, "Brent connection timeout")
        if module == "petrotwin.collectors.brent"
        else (1, None),
    )
    pipeline_called = False

    def mock_pipeline(*args, **kwargs):
        nonlocal pipeline_called
        pipeline_called = True
        return ("ok", None)

    monkeypatch.setattr(runner, "_run_pipeline", mock_pipeline)
    monkeypatch.setattr(runner.sys, "argv", ["runner"])

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 1
    assert not pipeline_called
    log = (tmp_path / "logs" / "collector_run.log").read_text(encoding="utf-8")
    assert '"overall_status": "failed"' in log
    assert '"pipeline_status": "skipped"' in log


# 8. Collector optional thất bại: ghi nhận partial đúng cách.
def test_integration_case8_optional_collector_failure_records_partial(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-06T00:00:00Z")
    # Optional FX fails, Brent and Domestic Fuel succeed
    monkeypatch.setattr(
        runner,
        "_run_collector",
        lambda module, **kwargs: (-1, "Algolab connection timed out")
        if module == "petrotwin.collectors.fx"
        else (1, None),
    )
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args, **kwargs: ("ok", None))
    monkeypatch.delenv("PETROTWIN_DB_HOST", raising=False)
    monkeypatch.setattr(runner.sys, "argv", ["runner"])

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 0
    log = (tmp_path / "logs" / "collector_run.log").read_text(encoding="utf-8")
    assert '"overall_status": "partial"' in log
    assert '"optional_collectors_failed": ["fx"]' in log
    assert '"pipeline_status": "ok"' in log


# =========================================================================
# FX Freshness Validation Tests (T14 Pipeline)
# =========================================================================

def _write_fx_csv(raw_dir: Path, obs_date: str = "2026-11-05") -> Path:
    fx_path = raw_dir / pipeline.FX_FILE
    with fx_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FX_FILE])
        w.writerow([
            obs_date,
            "USD",
            "VND",
            "25627",
            "VND per 1 USD",
            "SBV central reference rate",
            "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam",
            "Algolab",
            "25,627",
            f"{obs_date}T00:00:00Z",
        ])
    return fx_path


def test_fx_fresh_optional_is_integrated(tmp_path):
    from datetime import date
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    # Observation is 2026-11-04 (strictly prior to fuel event on 2026-11-05)
    _write_fx_csv(raw_dir, "2026-11-04")

    # Current date is 2026-11-06 (2 days old <= 7 max_age_days)
    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )

    assert quality["publication_status"] == "validated"
    assert quality["optional_sources_stale"] == []
    assert quality["optional_sources_missing"] == []
    assert quality["source_checks"]["usd_vnd"]["status"] == "validated"
    assert quality["source_checks"]["usd_vnd"]["age_days"] == 2
    assert quality["source_checks"]["usd_vnd"]["max_age_days"] == 7
    assert quality["source_checks"]["usd_vnd"]["latest_observation_date"] == "2026-11-04"
    assert quality["integrated"]["market_observation_rows"] == 2

    run_id = quality["run_id"]
    run_dir = processed_dir / "runs" / run_id
    market_text = (run_dir / "market_observation.csv").read_text(encoding="utf-8")
    assert "usd_vnd_sbv_reference" in market_text

    panel_text = (run_dir / "fuel_market_panel.csv").read_text(encoding="utf-8")
    assert "25627" in panel_text
    assert "2026-11-04" in panel_text


def test_fx_stale_optional_is_excluded_from_market_and_panel(tmp_path):
    from datetime import date
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    # Observation is 2026-10-20; reference date is 2026-11-06 (17 days old > 7)
    _write_fx_csv(raw_dir, "2026-10-20")

    quality = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path,
        collectors_config_path=coll_cfg,
        current_date=date(2026, 11, 6),
    )

    # Pipeline continues and publishes without stale FX
    assert quality["publication_status"] == "validated_with_optional_source_stale"
    assert quality["optional_sources_stale"] == [pipeline.FX_FILE]
    assert quality["optional_sources_missing"] == []
    assert quality["source_checks"]["usd_vnd"]["status"] == "optional_source_stale"
    assert quality["source_checks"]["usd_vnd"]["latest_observation_date"] == "2026-10-20"
    assert quality["source_checks"]["usd_vnd"]["age_days"] == 17
    assert quality["source_checks"]["usd_vnd"]["max_age_days"] == 7

    # Market rows must ONLY contain Brent (1 row), not stale FX
    assert quality["integrated"]["market_observation_rows"] == 1
    assert "usd_vnd_sbv_reference" not in quality["integrated"]["market_coverage_by_series"]

    # Verify artifacts on disk
    run_id = quality["run_id"]
    run_dir = processed_dir / "runs" / run_id
    market_text = (run_dir / "market_observation.csv").read_text(encoding="utf-8")
    assert "usd_vnd_sbv_reference" not in market_text

    # Verify panel does NOT integrate stale FX features
    with (run_dir / "fuel_market_panel.csv").open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        panel_rows = list(reader)
        assert len(panel_rows) == 1
        assert panel_rows[0]["fx_observation_date"] == ""
        assert panel_rows[0]["usd_vnd_rate"] == ""
        assert panel_rows[0]["fx_age_days"] == ""
        assert panel_rows[0]["fx_snapshot_id"] == ""
        # Brent is still present and integrated
        assert panel_rows[0]["brent_usd_per_barrel"] == "100.5"
        assert panel_rows[0]["brent_observation_date"] == "2026-11-01"


def test_fx_stale_required_fails_pipeline(tmp_path):
    from datetime import date
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    coll_cfg = tmp_path / "collectors.yaml"
    coll_cfg.write_text("fx:\n  optional: false\n  max_age_days: 7\n", encoding="utf-8")
    _write_fx_csv(raw_dir, "2026-10-20")

    with pytest.raises(pipeline.PipelineError, match="required FX source is stale") as exc_info:
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg_path,
            collectors_config_path=coll_cfg,
            current_date=date(2026, 11, 6),
        )
    assert "2026-10-20" in str(exc_info.value)
    assert "17 days old" in str(exc_info.value)
    assert "max_age_days=7" in str(exc_info.value)


def test_fx_custom_config_and_max_age_days(tmp_path):
    from datetime import date
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)

    # 1. Custom strict max_age_days = 3: 4-day-old observation is stale
    coll_cfg_strict = tmp_path / "collectors_strict.yaml"
    coll_cfg_strict.write_text("fx:\n  optional: true\n  max_age_days: 3\n", encoding="utf-8")
    _write_fx_csv(raw_dir, "2026-11-02")

    q_strict = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path,
        collectors_config_path=coll_cfg_strict,
        current_date=date(2026, 11, 6),
    )
    assert q_strict["source_checks"]["usd_vnd"]["status"] == "optional_source_stale"
    assert q_strict["source_checks"]["usd_vnd"]["max_age_days"] == 3
    assert q_strict["source_checks"]["usd_vnd"]["age_days"] == 4
    assert q_strict["publication_status"] == "validated_with_optional_source_stale"

    # 2. Custom lenient max_age_days = 14: 10-day-old observation is fresh
    coll_cfg_lenient = tmp_path / "collectors_lenient.yaml"
    coll_cfg_lenient.write_text("fx:\n  optional: true\n  max_age_days: 14\n", encoding="utf-8")
    _write_fx_csv(raw_dir, "2026-10-27")

    q_lenient = pipeline.run_pipeline(
        raw_dir, processed_dir, log_dir, cfg_path,
        collectors_config_path=coll_cfg_lenient,
        current_date=date(2026, 11, 6),
    )
    assert q_lenient["source_checks"]["usd_vnd"]["status"] == "validated"
    assert q_lenient["source_checks"]["usd_vnd"]["max_age_days"] == 14
    assert q_lenient["source_checks"]["usd_vnd"]["age_days"] == 10


def test_fx_future_date_rejected_for_optional_and_required(tmp_path):
    from datetime import date
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)

    # 1. Future-dated FX with optional: true
    coll_cfg_opt = tmp_path / "collectors_opt.yaml"
    coll_cfg_opt.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")
    _write_fx_csv(raw_dir, "2026-11-10")

    with pytest.raises(pipeline.PipelineError, match="FX observation date is in the future") as exc_opt:
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg_path,
            collectors_config_path=coll_cfg_opt,
            current_date=date(2026, 11, 6),
        )
    assert "2026-11-10" in str(exc_opt.value)
    assert "2026-11-06" in str(exc_opt.value)

    # 2. Future-dated FX with optional: false
    coll_cfg_req = tmp_path / "collectors_req.yaml"
    coll_cfg_req.write_text("fx:\n  optional: false\n  max_age_days: 7\n", encoding="utf-8")

    with pytest.raises(pipeline.PipelineError, match="FX observation date is in the future") as exc_req:
        pipeline.run_pipeline(
            raw_dir, processed_dir, log_dir, cfg_path,
            collectors_config_path=coll_cfg_req,
            current_date=date(2026, 11, 6),
        )
    assert "2026-11-10" in str(exc_req.value)
    assert "2026-11-06" in str(exc_req.value)


def test_run_id_changes_when_max_age_or_current_date_changes(tmp_path):
    from datetime import date
    raw_dir, processed_dir, log_dir, cfg_path = setup_minimal_environment(tmp_path)
    _write_fx_csv(raw_dir, "2026-11-04")

    coll_cfg_7 = tmp_path / "coll_7.yaml"
    coll_cfg_7.write_text("fx:\n  optional: true\n  max_age_days: 7\n", encoding="utf-8")

    coll_cfg_10 = tmp_path / "coll_10.yaml"
    coll_cfg_10.write_text("fx:\n  optional: true\n  max_age_days: 10\n", encoding="utf-8")

    # Base run: date=2026-11-06, max_age=7
    q1 = pipeline.run_pipeline(
        raw_dir, processed_dir / "p1", log_dir / "l1", cfg_path,
        collectors_config_path=coll_cfg_7,
        current_date=date(2026, 11, 6),
    )

    # Repeat exact same configuration: must produce identical run_id (reproducible)
    q1_repeat = pipeline.run_pipeline(
        raw_dir, processed_dir / "p1_rep", log_dir / "l1_rep", cfg_path,
        collectors_config_path=coll_cfg_7,
        current_date=date(2026, 11, 6),
    )
    assert q1["run_id"] == q1_repeat["run_id"]

    # Different current_date: must produce different run_id
    q2_diff_date = pipeline.run_pipeline(
        raw_dir, processed_dir / "p2", log_dir / "l2", cfg_path,
        collectors_config_path=coll_cfg_7,
        current_date=date(2026, 11, 7),
    )
    assert q1["run_id"] != q2_diff_date["run_id"]

    # Different max_age_days: must produce different run_id
    q3_diff_config = pipeline.run_pipeline(
        raw_dir, processed_dir / "p3", log_dir / "l3", cfg_path,
        collectors_config_path=coll_cfg_10,
        current_date=date(2026, 11, 6),
    )
    assert q1["run_id"] != q3_diff_config["run_id"]


def test_missing_price_change_values_counted_by_product(tmp_path):
    fuel_file = tmp_path / pipeline.FUEL_FILE
    with fuel_file.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE])
        # gasoline_ron95_iii: 2 rows (1 with missing change, 1 with change=0)
        w.writerow([
            "2026-11-01", "2026-11-01", "15:00", "Xăng RON 95-III", "22000", "22.000 đ",
            "", "", "Đồng/lít thực tế", "true", "https://pvoil.com.vn", "2026-11-01T08:00:00Z"
        ])
        w.writerow([
            "2026-11-05", "2026-11-05", "15:00", "Xăng RON 95-III", "22500", "22.500 đ",
            "500", "+500 đ", "Đồng/lít thực tế", "true", "https://pvoil.com.vn", "2026-11-05T08:00:00Z"
        ])
        # gasoline_e5_ron92_ii: 2 rows (both with missing change)
        w.writerow([
            "2026-11-01", "2026-11-01", "15:00", "Xăng E5 RON 92-II", "21000", "21.000 đ",
            "", "", "Đồng/lít thực tế", "true", "https://pvoil.com.vn", "2026-11-01T08:00:00Z"
        ])
        w.writerow([
            "2026-11-05", "2026-11-05", "15:00", "Xăng E5 RON 92-II", "21300", "21.300 đ",
            "", "", "Đồng/lít thực tế", "true", "https://pvoil.com.vn", "2026-11-05T08:00:00Z"
        ])
        # diesel_005s_ii: 1 row (change=0, not missing)
        w.writerow([
            "2026-11-05", "2026-11-05", "15:00", "Dầu DO 0,05S-II", "19000", "19.000 đ",
            "0", "0 đ", "Đồng/lít thực tế", "true", "https://pvoil.com.vn", "2026-11-05T08:00:00Z"
        ])

    file_hash = pipeline._sha256(fuel_file)
    output, quarantine, quality = pipeline._parse_fuel(
        fuel_file, file_hash, pipeline.EXPECTED_HEADERS[pipeline.FUEL_FILE]
    )

    assert len(output) == 5
    assert len(quarantine) == 0
    # Overall missing changes: 1 (RON95) + 2 (E5) + 0 (DO) = 3
    assert quality["missing_price_change_values"] == 3
    assert quality["missing_values"] == 3

    # Per-product missing count
    prods = quality["products"]
    assert prods["gasoline_ron95_iii"]["missing_price_change_values"] == 1
    assert prods["gasoline_e5_ron92_ii"]["missing_price_change_values"] == 2
    assert prods["diesel_005s_ii"]["missing_price_change_values"] == 0
