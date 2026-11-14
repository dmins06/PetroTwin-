from datetime import date
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest
import requests
import json
from petrotwin.collectors import domestic_fuel

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def make_valid_payload(
    price_date: str = "2026-11-12",
    is_stale: bool = False,
    blocked: bool = False,
    total_items: int = 4,
    cache_hit: bool = False,
    products: list[dict] | None = None,
    success: bool = True,
    status: str = "ok",
    source: str = "PVOil",
    source_url: str = "https://www.pvoil.com.vn",
    price_date_source: str = "pvoil-direct-http",
) -> dict:
    if products is None:
        products = [
            {"name": "Xăng E10 RON 95-III", "region1": None, "region2": None, "price": 19840, "unit": "VND/lít"},
            {"name": "Xăng E5 RON 92-II", "region1": None, "region2": None, "price": 19390, "unit": "VND/lít"},
            {"name": "DO 0,001S-V", "region1": None, "region2": None, "price": 28620, "unit": "VND/lít"},
            {"name": "DO 0,05S-II", "region1": None, "region2": None, "price": 26820, "unit": "VND/lít"},
        ]
    meta: dict = {
        "source": source,
        "sourceUrl": source_url,
        "scrapedAt": "2026-11-14T13:21:16.967Z",
        "priceDate": price_date,
        "priceDateDisplay": "12/11/2026",
        "priceDateSource": price_date_source,
        "priceAnnouncedAt": None,
        "blockedByProtection": blocked,
        "cacheHit": cache_hit,
        "cacheTtlRemainingSeconds": 2813,
        "isStale": is_stale,
        "totalItems": total_items,
    }
    return {
        "success": success,
        "status": status,
        "disclaimer": {"vi": "Dữ liệu tham khảo", "en": "Reference data"},
        "meta": meta,
        "data": products,
    }


# ---------------------------------------------------------------------------
# 1. Valid API Response & Provenance Permissiveness
# ---------------------------------------------------------------------------


def test_valid_api_response_parses_all_4_products():
    payload = make_valid_payload()
    price_date, records, meta = domestic_fuel._parse_api_payload(payload)

    assert price_date == date(2026, 11, 12)
    assert len(records) == 4
    names = {r["product_name"] for r in records}
    assert names == domestic_fuel.EXPECTED_PRODUCTS
    assert meta["source"] == "PVOil"


def test_price_date_source_other_than_direct_http_is_accepted():
    for variant in ["fallback-provider", "web-cache", "custom-aggregator", "unspecified"]:
        payload = make_valid_payload(price_date_source=variant)
        price_date, records, meta = domestic_fuel._parse_api_payload(payload)
        assert price_date == date(2026, 11, 12)
        assert len(records) == 4
        assert meta["priceDateSource"] == variant


def test_source_url_not_restricted_to_pvoil_domain():
    payload = make_valid_payload(source_url="https://intermediary.example.com/prices")
    price_date, records, meta = domestic_fuel._parse_api_payload(payload)
    assert price_date == date(2026, 11, 12)
    assert meta["sourceUrl"] == "https://intermediary.example.com/prices"


def test_product_name_mapping():
    assert domestic_fuel.normalize_api_product_name("Xăng E10 RON 95-III") == "Xăng E10 RON 95-III"
    assert domestic_fuel.normalize_api_product_name("Xăng E5 RON 92-II") == "Xăng E5 RON 92-II"
    assert domestic_fuel.normalize_api_product_name("DO 0,001S-V") == "Dầu DO 0,001S-V"
    assert domestic_fuel.normalize_api_product_name("Dầu DO 0,001S-V") == "Dầu DO 0,001S-V"
    assert domestic_fuel.normalize_api_product_name("DO 0,05S-II") == "Dầu DO 0,05S-II"
    assert domestic_fuel.normalize_api_product_name("Dầu DO 0,05S-II") == "Dầu DO 0,05S-II"
    assert domestic_fuel.normalize_api_product_name("Unknown Fuel") is None


