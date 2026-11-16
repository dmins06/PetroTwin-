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


# ---------------------------------------------------------------------------
# 12. HTTP 403 & 5xx Diagnostics & Hardened Error Reporting Tests
# ---------------------------------------------------------------------------


def test_http_403_captures_allowlisted_headers_and_logs_event(monkeypatch):
    """Verify HTTP 403 captures all allowlisted headers, preview, and emits http_403_diagnostics."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {
        "Server": "cloudflare",
        "Content-Type": "text/html; charset=UTF-8",
        "CF-Ray": "8e3641bc9a120456-NRT",
        "Via": "1.1 vegur",
        "Retry-After": "120",
        "X-Request-ID": "req-987654",
        "Set-Cookie": "cf_clearance=secret_cookie_val",
        "Authorization": "Bearer secret_bearer_token",
    }
    mock_resp.text = "<html><head><title>Just a moment...</title></head><body>Cloudflare challenge</body></html>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    logged_events: list[dict] = []

    def mock_log(event: str, **kwargs):
        logged_events.append({"event": event, **kwargs})

    monkeypatch.setattr(domestic_fuel, "_log", mock_log)

    with pytest.raises(domestic_fuel.DomesticFuelFetchError) as exc_info:
        domestic_fuel._fetch_api_with_retries(session=mock_session)

    exc = exc_info.value
    assert exc.status_code == 403
    assert exc.deterministic is True

    # 1. Allowlisted headers captured
    diag = exc.response_diagnostics
    assert diag["status_code"] == 403
    headers_dict = diag["headers"]
    assert headers_dict["Server"] == "cloudflare"
    assert headers_dict["Content-Type"] == "text/html; charset=UTF-8"
    assert headers_dict["CF-Ray"] == "8e3641bc9a120456-NRT"
    assert headers_dict["Via"] == "1.1 vegur"
    assert headers_dict["Retry-After"] == "120"
    assert headers_dict["X-Request-ID"] == "req-987654"

    # 2. Body preview captures sanitized title and never raw HTML
    assert "HTML Title: Just a moment..." in diag["body_preview"]
    assert "<html" not in diag["body_preview"]
    assert "<body" not in diag["body_preview"]

    # 3. Log event http_403_diagnostics emitted
    diag_events = [e for e in logged_events if e["event"] == "http_403_diagnostics"]
    assert len(diag_events) == 1
    assert diag_events[0]["status_code"] == 403
    assert diag_events[0]["headers"]["CF-Ray"] == "8e3641bc9a120456-NRT"
    assert diag_events[0]["headers"]["Server"] == "cloudflare"


def test_diagnostics_contain_no_duplicate_flattened_header_fields():
    """Verify diagnostics structure contains only status_code, headers, and body_preview."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {
        "Server": "cloudflare",
        "Content-Type": "text/html",
        "CF-Ray": "8e3641bc9a120456-NRT",
        "Via": "1.1 vegur",
        "Retry-After": "60",
        "X-Request-ID": "req-123",
    }
    mock_resp.text = "<title>Access Denied</title>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    diag = domestic_fuel._extract_safe_response_diagnostics(mock_resp)

    # Must contain ONLY status_code, headers, body_preview
    assert set(diag.keys()) == {"status_code", "headers", "body_preview"}

    # Must NOT contain duplicate flattened fields
    for duplicate_field in ["server", "cf_ray", "content_type", "via", "retry_after", "x_request_id"]:
        assert duplicate_field not in diag


def test_html_response_body_does_not_expose_raw_html():
    """Verify HTML response bodies never expose raw HTML markup, tags, or scripts."""
    raw_html = (
        "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title>"
        "<script>var secret = 'do_not_leak_me';</script></head>"
        "<body><div class='cf-error-details'><h1>Access denied</h1><p>IP: 1.2.3.4</p></div></body></html>"
    )
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html; charset=UTF-8"}
    mock_resp.text = raw_html
    mock_resp.content = raw_html.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "<!DOCTYPE" not in preview
    assert "<script" not in preview
    assert "<div" not in preview
    assert "<html" not in preview
    assert "<body" not in preview
    assert "do_not_leak_me" not in preview
    assert preview == "HTML Title: Attention Required! | Cloudflare"


def test_html_challenge_title_extraction_safe_or_empty():
    """Verify HTML challenge title extraction is safe, handles inner tags, and falls back gracefully."""
    # Case A: Title with inner tags
    mock_resp_a = MagicMock()
    mock_resp_a.headers = {"Content-Type": "text/html"}
    mock_resp_a.text = "<html><head><title>  Access <b>Denied</b> - <i>Security Check</i>  </title></head></html>"
    assert domestic_fuel._sanitize_response_body(mock_resp_a) == "HTML Title: Access Denied - Security Check"

    # Case B: No title tag, but recognizable Cloudflare challenge
    mock_resp_b = MagicMock()
    mock_resp_b.headers = {"Content-Type": "text/html"}
    mock_resp_b.text = "<html><body><h1>Cloudflare Ray ID: 12345</h1>Just a moment...</body></html>"
    assert domestic_fuel._sanitize_response_body(mock_resp_b) == "HTML challenge page detected"

    # Case C: No title tag and no recognizable challenge -> returns empty string
    mock_resp_c = MagicMock()
    mock_resp_c.headers = {"Content-Type": "text/html"}
    mock_resp_c.text = "<html><body><h1>Unknown Error</h1></body></html>"
    assert domestic_fuel._sanitize_response_body(mock_resp_c) == ""