# ---------------------------------------------------------------------------
# 2. Timezone Asia/Ho_Chi_Minh Validation
# ---------------------------------------------------------------------------


def test_price_date_in_future_relative_to_vietnam_time_fails(monkeypatch):
    monkeypatch.setattr(domestic_fuel, "_today_vn", lambda: date(2026, 11, 14))

    # Same day or past is valid
    payload_valid = make_valid_payload(price_date="2026-11-14")
    p_date, _records, _meta = domestic_fuel._parse_api_payload(payload_valid)
    assert p_date == date(2026, 11, 14)

    # Future date in VN time is rejected
    payload_future = make_valid_payload(price_date="2026-11-15")
    with pytest.raises(domestic_fuel.DomesticFuelDateError, match="in the future in Vietnam timezone"):
        domestic_fuel._parse_api_payload(payload_future)


def test_vietnam_date_differs_from_utc_date(monkeypatch):
    fixed_vn_date = date(2026, 11, 15)
    monkeypatch.setattr(domestic_fuel, "_today_vn", lambda: fixed_vn_date)

    payload = make_valid_payload(price_date="2026-11-15")
    p_date, _records, _meta = domestic_fuel._parse_api_payload(payload)
    assert p_date == fixed_vn_date


# ---------------------------------------------------------------------------
# 3. Metadata Validation: isStale & blockedByProtection Boolean Checks
# ---------------------------------------------------------------------------


def test_is_stale_boolean_validation():
    # 1. Real boolean False -> accepted
    payload_ok = make_valid_payload(is_stale=False)
    _d, _r, meta = domestic_fuel._parse_api_payload(payload_ok)
    assert meta["isStale"] is False

    # 2. Real boolean True -> rejected with DomesticFuelAPIError
    payload_stale = make_valid_payload(is_stale=True)
    with pytest.raises(domestic_fuel.DomesticFuelAPIError, match="stale fuel price data"):
        domestic_fuel._parse_api_payload(payload_stale)

    # 3. String "true", "false", null, int, missing -> raise DomesticFuelSchemaError
    bad_stale_values = ["true", "false", None, 1, 0, 1.0]
    for bad_val in bad_stale_values:
        p = make_valid_payload()
        p["meta"]["isStale"] = bad_val
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="meta.isStale must be a boolean"):
            domestic_fuel._parse_api_payload(p)

    # Missing field
    p_missing = make_valid_payload()
    del p_missing["meta"]["isStale"]
    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="Missing 'isStale' in meta"):
        domestic_fuel._parse_api_payload(p_missing)


def test_blocked_by_protection_boolean_validation():
    # 1. Real boolean False -> accepted
    payload_ok = make_valid_payload(blocked=False)
    _d, _r, meta = domestic_fuel._parse_api_payload(payload_ok)
    assert meta["blockedByProtection"] is False

    # 2. Real boolean True -> rejected with DomesticFuelAPIError
    payload_blocked = make_valid_payload(blocked=True)
    with pytest.raises(domestic_fuel.DomesticFuelAPIError, match="blocked by provider protection"):
        domestic_fuel._parse_api_payload(payload_blocked)

    # 3. String "true", "false", null, int, missing -> raise DomesticFuelSchemaError
    bad_blocked_values = ["true", "false", None, 1, 0, 1.0]
    for bad_val in bad_blocked_values:
        p = make_valid_payload()
        p["meta"]["blockedByProtection"] = bad_val
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="meta.blockedByProtection must be a boolean"):
            domestic_fuel._parse_api_payload(p)

    # Missing field
    p_missing = make_valid_payload()
    del p_missing["meta"]["blockedByProtection"]
    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="Missing 'blockedByProtection' in meta"):
        domestic_fuel._parse_api_payload(p_missing)


# ---------------------------------------------------------------------------
# 4. Numeric Validation (price, region1, region2, change)
# ---------------------------------------------------------------------------


def test_price_numeric_rejections():
    bad_prices = [True, False, 19840.5, float("nan"), float("inf"), 0, -100, "19840"]
    for bad_val in bad_prices:
        products = [
            {"name": "Xăng E10 RON 95-III", "price": bad_val},
            {"name": "Xăng E5 RON 92-II", "price": 19390},
            {"name": "DO 0,001S-V", "price": 28620},
            {"name": "DO 0,05S-II", "price": 26820},
        ]
        payload = make_valid_payload(products=products)
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError):
            domestic_fuel._parse_api_payload(payload)


def test_regional_prices_null_and_validation():
    payload = make_valid_payload()
    _date, records, _meta = domestic_fuel._parse_api_payload(payload)
    for r in records:
        assert r["region1"] is None
        assert r["region2"] is None
        assert r["region1"] != 0
        assert r["region2"] != 0

    products_with_region = [
        {"name": "Xăng E10 RON 95-III", "region1": 20000, "region2": 20400, "price": 19840},
        {"name": "Xăng E5 RON 92-II", "region1": 19500, "region2": 19900, "price": 19390},
        {"name": "DO 0,001S-V", "region1": 29000, "region2": 29500, "price": 28620},
        {"name": "DO 0,05S-II", "region1": 27000, "region2": 27500, "price": 26820},
    ]
    p_with_reg = make_valid_payload(products=products_with_region)
    _d, recs, _m = domestic_fuel._parse_api_payload(p_with_reg)
    assert recs[0]["region1"] == 20000
    assert recs[0]["region2"] == 20400

    bad_region_vals = [True, False, 20000.5, float("nan"), float("inf"), 0, -500]
    for bad_reg in bad_region_vals:
        bad_reg_prods = [
            {"name": "Xăng E10 RON 95-III", "region1": bad_reg, "price": 19840},
            {"name": "Xăng E5 RON 92-II", "price": 19390},
            {"name": "DO 0,001S-V", "price": 28620},
            {"name": "DO 0,05S-II", "price": 26820},
        ]
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError):
            domestic_fuel._parse_api_payload(make_valid_payload(products=bad_reg_prods))


def test_change_validation():
    valid_changes = [150, -350, 0, "+200", "-50"]
    for ch in valid_changes:
        prods = [
            {"name": "Xăng E10 RON 95-III", "price": 19840, "change": ch},
            {"name": "Xăng E5 RON 92-II", "price": 19390},
            {"name": "DO 0,001S-V", "price": 28620},
            {"name": "DO 0,05S-II", "price": 26820},
        ]
        _d, recs, _m = domestic_fuel._parse_api_payload(make_valid_payload(products=prods))
        assert recs[0]["change"] is not None

    invalid_changes = [True, False, 150.5, float("nan"), float("inf"), "abc"]
    for bad_ch in invalid_changes:
        bad_prods = [
            {"name": "Xăng E10 RON 95-III", "price": 19840, "change": bad_ch},
            {"name": "Xăng E5 RON 92-II", "price": 19390},
            {"name": "DO 0,001S-V", "price": 28620},
            {"name": "DO 0,05S-II", "price": 26820},
        ]
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError):
            domestic_fuel._parse_api_payload(make_valid_payload(products=bad_prods))


def test_validate_api_change_direct():
    """Verify _validate_api_change strictly adheres to ^[+-]?\\d+$ and integer types."""
    # 1. Valid string formats
    assert domestic_fuel._validate_api_change("+150", "fuel") == 150
    assert domestic_fuel._validate_api_change("-350", "fuel") == -350
    assert domestic_fuel._validate_api_change("0", "fuel") == 0
    assert domestic_fuel._validate_api_change("+0", "fuel") == 0
    assert domestic_fuel._validate_api_change("-0", "fuel") == 0
    assert domestic_fuel._validate_api_change("200", "fuel") == 200

    # 2. Valid integer types
    assert domestic_fuel._validate_api_change(150, "fuel") == 150
    assert domestic_fuel._validate_api_change(-350, "fuel") == -350
    assert domestic_fuel._validate_api_change(0, "fuel") == 0

    # 3. None is preserved as None
    assert domestic_fuel._validate_api_change(None, "fuel") is None

    # 4. Malformed string formats rejected
    malformed_strings = [
        "++10",
        "+-10",
        "--10",
        "-+10",
        "+",
        "-",
        "",
        "   ",
        "150.5",
        "+150.0",
        "abc",
        "123a",
        "1 2",
        "150 ",
    ]
    for bad_str in malformed_strings:
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError):
            domestic_fuel._validate_api_change(bad_str, "fuel")

    # 5. Invalid types rejected
    bad_types = [True, False, [150], {"val": 10}, 150.5, float("nan"), float("inf")]
    for bad_val in bad_types:
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError):
            domestic_fuel._validate_api_change(bad_val, "fuel")