def test_bearer_token_with_special_base64_characters():
    """Verify Bearer token credentials containing +, /, and = are thoroughly redacted."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.text = '{"error": "Unauthorized", "auth": "Bearer eyJhbGciOi+NTY4/abc+def/123=="}'
    mock_resp.content = mock_resp.text.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "eyJhbGciOi" not in preview
    assert "+NTY4" not in preview
    assert "/abc+def/123==" not in preview
    assert 'Bearer [REDACTED]' in preview


def test_credentials_and_secrets_redaction():
    """Verify access_token, refresh_token, client_secret, X-Api-Key, and passwords are redacted."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.text = (
        '{"access_token": "secret_access_token_12345", '
        '"refresh_token": "secret_refresh_token_67890", '
        '"client_secret": "my_client_secret_xyz", '
        '"X-Api-Key": "api_key_value_99999", '
        '"password": "super_secret_password_abc"}'
    )
    mock_resp.content = mock_resp.text.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "secret_access_token_12345" not in preview
    assert "secret_refresh_token_67890" not in preview
    assert "my_client_secret_xyz" not in preview
    assert "api_key_value_99999" not in preview
    assert "super_secret_password_abc" not in preview
    assert "[REDACTED]" in preview


def test_credentials_embedded_in_url_query_parameters():
    """Verify credentials embedded in URL query parameters are redacted."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.text = (
        '{"upstream_url": "https://api.pvoil.com.vn/prices?api_key=SECRET_API_KEY_123&user=vietfuel", '
        '"redirect": "https://xangdau.nhatkhanhtech.com/callback?token=SECRET_TOKEN_456&client_secret=MY_SECRET"}'
    )
    mock_resp.content = mock_resp.text.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "SECRET_API_KEY_123" not in preview
    assert "SECRET_TOKEN_456" not in preview
    assert "MY_SECRET" not in preview
    assert "api_key=[REDACTED]" in preview
    assert "token=[REDACTED]" in preview
    assert "client_secret=[REDACTED]" in preview
    assert "user=vietfuel" in preview


def test_sensitive_headers_never_included(monkeypatch):
    """Verify sensitive response headers (cookies, auth, custom keys) are never captured."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {
        "Content-Type": "application/json",
        "Server": "cloudflare",
        "Set-Cookie": "session_id=ultra_secret_cookie_val; HttpOnly",
        "Cookie": "user_id=12345; auth=xyz",
        "Authorization": "Bearer super_secret_access_token_12345",
        "X-Api-Key": "my-secret-api-key-9999",
        "X-Auth-Token": "bearer-token-val",
    }
    mock_resp.text = '{"error": "Forbidden"}'
    mock_resp.content = mock_resp.text.encode("utf-8")

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with pytest.raises(domestic_fuel.DomesticFuelFetchError) as exc_info:
        domestic_fuel._fetch_api_with_retries(session=mock_session)

    diag = exc_info.value.response_diagnostics
    headers_dict = diag["headers"]

    for sensitive_header in [
        "Set-Cookie",
        "Cookie",
        "Authorization",
        "X-Api-Key",
        "X-Auth-Token",
        "set-cookie",
        "cookie",
        "authorization",
    ]:
        assert sensitive_header not in headers_dict
        assert sensitive_header not in diag

    diag_str = str(diag)
    assert "ultra_secret_cookie_val" not in diag_str
    assert "super_secret_access_token_12345" not in diag_str
    assert "my-secret-api-key-9999" not in diag_str


def test_response_body_truncation_at_1000_characters():
    """Verify response body exceeding 1000 characters is safely truncated."""
    long_body = "A" * 1500
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_resp.text = long_body
    mock_resp.content = long_body.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)
    assert len(preview) == 1000
    assert preview == "A" * 1000


def test_empty_binary_and_malformed_response_bodies_handled():
    """Verify empty string, None, binary data, and malformed bodies are safely handled."""
    # 1. Empty text and content
    mock_resp_empty = MagicMock()
    mock_resp_empty.headers = {"Content-Type": "text/plain"}
    mock_resp_empty.text = ""
    mock_resp_empty.content = b""
    assert domestic_fuel._sanitize_response_body(mock_resp_empty) == ""

    # 2. None text and content
    mock_resp_none = MagicMock()
    mock_resp_none.headers = {"Content-Type": "application/json"}
    mock_resp_none.text = None
    mock_resp_none.content = None
    assert domestic_fuel._sanitize_response_body(mock_resp_none) == ""

    # 3. Binary non-text content
    mock_resp_binary = MagicMock()
    mock_resp_binary.headers = {"Content-Type": "application/octet-stream"}
    mock_resp_binary.text = "raw binary"
    mock_resp_binary.content = b"\x80\x81\xff\xfe\x00\x01data"
    preview_bin = domestic_fuel._sanitize_response_body(mock_resp_binary)
    assert isinstance(preview_bin, str)
    assert "\x00" not in preview_bin
    assert "\x01" not in preview_bin