# ---------------------------------------------------------------------------
# 5. Historical Price Logic (Out-of-Order CSV Handling)
# ---------------------------------------------------------------------------


def test_last_prices_by_product_handles_unsorted_csv(tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    unsorted_content = (
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,"
        "price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-10-22,2026-10-22,15:00,Xăng E10 RON 95-III,24860,24.860 đ,-1810,-1810,Đồng/lít thực tế,true,https://pvoil.com.vn,2026-10-22T15:00:00Z\n"
        "2026-10-08,2026-10-08,15:00,Xăng E10 RON 95-III,28250,28.250 đ,1070,+1070,Đồng/lít thực tế,true,https://pvoil.com.vn,2026-10-08T15:00:00Z\n"
    )
    csv_file.write_text(unsorted_content, encoding="utf-8")

    prices = domestic_fuel._last_prices_by_product(csv_file)
    assert prices["Xăng E10 RON 95-III"] == 24860


def test_last_date_in_csv_handles_unsorted_csv(tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    unsorted_content = (
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,"
        "price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-10-22,2026-10-22,15:00,Xăng E10 RON 95-III,24860,24.860 đ,-1810,-1810,Đồng/lít thực tế,true,https://pvoil.com.vn,2026-10-22T15:00:00Z\n"
        "2026-10-15,2026-10-15,15:00,Xăng E10 RON 95-III,27020,27.020 đ,-1230,-1230,Đồng/lít thực tế,true,https://pvoil.com.vn,2026-10-15T15:00:00Z\n"
    )
    csv_file.write_text(unsorted_content, encoding="utf-8")
    assert domestic_fuel._last_date_in_csv(csv_file) == date(2026, 10, 22)


# ---------------------------------------------------------------------------
# 6. CSV Integrity & Header Validation Tests
# ---------------------------------------------------------------------------


def test_append_to_nonexistent_file_writes_header_and_rows(tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    assert not csv_file.exists()

    rows = [
        {col: f"val_{col}" for col in domestic_fuel.COLUMNS}
    ]
    domestic_fuel._append_to_csv(csv_file, rows)

    assert csv_file.exists()
    lines = csv_file.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == ",".join(domestic_fuel.COLUMNS)
    assert len(lines) == 2


def test_append_to_empty_file_writes_header_and_rows(tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    csv_file.write_text("", encoding="utf-8")  # 0 bytes

    rows = [
        {col: f"val_{col}" for col in domestic_fuel.COLUMNS}
    ]
    domestic_fuel._append_to_csv(csv_file, rows)

    lines = csv_file.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == ",".join(domestic_fuel.COLUMNS)
    assert len(lines) == 2


def test_append_to_whitespace_only_file_writes_header_and_rows(tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    csv_file.write_text("   \n\n   \n", encoding="utf-8")

    rows = [
        {col: f"val_{col}" for col in domestic_fuel.COLUMNS}
    ]
    domestic_fuel._append_to_csv(csv_file, rows)

    lines = csv_file.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == ",".join(domestic_fuel.COLUMNS)
    assert len(lines) == 2


def test_append_to_valid_header_preserves_content_and_appends(tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    header_line = ",".join(domestic_fuel.COLUMNS)
    first_row = ",".join(["test"] * len(domestic_fuel.COLUMNS))
    csv_file.write_text(f"{header_line}\n{first_row}\n", encoding="utf-8")

    rows = [
        {col: f"new_{col}" for col in domestic_fuel.COLUMNS}
    ]
    domestic_fuel._append_to_csv(csv_file, rows)

    lines = csv_file.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == header_line
    assert lines[1] == first_row
    assert len(lines) == 3


def test_append_to_invalid_header_fails_closed_and_leaves_file_untouched(tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    corrupt_content = "wrong,header,date,price\n1,2,3,4\n"
    csv_file.write_text(corrupt_content, encoding="utf-8")

    rows = [
        {col: f"val_{col}" for col in domestic_fuel.COLUMNS}
    ]
    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="CSV header .* does not match expected COLUMNS"):
        domestic_fuel._append_to_csv(csv_file, rows)

    # File must be completely untouched
    assert csv_file.read_text(encoding="utf-8") == corrupt_content


def test_run_with_corrupt_csv_header_fails_closed(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    corrupt_content = "bad,header\n1,2\n"
    csv_file.write_text(corrupt_content, encoding="utf-8")

    fetch_mock = MagicMock()
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)
    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", fetch_mock)

    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()
    assert exc.value.code == 1
    # Pre-flight check must fail before calling the API
    assert fetch_mock.call_count == 0
    assert csv_file.read_text(encoding="utf-8") == corrupt_content


# ---------------------------------------------------------------------------
# 7. Config Validation Tests
# ---------------------------------------------------------------------------


def test_collector_config_handles_non_dict_yaml_root(monkeypatch, tmp_path):
    cfg_file = tmp_path / "collectors.yaml"
    cfg_file.write_text("- item1\n- item2\n", encoding="utf-8")  # YAML list

    monkeypatch.setattr(domestic_fuel, "_repo_root", lambda: tmp_path)
    # create fake configs dir
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "collectors.yaml").write_text("- item1\n- item2\n", encoding="utf-8")

    assert domestic_fuel._collector_config() == {}
    assert domestic_fuel._api_endpoint() == domestic_fuel.DEFAULT_API_ENDPOINT
    assert domestic_fuel._request_timeout() == domestic_fuel.DEFAULT_REQUEST_TIMEOUT
    assert domestic_fuel._retry_settings() == (domestic_fuel.DEFAULT_RETRY_COUNT, domestic_fuel.DEFAULT_RETRY_DELAY)


def test_collector_config_handles_non_dict_section(monkeypatch, tmp_path):
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "collectors.yaml").write_text("domestic_fuel: 'not-a-dict'\n", encoding="utf-8")

    monkeypatch.setattr(domestic_fuel, "_repo_root", lambda: tmp_path)

    assert domestic_fuel._collector_config() == {}
    assert domestic_fuel._api_endpoint() == domestic_fuel.DEFAULT_API_ENDPOINT
    assert domestic_fuel._request_timeout() == domestic_fuel.DEFAULT_REQUEST_TIMEOUT
    assert domestic_fuel._retry_settings() == (domestic_fuel.DEFAULT_RETRY_COUNT, domestic_fuel.DEFAULT_RETRY_DELAY)


def test_collector_config_handles_empty_or_scalar_file(monkeypatch, tmp_path):
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "collectors.yaml").write_text("just-a-string\n", encoding="utf-8")

    monkeypatch.setattr(domestic_fuel, "_repo_root", lambda: tmp_path)

    assert domestic_fuel._collector_config() == {}


def test_is_valid_https_url():
    assert domestic_fuel._is_valid_https_url("https://xangdau.nhatkhanhtech.com/api/fuel-prices/pvoil") is True
    assert domestic_fuel._is_valid_https_url("https://example.com/api") is True
    assert domestic_fuel._is_valid_https_url("https://pvoil.com.vn") is True

    # Insecure or invalid schemes
    assert domestic_fuel._is_valid_https_url("http://xangdau.nhatkhanhtech.com/api") is False
    assert domestic_fuel._is_valid_https_url("ftp://example.com") is False
    assert domestic_fuel._is_valid_https_url("https://") is False
    assert domestic_fuel._is_valid_https_url("https:///") is False
    assert domestic_fuel._is_valid_https_url("") is False
    assert domestic_fuel._is_valid_https_url("   ") is False
    assert domestic_fuel._is_valid_https_url(None) is False
    assert domestic_fuel._is_valid_https_url(123) is False
    assert domestic_fuel._is_valid_https_url(True) is False
    assert domestic_fuel._is_valid_https_url("not a url") is False


def test_api_endpoint_config_validation(monkeypatch):
    # Valid custom HTTPS endpoint
    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"api_endpoint": "https://custom.endpoint.vn/api"})
    assert domestic_fuel._api_endpoint() == "https://custom.endpoint.vn/api"

    # Insecure HTTP endpoint falls back to default
    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"api_endpoint": "http://insecure.endpoint.vn/api"})
    assert domestic_fuel._api_endpoint() == domestic_fuel.DEFAULT_API_ENDPOINT

    # Malformed endpoint falls back to default
    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"api_endpoint": "invalid-url"})
    assert domestic_fuel._api_endpoint() == domestic_fuel.DEFAULT_API_ENDPOINT

    # Non-string endpoint falls back to default
    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"api_endpoint": 12345})
    assert domestic_fuel._api_endpoint() == domestic_fuel.DEFAULT_API_ENDPOINT


def test_timeout_and_retry_config_validation(monkeypatch):
    # 1. request_timeout_seconds
    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"request_timeout_seconds": 45.0})
    assert domestic_fuel._request_timeout() == 45.0

    bad_timeouts = [-5, 0, float("nan"), float("inf"), True, False, "30", None]
    for bad_t in bad_timeouts:
        monkeypatch.setattr(domestic_fuel, "_collector_config", lambda bad=bad_t: {"request_timeout_seconds": bad})
        assert domestic_fuel._request_timeout() == domestic_fuel.DEFAULT_REQUEST_TIMEOUT

    # 2. retry_count
    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"retry_count": 0})
    assert domestic_fuel._retry_settings()[0] == 0

    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"retry_count": 5})
    assert domestic_fuel._retry_settings()[0] == 5

    bad_counts = [-1, True, False, 2.5, "2"]
    for bad_c in bad_counts:
        monkeypatch.setattr(domestic_fuel, "_collector_config", lambda bad=bad_c: {"retry_count": bad})
        assert domestic_fuel._retry_settings()[0] == domestic_fuel.DEFAULT_RETRY_COUNT

    # 3. retry_delay_seconds
    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"retry_delay_seconds": 0.0})
    assert domestic_fuel._retry_settings()[1] == 0.0

    monkeypatch.setattr(domestic_fuel, "_collector_config", lambda: {"retry_delay_seconds": 5.5})
    assert domestic_fuel._retry_settings()[1] == 5.5

    bad_delays = [-1.0, float("nan"), float("inf"), True, False, "10"]
    for bad_d in bad_delays:
        monkeypatch.setattr(domestic_fuel, "_collector_config", lambda bad=bad_d: {"retry_delay_seconds": bad})
        assert domestic_fuel._retry_settings()[1] == domestic_fuel.DEFAULT_RETRY_DELAY