def test_control_characters_removed_from_body_preview():
    """Verify control characters (e.g. null bytes, escape codes) are stripped."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_resp.text = "Hello\x00\x01\x08\x0b\x0c\x1bWorld\r\n\tAccepted"
    mock_resp.content = mock_resp.text.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)
    assert "\x00" not in preview
    assert "\x01" not in preview
    assert "\x08" not in preview
    assert "\x1b" not in preview
    assert preview == "HelloWorld\r\n\tAccepted"


def test_non_403_client_error_diagnostics_without_403_event(monkeypatch):
    """Verify non-403 client errors (404, 400) capture diagnostics but do not emit http_403_diagnostics."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {
        "Server": "nginx",
        "Content-Type": "application/json",
        "CF-Ray": "8e3641bc9a120456-NRT",
    }
    mock_resp.text = '{"detail": "Not found"}'
    mock_resp.content = mock_resp.text.encode("utf-8")

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    logged_events: list[dict] = []

    def mock_log(event: str, **kwargs):
        logged_events.append({"event": event, **kwargs})

    monkeypatch.setattr(domestic_fuel, "_log", mock_log)

    with pytest.raises(domestic_fuel.DomesticFuelFetchError) as exc_info:
        domestic_fuel._fetch_api_with_retries(session=mock_session)

    exc = exc_info.value
    assert exc.status_code == 404
    assert exc.deterministic is True

    assert exc.response_diagnostics["status_code"] == 404
    assert exc.response_diagnostics["headers"]["Server"] == "nginx"

    diag_403_events = [e for e in logged_events if e["event"] == "http_403_diagnostics"]
    assert len(diag_403_events) == 0
    assert mock_session.get.call_count == 1


def test_http_5xx_final_response_diagnostics_preserved_after_retries(monkeypatch):
    """Verify HTTP 5xx final response diagnostics are preserved when retries are exhausted."""
    monkeypatch.setattr(domestic_fuel, "_retry_settings", lambda: (2, 0.001))

    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 502
    mock_resp_500.headers = {"Server": "cloudflare", "CF-Ray": "502-ray-id", "Content-Type": "text/html"}
    mock_resp_500.text = "<title>Bad Gateway</title>"
    mock_resp_500.content = mock_resp_500.text.encode("utf-8")

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp_500

    logged_events: list[dict] = []

    def mock_log(event: str, **kwargs):
        logged_events.append({"event": event, **kwargs})

    monkeypatch.setattr(domestic_fuel, "_log", mock_log)

    with pytest.raises(domestic_fuel.DomesticFuelFetchError) as exc_info:
        domestic_fuel._fetch_api_with_retries(session=mock_session)

    assert exc_info.value.status_code == 502
    assert exc_info.value.deterministic is False
    assert mock_session.get.call_count == 3  # 1 initial + 2 retries

    diag = exc_info.value.response_diagnostics
    assert diag["status_code"] == 502
    assert diag["headers"]["Server"] == "cloudflare"
    assert diag["headers"]["CF-Ray"] == "502-ray-id"
    assert diag["body_preview"] == "HTML Title: Bad Gateway"

    # Intermediate retries must NOT log body previews
    retry_events = [e for e in logged_events if e["event"] == "fetch_retryable_error"]
    assert len(retry_events) == 2
    for r_evt in retry_events:
        assert "body_preview" not in r_evt


def test_intermediate_retry_responses_not_logged_with_body_previews(monkeypatch):
    """Verify intermediate retries never log response body previews."""
    monkeypatch.setattr(domestic_fuel, "_retry_settings", lambda: (2, 0.001))

    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500
    mock_resp_500.headers = {"Content-Type": "application/json"}
    mock_resp_500.text = '{"error": "Internal Server Error", "secret": "leaky_body_data"}'
    mock_resp_500.content = mock_resp_500.text.encode("utf-8")

    # Succeeds on 3rd attempt
    valid_payload = make_valid_payload()
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = valid_payload
    mock_resp_200.content = b'{"status": "ok"}'

    mock_session = MagicMock()
    mock_session.get.side_effect = [mock_resp_500, mock_resp_500, mock_resp_200]

    logged_events: list[dict] = []

    def mock_log(event: str, **kwargs):
        logged_events.append({"event": event, **kwargs})

    monkeypatch.setattr(domestic_fuel, "_log", mock_log)

    res = domestic_fuel._fetch_api_with_retries(session=mock_session)
    assert res == valid_payload

    retry_events = [e for e in logged_events if e["event"] == "fetch_retryable_error"]
    assert len(retry_events) == 2
    for r_evt in retry_events:
        assert "body_preview" not in r_evt
        assert "leaky_body_data" not in str(r_evt)


def test_existing_4xx_no_retry_behavior_remains_unchanged(monkeypatch):
    """Verify HTTP 4xx errors are deterministic and immediately fail without any retry."""
    monkeypatch.setattr(domestic_fuel, "_retry_settings", lambda: (3, 0.001))

    for code in [400, 401, 403, 404, 422]:
        mock_resp = MagicMock()
        mock_resp.status_code = code
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.text = f'{{"error": "{code}"}}'
        mock_resp.content = mock_resp.text.encode("utf-8")

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        with pytest.raises(domestic_fuel.DomesticFuelFetchError) as exc_info:
            domestic_fuel._fetch_api_with_retries(session=mock_session)

        assert exc_info.value.status_code == code
        assert exc_info.value.deterministic is True
        assert mock_session.get.call_count == 1  # No retries for 4xx