# ---------------------------------------------------------------------------
# 8. Schema & Product Consistency Tests
# ---------------------------------------------------------------------------


def test_missing_product_fails():
    products = [
        {"name": "Xăng E10 RON 95-III", "price": 19840},
        {"name": "Xăng E5 RON 92-II", "price": 19390},
        {"name": "DO 0,001S-V", "price": 28620},
    ]
    payload = make_valid_payload(total_items=3, products=products)
    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="Expected exactly 4 products"):
        domestic_fuel._parse_api_payload(payload)


def test_duplicate_product_fails():
    products = [
        {"name": "Xăng E10 RON 95-III", "price": 19840},
        {"name": "Xăng E5 RON 92-II", "price": 19390},
        {"name": "DO 0,05S-II", "price": 26820},
        {"name": "Dầu DO 0,05S-II", "price": 26820},
    ]
    payload = make_valid_payload(products=products)
    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="Duplicate product"):
        domestic_fuel._parse_api_payload(payload)


def test_unknown_product_fails():
    products = [
        {"name": "Xăng E10 RON 95-III", "price": 19840},
        {"name": "Xăng E5 RON 92-II", "price": 19390},
        {"name": "DO 0,001S-V", "price": 28620},
        {"name": "Xăng Hàng Không Jet A-1", "price": 25000},
    ]
    payload = make_valid_payload(products=products)
    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="Unknown or unexpected product"):
        domestic_fuel._parse_api_payload(payload)


def test_total_items_mismatch_fails():
    payload = make_valid_payload(total_items=10)
    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="totalItems .* does not match len"):
        domestic_fuel._parse_api_payload(payload)


def test_total_items_strict_integer_validation():
    """Verify meta.totalItems rejects floats, booleans, strings, None, and requires real int."""
    bad_total_items = [4.0, True, False, "4", None, [4]]
    for bad_val in bad_total_items:
        payload = make_valid_payload()
        payload["meta"]["totalItems"] = bad_val
        with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="meta.totalItems must be an integer"):
            domestic_fuel._parse_api_payload(payload)

    # Valid int 4 succeeds
    valid_payload = make_valid_payload(total_items=4)
    price_date, records, meta = domestic_fuel._parse_api_payload(valid_payload)
    assert meta["totalItems"] == 4
    assert len(records) == 4


def test_api_success_false_fails():
    payload = make_valid_payload(success=False)
    with pytest.raises(domestic_fuel.DomesticFuelAPIError, match="success=False"):
        domestic_fuel._parse_api_payload(payload)


def test_api_status_not_ok_fails():
    payload = make_valid_payload(status="degraded")
    with pytest.raises(domestic_fuel.DomesticFuelAPIError, match="Unexpected API status: 'degraded'"):
        domestic_fuel._parse_api_payload(payload)


def test_cache_hit_response_succeeds():
    payload = make_valid_payload(cache_hit=True)
    _date, records, meta = domestic_fuel._parse_api_payload(payload)
    assert meta["cacheHit"] is True
    assert len(records) == 4


# ---------------------------------------------------------------------------
# 9. Network Errors, Timeouts, HTTP 4xx/5xx & Invalid JSON
# ---------------------------------------------------------------------------


def test_http_403_fails_closed(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)

    def mock_fetch():
        raise domestic_fuel.DomesticFuelFetchError("HTTP 403 Forbidden", status_code=403, deterministic=True)

    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", mock_fetch)
    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()
    assert exc.value.code == 1
    assert not csv_file.exists()


def test_http_500_server_error_fails(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)

    def mock_fetch():
        raise domestic_fuel.DomesticFuelFetchError("HTTP 500 Internal Server Error", status_code=500, deterministic=False)

    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", mock_fetch)
    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()
    assert exc.value.code == 1


def test_connection_timeout_handling(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)

    def mock_fetch():
        raise domestic_fuel.DomesticFuelFetchError("ConnectTimeout connecting to API", deterministic=False)

    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", mock_fetch)
    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()
    assert exc.value.code == 1


def test_invalid_json_handling():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Invalid JSON string")

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with pytest.raises(domestic_fuel.DomesticFuelSchemaError, match="Failed to decode JSON"):
        domestic_fuel._fetch_api_with_retries(session=mock_session)


# ---------------------------------------------------------------------------
# 10. CSV Append, Idempotency & Fail-Closed Integrity
# ---------------------------------------------------------------------------


def test_historical_csv_preserved_and_appended(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    existing_content = (
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,"
        "price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-10-22,2026-10-22,15:00,Xăng E10 RON 95-III,24860,24.860 đ,-1810,-1810,"
        "Đồng/lít thực tế,true,https://pvoil.com.vn/tin-gia-xang-dau,2026-10-23T06:02:10Z\n"
    )
    csv_file.write_text(existing_content, encoding="utf-8")

    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)
    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", lambda: make_valid_payload("2026-11-12"))

    added = domestic_fuel.run()
    assert added == 4

    lines = csv_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 + 1 + 4  # header + 1 old row + 4 new rows
    assert "2026-10-22,2026-10-22" in lines[1]
    assert "2026-11-12" in lines[2]