def test_network_exception_messages_do_not_expose_credentials_or_query_parameters(monkeypatch):
    """Verify network exception logs and error messages do not leak URLs or query parameters."""
    leak_url = "https://xangdau.nhatkhanhtech.com/api?api_key=SUPER_SECRET_KEY_999&token=SECRET_TOKEN"
    raw_error_msg = f"HTTPSConnectionPool(host='xangdau.nhatkhanhtech.com'): Max retries exceeded with url: {leak_url}"

    mock_session = MagicMock()
    mock_session.get.side_effect = requests.exceptions.ConnectionError(raw_error_msg)

    logged_events: list[dict] = []

    def mock_log(event: str, **kwargs):
        logged_events.append({"event": event, **kwargs})

    monkeypatch.setattr(domestic_fuel, "_log", mock_log)
    monkeypatch.setattr(domestic_fuel, "_retry_settings", lambda: (1, 0.001))

    with pytest.raises(domestic_fuel.DomesticFuelFetchError) as exc_info:
        domestic_fuel._fetch_api_with_retries(session=mock_session)

    # Error message must not contain secret values
    err_str = str(exc_info.value)
    assert "SUPER_SECRET_KEY_999" not in err_str
    assert "SECRET_TOKEN" not in err_str
    assert "ConnectionError: Connection failed" in err_str

    # Logged retry event must not contain secret values
    retry_events = [e for e in logged_events if e["event"] == "fetch_retryable_error"]
    assert len(retry_events) == 1
    assert "SUPER_SECRET_KEY_999" not in str(retry_events[0])
    assert "SECRET_TOKEN" not in str(retry_events[0])
    assert retry_events[0]["error"] == "ConnectionError: Connection failed"


def test_check_only_reports_diagnostics_on_http_403(monkeypatch):
    """Verify check_only reports safe diagnostics in check_fetch_error log event on HTTP 403."""
    diag = {
        "status_code": 403,
        "headers": {"Server": "cloudflare", "CF-Ray": "8e3641bc9a120456-NRT"},
        "body_preview": "HTML Title: Just a moment...",
    }

    def mock_fetch():
        raise domestic_fuel.DomesticFuelFetchError(
            "VietFuel API HTTP client error: 403",
            status_code=403,
            deterministic=True,
            response_diagnostics=diag,
        )

    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", mock_fetch)

    logged_events: list[dict] = []

    def mock_log(event: str, **kwargs):
        logged_events.append({"event": event, **kwargs})

    monkeypatch.setattr(domestic_fuel, "_log", mock_log)

    with pytest.raises(SystemExit) as exc:
        domestic_fuel.check_only()

    assert exc.value.code == 1
    fetch_err_events = [e for e in logged_events if e["event"] == "check_fetch_error"]
    assert len(fetch_err_events) == 1
    logged_diag = fetch_err_events[0]["diagnostics"]
    assert logged_diag["status_code"] == 403
    assert logged_diag["headers"]["Server"] == "cloudflare"
    assert logged_diag["headers"]["CF-Ray"] == "8e3641bc9a120456-NRT"


def test_run_reports_diagnostics_on_http_403(monkeypatch, tmp_path):
    """Verify run reports safe diagnostics in fetch_error log event on HTTP 403."""
    csv_file = tmp_path / "data_price_petro.csv"
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)

    diag = {
        "status_code": 403,
        "headers": {"Server": "cloudflare", "CF-Ray": "8e3641bc9a120456-NRT"},
        "body_preview": "HTML Title: Just a moment...",
    }

    def mock_fetch():
        raise domestic_fuel.DomesticFuelFetchError(
            "VietFuel API HTTP client error: 403",
            status_code=403,
            deterministic=True,
            response_diagnostics=diag,
        )

    monkeypatch.setattr(domestic_fuel, "_fetch_api_with_retries", mock_fetch)

    logged_events: list[dict] = []

    def mock_log(event: str, **kwargs):
        logged_events.append({"event": event, **kwargs})

    monkeypatch.setattr(domestic_fuel, "_log", mock_log)

    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()

    assert exc.value.code == 1
    fetch_err_events = [e for e in logged_events if e["event"] == "fetch_error"]
    assert len(fetch_err_events) == 1
    logged_diag = fetch_err_events[0]["diagnostics"]
    assert logged_diag["status_code"] == 403
    assert logged_diag["headers"]["Server"] == "cloudflare"
    assert logged_diag["headers"]["CF-Ray"] == "8e3641bc9a120456-NRT"


def test_secret_in_html_title_is_redacted():
    """Verify secrets inside HTML title tags are thoroughly redacted."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html; charset=UTF-8"}
    mock_resp.text = "<html><head><title>Error: password=mysecret123 and accessToken=abc_token_456</title></head></html>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "mysecret123" not in preview
    assert "abc_token_456" not in preview
    assert preview == "HTML Title: Error: password=[REDACTED] and accessToken=[REDACTED]"


def test_secret_in_response_header_is_redacted():
    """Verify secrets appearing in allowlisted response headers are redacted."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {
        "Server": "cloudflare Bearer secret_bearer_token_888",
        "Via": "1.1 vegur, apiKey=secret_api_key_777",
    }
    mock_resp.text = "<title>Access Denied</title>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    diag = domestic_fuel._extract_safe_response_diagnostics(mock_resp)

    assert "secret_bearer_token_888" not in diag["headers"]["Server"]
    assert "secret_api_key_777" not in diag["headers"]["Via"]
    assert diag["headers"]["Server"] == "cloudflare Bearer [REDACTED]"
    assert diag["headers"]["Via"] == "1.1 vegur, apiKey=[REDACTED]"


def test_header_crlf_and_control_characters_removed():
    """Verify CR/LF and control characters in header values are stripped to prevent log injection."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {
        "Server": "cloudflare\r\nInjected-Header: evil_attack\x00\x08\x1b",
        "Via": "1.1\nvegur\r\n",
    }
    mock_resp.text = "<title>OK</title>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    diag = domestic_fuel._extract_safe_response_diagnostics(mock_resp)

    server_val = diag["headers"]["Server"]
    via_val = diag["headers"]["Via"]

    assert "\r" not in server_val
    assert "\n" not in server_val
    assert "\x00" not in server_val
    assert "\x08" not in server_val
    assert "\x1b" not in server_val
    assert server_val == "cloudflare Injected-Header: evil_attack"
    assert via_val == "1.1 vegur"


def test_header_length_limit_enforced():
    """Verify header values exceeding MAX_HEADER_VALUE_LENGTH (256) are safely truncated."""
    long_val = "A" * 500
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {"Server": long_val}
    mock_resp.text = "<title>OK</title>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    diag = domestic_fuel._extract_safe_response_diagnostics(mock_resp)

    assert len(diag["headers"]["Server"]) == 256
    assert diag["headers"]["Server"] == "A" * 256


def test_camelcase_and_snakecase_credential_redaction():
    """Verify camelCase, snake_case, kebab-case, and uppercase credential keys are redacted."""
    raw_payload = (
        '{"accessToken": "tok1", "access_token": "tok2", "access-token": "tok3", '
        '"refreshToken": "ref1", "refresh_token": "ref2", '
        '"clientSecret": "cs1", "client_secret": "cs2", '
        '"apiKey": "k1", "api_key": "k2", '
        '"xApiKey": "xk1", "x_api_key": "xk2", "X_API_KEY": "xk3", '
        '"authToken": "at1", "auth_token": "at2", "password": "pw1"}'
    )
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.text = raw_payload
    mock_resp.content = raw_payload.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    for leaked in ["tok1", "tok2", "tok3", "ref1", "ref2", "cs1", "cs2", "k1", "k2", "xk1", "xk2", "xk3", "at1", "at2", "pw1"]:
        assert leaked not in preview


def test_url_encoded_credential_parameters():
    """Verify plain and URL-encoded credential parameters in URLs are cleanly redacted."""
    raw_text = (
        "Call failed: https://api.example.com/data?apiKey=secret_key_1"
        "&token%3Dsecret_tok_2%26clientSecret=secret_cs_3&user=vietfuel"
        "%3FaccessToken%3Dsecret_tok_4%26next=true"
    )
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_resp.text = raw_text
    mock_resp.content = raw_text.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "secret_key_1" not in preview
    assert "secret_tok_2" not in preview
    assert "secret_cs_3" not in preview
    assert "secret_tok_4" not in preview
    assert "apiKey=[REDACTED]" in preview
    assert "token%3D[REDACTED]" in preview
    assert "clientSecret=[REDACTED]" in preview
    assert "accessToken%3D[REDACTED]" in preview
    assert "user=vietfuel" in preview
    assert "next=true" in preview


def test_very_large_response_body_safely_bounded():
    """Verify that processing very large response bodies is capped and does not hang."""
    large_body = ("A" * 1000 + " ") * 2000  # 2+ MB body
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_resp.text = large_body
    mock_resp.content = large_body.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert len(preview) <= 1000
    assert len(preview) == 1000


def test_html_challenge_without_title():
    """Verify HTML challenge pages without a title tag are detected safely."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.text = "<html><body><h1>Cloudflare Ray ID: 8e123</h1><p>Checking your browser...</p></body></html>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)
    assert preview == "HTML challenge page detected"