def test_idempotent_on_same_effective_date(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    existing_content = (
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,"
        "price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-11-12,2026-11-12,15:00,Xăng E10 RON 95-III,19840,19.840 đ,0,0 đ,"
        "Đồng/lít thực tế,true,https://pvoil.com.vn/tin-gia-xang-dau,2026-11-12T15:00:00Z\n"
    )
    csv_file.write_text(existing_content, encoding="utf-8")

    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)
    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", lambda: make_valid_payload("2026-11-12"))

    added = domestic_fuel.run()
    assert added == 0
    assert csv_file.read_text(encoding="utf-8") == existing_content


def test_api_error_does_not_modify_csv(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    existing_content = (
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,"
        "price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-10-22,2026-10-22,15:00,Xăng E10 RON 95-III,24860,24.860 đ,-1810,-1810,"
        "Đồng/lít thực tế,true,https://pvoil.com.vn/tin-gia-xang-dau,2026-10-23T06:02:10Z\n"
    )
    csv_file.write_text(existing_content, encoding="utf-8")

    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)

    def mock_fetch():
        raise domestic_fuel.DomesticFuelFetchError("Connection dropped by remote host", deterministic=False)

    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", mock_fetch)

    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()
    assert exc.value.code == 1
    assert csv_file.read_text(encoding="utf-8") == existing_content


def test_invalid_product_does_not_write_partial_data(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)

    invalid_products = [
        {"name": "Xăng E10 RON 95-III", "price": 19840},
        {"name": "Xăng E5 RON 92-II", "price": 19390},
        {"name": "DO 0,001S-V", "price": 28620},
        {"name": "DO 0,05S-II", "price": 0},  # Invalid price = 0
    ]
    payload = make_valid_payload(products=invalid_products)
    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", lambda: payload)

    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()
    assert exc.value.code == 1
    assert not csv_file.exists()

# ---------------------------------------------------------------------------
# 11. Read-only API Check
# ---------------------------------------------------------------------------


def test_check_only_validates_api_without_file_access(monkeypatch, capsys):
    payload = make_valid_payload("2026-11-12")
    monkeypatch.setattr(
        domestic_fuel,
        "_fetch_api_with_retries",
        lambda: payload,
    )

    def unexpected_file_access(*args, **kwargs):
        pytest.fail("check_only must not access local data files")

    for name in (
        "_csv_path",
        "_check_csv_header",
        "_last_date_in_csv",
        "_last_prices_by_product",
        "_build_csv_rows",
        "_append_to_csv",
    ):
        if hasattr(domestic_fuel, name):
            monkeypatch.setattr(domestic_fuel, name, unexpected_file_access)

    monkeypatch.setattr(
        domestic_fuel,
        "file_lock",
        unexpected_file_access,
    )

    result = domestic_fuel.check_only()
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["status"] == "success"
    assert output["mode"] == "check-only"
    assert output["root_source"] == "PVOIL"
    assert output["products_found"] == 4
    assert output["price_date"] == "2026-11-12"


def test_check_only_api_failure_exits_without_file_access(monkeypatch):
    def mock_fetch():
        raise domestic_fuel.DomesticFuelFetchError(
            "Connection failed",
            deterministic=False,
        )

    monkeypatch.setattr(
        domestic_fuel,
        "_fetch_api_with_retries",
        mock_fetch,
    )

    def unexpected_file_access(*args, **kwargs):
        pytest.fail("check_only must not access local data files")

    monkeypatch.setattr(domestic_fuel, "_csv_path", unexpected_file_access)
    monkeypatch.setattr(domestic_fuel, "file_lock", unexpected_file_access)

    with pytest.raises(SystemExit) as exc:
        domestic_fuel.check_only()

    assert exc.value.code == 1