def test_binary_and_malformed_response_bodies_no_exception():
    """Verify binary, invalid byte sequences, and malformed responses never raise exceptions."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "application/octet-stream"}
    # Invalid UTF-8 bytes mixed with null bytes
    mock_resp.content = b"\x80\xff\xfe\x00\x01\x02\xc3\x28\x00\x1b"
    mock_resp.text = None

    preview = domestic_fuel._sanitize_response_body(mock_resp)
    assert isinstance(preview, str)
    assert "\x00" not in preview
    assert "\x1b" not in preview


# ---------------------------------------------------------------------------
# HTML detection: body-prefix sniffing (Content-Type agnostic)
# ---------------------------------------------------------------------------


def test_html_body_with_text_plain_content_type_never_leaks_raw_html():
    """HTML body advertised as text/plain must be detected via body sniff and never logged raw."""
    html_body = (
        "<!DOCTYPE html><html><head><title>Blocked</title></head>"
        "<body><p>Your request was blocked.</p></body></html>"
    )
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/plain; charset=utf-8"}
    mock_resp.text = html_body
    mock_resp.content = html_body.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    # Must not expose raw HTML markup
    assert "<html" not in preview
    assert "<body" not in preview
    assert "<p>" not in preview
    # Title must be extracted safely
    assert preview == "HTML Title: Blocked"


def test_html_body_with_missing_content_type_never_leaks_raw_html():
    """HTML body with no Content-Type header must be detected via body sniff."""
    html_body = "<html><head><title>Access Denied</title></head><body></body></html>"
    mock_resp = MagicMock()
    mock_resp.headers = {}
    mock_resp.text = html_body
    mock_resp.content = html_body.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "<html" not in preview
    assert preview == "HTML Title: Access Denied"


def test_html_body_without_title_and_text_plain_content_type_returns_safe_fallback():
    """HTML body with no <title> and wrong Content-Type returns empty string (no raw HTML)."""
    html_body = (
        "<!DOCTYPE html><html><body>"
        "<h1>Unknown error</h1>"
        "</body></html>"
    )
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_resp.text = html_body
    mock_resp.content = html_body.encode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "<html" not in preview
    assert "<body" not in preview
    # No recognisable challenge keyword → empty string
    assert preview == ""


def test_utf8_bom_html_body_with_text_plain_content_type_never_leaks_raw_html():
    """HTML body with UTF-8 BOM and text/plain Content-Type must be detected as HTML and not leak raw markup."""
    bom_html_bytes = b"\xef\xbb\xbf<!DOCTYPE html><html><head><title>BOM Blocked</title></head><body><p>Blocked payload</p></body></html>"
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/plain; charset=utf-8"}
    mock_resp.content = bom_html_bytes
    mock_resp.text = bom_html_bytes.decode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "<!DOCTYPE" not in preview
    assert "<html" not in preview
    assert "<body" not in preview
    assert "<p>" not in preview
    assert preview == "HTML Title: BOM Blocked"


def test_utf8_bom_html_body_with_missing_content_type_never_leaks_raw_html():
    """HTML body with UTF-8 BOM and missing Content-Type must be detected as HTML and not leak raw markup."""
    bom_html_bytes = b"  \r\n\xef\xbb\xbf  <html><head><title>BOM Access Denied</title></head><body>Secret body</body></html>"
    mock_resp = MagicMock()
    mock_resp.headers = {}
    mock_resp.content = bom_html_bytes
    mock_resp.text = bom_html_bytes.decode("utf-8")

    preview = domestic_fuel._sanitize_response_body(mock_resp)

    assert "<html" not in preview
    assert "<body" not in preview
    assert "Secret body" not in preview
    assert preview == "HTML Title: BOM Access Denied"


def test_body_bytes_are_html_with_utf8_bom_and_whitespace():
    """Verify _body_bytes_are_html recognizes UTF-8 BOM followed by HTML signatures with whitespace."""
    assert domestic_fuel._body_bytes_are_html(b"\xef\xbb\xbf<!DOCTYPE html>") is True
    assert domestic_fuel._body_bytes_are_html(b"\xef\xbb\xbf<html>") is True
    assert domestic_fuel._body_bytes_are_html(b" \t\r\n\xef\xbb\xbf  <!doctype HTML>") is True
    assert domestic_fuel._body_bytes_are_html(b" \xef\xbb\xbf  <HTML lang='en'>") is True
    # Non-HTML with BOM must return False
    assert domestic_fuel._body_bytes_are_html(b"\xef\xbb\xbf{\"error\": \"not found\"}") is False
    assert domestic_fuel._body_bytes_are_html(b"\xef\xbb\xbfPlain text error message") is False


def test_real_response_diagnostics_do_not_buffer_full_multimegabyte_stream():
    """For a real requests.Response the diagnostics path must not buffer the full body.

    Uses a CountedStream raw backend to prove that only 16 KB are ever read
    from the network socket and that .content / ._content are never touched.
    """
    five_mb_html = (
        b"<!DOCTYPE html><html><head><title>Cloudflare error</title></head><body>"
        + b"X" * (5 * 1024 * 1024 - 200)
        + b"</body></html>"
    )
    counted = CountedStream(five_mb_html)

    resp = requests.Response()
    resp.status_code = 403
    resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    resp.raw = counted

    diag = domestic_fuel._extract_safe_response_diagnostics(resp)

    assert counted.bytes_read <= domestic_fuel.MAX_BODY_SCAN_BYTES
    assert counted.bytes_read == domestic_fuel.MAX_BODY_SCAN_BYTES
    # Must never buffer the full body into _content
    assert resp._content is False
    assert "<html" not in diag["body_preview"]
    assert diag["body_preview"] == "HTML Title: Cloudflare error"


def test_real_response_diagnostics_never_access_content_or_text_attribute():
    """Prove that for a real requests.Response the error-diagnostics path uses only
    iter_content() and never falls back to .content or .text.

    We set ._content = False (the sentinel that requests uses for unread bodies)
    and use a CountedStream.  After diagnostics _content must still be False.
    """
    payload = b"plain text error body " + b"Y" * 100
    counted = CountedStream(payload)

    resp = requests.Response()
    resp.status_code = 500
    resp.headers = {"Content-Type": "text/plain"}
    resp.raw = counted

    # Confirm _content is the unread sentinel before we call diagnostics
    assert resp._content is False

    diag = domestic_fuel._extract_safe_response_diagnostics(resp)

    # _content must remain False — we never called response.content
    assert resp._content is False
    assert counted.bytes_read > 0  # some bytes were streamed
    assert counted.bytes_read <= domestic_fuel.MAX_BODY_SCAN_BYTES
# Streamed Error Response Bounds & Resource Management Tests
# ---------------------------------------------------------------------------


class CountedStream:
    """Simulated stream wrapper to track exact bytes consumed over network."""

    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0
        self.bytes_read = 0
        self.closed = False

    def read(self, amt: int | None = None, decode_content: bool | None = None):
        if self._offset >= len(self._data):
            return b""
        if amt is None or amt < 0:
            chunk = self._data[self._offset :]
        else:
            chunk = self._data[self._offset : self._offset + amt]
        self._offset += len(chunk)
        self.bytes_read += len(chunk)
        return chunk

    def close(self):
        self.closed = True


def test_error_diagnostics_never_consume_more_than_16kb_from_multimegabyte_stream():
    """Verify that diagnostics on error responses strictly read at most 16 KB from the stream."""
    five_mb_payload = b"Error details: " + (b"X" * 1024) * 5000  # ~5.1 MB
    counted_stream = CountedStream(five_mb_payload)

    resp = requests.Response()
    resp.status_code = 403
    resp.headers = {"Content-Type": "text/plain"}
    resp.raw = counted_stream

    diag = domestic_fuel._extract_safe_response_diagnostics(resp)

    # Must consume at most MAX_BODY_SCAN_BYTES (16384 bytes)
    assert counted_stream.bytes_read <= domestic_fuel.MAX_BODY_SCAN_BYTES
    assert counted_stream.bytes_read == 16384
    # response._content must not have been loaded/buffered
    assert resp._content is False
    # Preview bounded
    assert len(diag["body_preview"]) <= domestic_fuel.MAX_BODY_PREVIEW_LENGTH


def test_fetch_uses_stream_true_and_closes_resources_on_error():
    """Verify session.get is called with stream=True and response is closed on HTTP 403."""
    mock_session = MagicMock()
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 403
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.iter_content.return_value = [b"<html><head><title>Forbidden</title></head></html>"]
    mock_session.get.return_value = mock_resp

    with pytest.raises(domestic_fuel.DomesticFuelFetchError) as exc_info:
        domestic_fuel._fetch_api_with_retries(session=mock_session)

    # stream=True was used
    call_kwargs = mock_session.get.call_args[1]
    assert call_kwargs.get("stream") is True
    # response was closed
    assert mock_resp.close.call_count == 1
    assert exc_info.value.status_code == 403
    assert exc_info.value.response_diagnostics["body_preview"] == "HTML Title: Forbidden"


def test_fetch_closes_resources_on_successful_json_response():
    """Verify response is closed after normal JSON parsing for successful responses."""
    valid_payload = make_valid_payload()
    mock_session = MagicMock()
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = valid_payload
    mock_resp._content = b'{"status": "ok"}'
    mock_session.get.return_value = mock_resp

    result = domestic_fuel._fetch_api_with_retries(session=mock_session)
    assert result == valid_payload
    assert mock_resp.close.call_count == 1


def test_fetch_closes_resources_on_json_decode_error():
    """Verify response is closed if JSON decoding fails on a 200 response."""
    mock_session = MagicMock()
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Corrupted JSON")
    mock_session.get.return_value = mock_resp

    with pytest.raises(domestic_fuel.DomesticFuelSchemaError):
        domestic_fuel._fetch_api_with_retries(session=mock_session)

    assert mock_resp.close.call_count == 1


# ---------------------------------------------------------------------------
# Authorization Credential Redaction Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth_sample",
    [
        "Authorization: Bearer short",
        "Authorization: Bearer eyJhbGciOi...",
        "Authorization: Basic abcdefghijklmnop",
        "authorization=Basic abcdefghijklmnop",
        "authorization=Bearer short",
        '{"Authorization": "Bearer short"}',
        '{"authorization": "Basic abcdefghijklmnop"}',
        "https://example.com/api?authorization=Basic%20abcdefghijklmnop&foo=bar",
        "Authorization: Digest username=admin, realm=zone",
        "Authorization: secret_without_scheme",
    ],
)
def test_authorization_entire_value_redacted(auth_sample):
    """Verify entire Authorization value is redacted regardless of scheme or token length."""
    redacted = domestic_fuel._sanitize_text_credentials(auth_sample)

    assert "short" not in redacted
    assert "eyJhbGciOi..." not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "secret_without_scheme" not in redacted
    assert "[REDACTED]" in redacted


def test_authorization_digest_with_double_quotes():
    """Verify HTTP Digest Authorization with quoted parameters is fully redacted without leaking secrets."""
    header = 'Authorization: Digest username="alice", realm="example", nonce="secret", uri="/api/v1", response="secret_resp"'
    redacted = domestic_fuel._sanitize_text_credentials(header)

    assert redacted == "Authorization: [REDACTED]"
    for leaked in ["alice", "example", "secret", "secret_resp", "/api/v1"]:
        assert leaked not in redacted


def test_authorization_digest_without_quotes():
    """Verify HTTP Digest Authorization with unquoted parameters is fully redacted."""
    header = "Authorization: Digest username=admin, realm=zone, nonce=secret123, qop=auth, nc=00000001, cnonce=secret_client"
    redacted = domestic_fuel._sanitize_text_credentials(header)

    assert redacted == "Authorization: [REDACTED]"
    for leaked in ["admin", "zone", "secret123", "secret_client"]:
        assert leaked not in redacted


def test_json_authorization_with_digest_preserves_valid_json():
    """Verify JSON containing Digest Authorization replaces entire value and preserves valid JSON structure."""
    payload_str = json.dumps({
        "status": "error",
        "authorization": 'Digest username="alice", realm="example", nonce="secret", response="secret_resp"',
        "detail": "Authentication required",
        "code": 401,
    })
    redacted = domestic_fuel._sanitize_text_credentials(payload_str)


    # Valid JSON parsing
    parsed = json.loads(redacted)
    assert parsed["status"] == "error"
    assert parsed["authorization"] == "[REDACTED]"
    assert parsed["detail"] == "Authentication required"
    assert parsed["code"] == 401
    for leaked in ["alice", "example", "secret", "secret_resp"]:
        assert leaked not in redacted

def test_json_authorization_with_apostrophe_fully_redacted():
    payload = '{"authorization": "Bearer O\'Reilly-secret", "status": "ok"}'

    sanitized = domestic_fuel._sanitize_text_credentials(payload)
    parsed = json.loads(sanitized)

    assert parsed["authorization"] == "[REDACTED]"
    assert "O'Reilly-secret" not in sanitized
    assert parsed["status"] == "ok"


def test_authorization_bearer_and_basic_still_redacted():
    """Verify Bearer and Basic authentication headers and values are still thoroughly redacted."""
    cases = [
        ("Authorization: Bearer short", "Authorization: [REDACTED]"),
        ("Authorization: Bearer eyJhbGciOi...", "Authorization: [REDACTED]"),
        ("Authorization: Basic abcdefghijklmnop", "Authorization: [REDACTED]"),
        ("authorization=Basic abcdefghijklmnop", "authorization=[REDACTED]"),
        ("authorization=Bearer short", "authorization=[REDACTED]"),
        ("Server: gateway Bearer my_bearer_token", "Server: gateway Bearer [REDACTED]"),
        ("Via: proxy Basic my_basic_auth", "Via: proxy Basic [REDACTED]"),
    ]
    for raw, expected in cases:
        redacted = domestic_fuel._sanitize_text_credentials(raw)
        assert redacted == expected
        for secret in ["short", "eyJhbGciOi...", "abcdefghijklmnop", "my_bearer_token", "my_basic_auth"]:
            assert secret not in redacted


def test_authorization_url_query_redacted():
    """Verify Authorization in URL query parameters is redacted up to next parameter or URL end."""
    url1 = "https://service.test/api?authorization=Digest%20username%3D%22alice%22%2Crealm%3D%22myrealm%22&foo=bar"
    redacted1 = domestic_fuel._sanitize_text_credentials(url1)
    assert "authorization=[REDACTED]" in redacted1
    assert "foo=bar" in redacted1
    assert "alice" not in redacted1
    assert "myrealm" not in redacted1

    url2 = "https://example.com/api?source=pvoil&authorization=Basic%20secret123"
    redacted2 = domestic_fuel._sanitize_text_credentials(url2)
    assert redacted2 == "https://example.com/api?source=pvoil&authorization=[REDACTED]"
    assert "secret123" not in redacted2


# ---------------------------------------------------------------------------
# Header Bound & Unusually Long Header Value Tests
# ---------------------------------------------------------------------------


def test_unusually_long_header_value_bounded_before_regex():
    """Verify unusually long header value is bounded before regex and strictly limited to 256 chars."""
    huge_header = "Cloudflare-Edge " + ("Server-Info-Token " * 5000)  # ~90,000 chars
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {
        "Server": huge_header,
        "CF-Ray": "apiKey=secret_key_in_header " + ("B" * 50000),
    }
    mock_resp.text = "<title>Error</title>"
    mock_resp.content = mock_resp.text.encode("utf-8")

    diag = domestic_fuel._extract_safe_response_diagnostics(mock_resp)

    server_val = diag["headers"]["Server"]
    cf_ray_val = diag["headers"]["CF-Ray"]

    assert len(server_val) <= domestic_fuel.MAX_HEADER_VALUE_LENGTH
    assert len(cf_ray_val) <= domestic_fuel.MAX_HEADER_VALUE_LENGTH
    assert len(server_val) == 256
    assert len(cf_ray_val) == 256
    assert "secret_key_in_header" not in cf_ray_val
    assert "apiKey=[REDACTED]" in cf_ray_val
