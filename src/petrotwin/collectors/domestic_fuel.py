"""Collector: Vietnamese domestic fuel prices from PVOIL via VietFuel API.

Fetches the latest retail fuel prices for PVOIL via VietFuel REST API:
    https://xangdau.nhatkhanhtech.com/api/fuel-prices/pvoil
and appends new rows to ``data/raw/data_price_petro.csv``.

Data Provenance:
    - Root Data Source: PVOIL (https://pvoil.com.vn/tin-gia-xang-dau)
    - Intermediary API Provider: VietFuel API (https://xangdau.nhatkhanhtech.com)
    - Collection Method: REST API

VietFuel API serves as an intermediary collection interface; PVOIL remains the
authoritative root publisher of retail fuel prices.

Usage::

    python -m petrotwin.collectors.domestic_fuel

Environment variables:
    PETROTWIN_DATA_DIR — optional override for the ``data/raw`` directory.
"""

from __future__ import annotations
import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
import yaml

from petrotwin.collectors.locks import file_lock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_API_ENDPOINT = "https://xangdau.nhatkhanhtech.com/api/fuel-prices/pvoil"
SOURCE_ID = "pvoil"
SOURCE_NAME = "PVOIL"
API_PROVIDER = "VietFuel API"
COLLECTION_METHOD = "REST API"
SOURCE_URL = "https://pvoil.com.vn/tin-gia-xang-dau"
UNIT = "Đồng/lít thực tế"
INCLUDES_VAT = "true"
DEFAULT_EFFECTIVE_TIME = "15:00"

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_RETRY_COUNT = 2
DEFAULT_RETRY_DELAY = 10.0

CHANGE_REGEX = re.compile(r"^[+-]?\d+$")

# Canonical expected products (must exactly match these 4 products)
EXPECTED_PRODUCTS: frozenset[str] = frozenset(
    [
        "Xăng E10 RON 95-III",
        "Xăng E5 RON 92-II",
        "Dầu DO 0,001S-V",
        "Dầu DO 0,05S-II",
    ]
)

# Canonical products retained for compatibility with pipeline and historical data
KNOWN_PRODUCTS: frozenset[str] = frozenset(
    [
        "Dầu DO 0,005S-IV",
        "Dầu DO 0,05S-II",
        "Dầu DO 0,001S-V",
        "Dầu FO 3,5%S",
        "Dầu FO 3%S",
        "Dầu KO",
        "Xăng E5 RON 92-II",
        "Xăng E10 RON 95-III",
        "Xăng RON 95-III",
        "Xăng RON 95-IV",
    ]
)

# Mapping from API product names to PetroTwin canonical names
API_PRODUCT_MAP: dict[str, str] = {
    "xăng e10 ron 95-iii": "Xăng E10 RON 95-III",
    "e10 ron 95-iii": "Xăng E10 RON 95-III",
    "xăng e5 ron 92-ii": "Xăng E5 RON 92-II",
    "e5 ron 92-ii": "Xăng E5 RON 92-II",
    "do 0,001s-v": "Dầu DO 0,001S-V",
    "dầu do 0,001s-v": "Dầu DO 0,001S-V",
    "do 0.001s-v": "Dầu DO 0,001S-V",
    "dầu do 0.001s-v": "Dầu DO 0,001S-V",
    "do 0,05s-ii": "Dầu DO 0,05S-II",
    "dầu do 0,05s-ii": "Dầu DO 0,05S-II",
    "do 0.05s-ii": "Dầu DO 0,05S-II",
    "dầu do 0.05s-ii": "Dầu DO 0,05S-II",
}

COLUMNS: list[str] = [
    "date",
    "effective_date",
    "effective_time",
    "product_name",
    "price_vnd_per_liter",
    "price_display",
    "price_change_vnd",
    "price_change_display",
    "unit",
    "includes_vat_and_environmental_tax",
    "source_url",
    "extracted_at_utc",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _log(event: str, **kwargs: object) -> None:
    record = {"event": event, "ts": _utcnow_iso(), **kwargs}
    log.info(json.dumps(record, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DomesticFuelError(Exception):
    """Base exception for domestic fuel collector errors."""

    def __init__(
        self,
        message: str,
        category: str = "collector_error",
        status_code: int | None = None,
        deterministic: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.deterministic = deterministic


class DomesticFuelFetchError(DomesticFuelError):
    """Network, connection, or HTTP error while calling the API."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        deterministic: bool = False,
    ) -> None:
        category = "http_error" if status_code is not None else "connection_error"
        super().__init__(
            message,
            category=category,
            status_code=status_code,
            deterministic=deterministic,
        )


class DomesticFuelAPIError(DomesticFuelError):
    """API-level error response (success=false, status!=ok, blocked, or stale)."""

    def __init__(
        self,
        message: str,
        category: str = "source_unavailable",
        status_code: int | None = None,
    ) -> None:
        super().__init__(
            message,
            category=category,
            status_code=status_code,
            deterministic=True,
        )


class DomesticFuelSchemaError(DomesticFuelError):
    """Schema validation failure: unexpected products, missing products, or invalid prices."""

    def __init__(self, message: str, category: str = "schema_error") -> None:
        super().__init__(message, category=category, deterministic=True)


class DomesticFuelDateError(DomesticFuelError):
    """Effective date validation failure: invalid date format or future date."""

    def __init__(self, message: str, category: str = "future_date") -> None:
        super().__init__(message, category=category, deterministic=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_vn() -> date:
    """Return the current date in Asia/Ho_Chi_Minh timezone."""
    return datetime.now(VN_TZ).date()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _raw_dir() -> Path:
    override = os.environ.get("PETROTWIN_DATA_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else (_repo_root() / path).resolve()
    return _repo_root() / "data" / "raw"


def _csv_path() -> Path:
    return _raw_dir() / "data_price_petro.csv"


def _is_valid_https_url(url: Any) -> bool:
    """Validate that url is a well-formed HTTPS URL string with a non-empty hostname."""
    if not isinstance(url, str):
        return False
    cleaned = url.strip()
    if not cleaned.startswith("https://") or len(cleaned) <= 8:
        return False
    try:
        parsed = urlsplit(cleaned)
        return parsed.scheme == "https" and bool(parsed.netloc)
    except Exception:
        return False


def _collector_config() -> dict[str, Any]:
    """Load configuration dictionary safely without letting AttributeError escape."""
    config_path = _repo_root() / "configs" / "collectors.yaml"
    try:
        with config_path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
            if not isinstance(data, dict):
                _log("config_warning", error=f"Collector config root must be a dict, got {type(data).__name__}")
                return {}
            section = data.get("domestic_fuel")
            if not isinstance(section, dict):
                _log("config_warning", error=f"Section 'domestic_fuel' must be a dict, got {type(section).__name__}")
                return {}
            return section
    except (OSError, yaml.YAMLError) as exc:
        _log("config_warning", error=f"Could not load collector config: {exc}")
        return {}
    except Exception as exc:
        _log("config_warning", error=f"Unexpected error loading collector config: {exc}")
        return {}


def _api_endpoint() -> str:
    """Read API endpoint from config with HTTPS validation and explicit fallback logging."""
    cfg = _collector_config()
    raw = cfg.get("api_endpoint")
    if raw is None:
        return DEFAULT_API_ENDPOINT
    if not _is_valid_https_url(raw):
        _log(
            "config_warning",
            error=f"api_endpoint must be a valid HTTPS URL, got {raw!r}. Using default: {DEFAULT_API_ENDPOINT}",
        )
        return DEFAULT_API_ENDPOINT
    return str(raw).strip()


def _request_timeout() -> float:
    """Read request timeout from config with finite, positive validation and fallback logging."""
    cfg = _collector_config()
    raw = cfg.get("request_timeout_seconds")
    if raw is None:
        return DEFAULT_REQUEST_TIMEOUT
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _log(
            "config_warning",
            error=f"request_timeout_seconds must be a number, got {type(raw).__name__}: {raw!r}. Using default: {DEFAULT_REQUEST_TIMEOUT}",
        )
        return DEFAULT_REQUEST_TIMEOUT
    val = float(raw)
    if math.isnan(val) or math.isinf(val) or val <= 0:
        _log(
            "config_warning",
            error=f"request_timeout_seconds must be finite and positive, got {val}. Using default: {DEFAULT_REQUEST_TIMEOUT}",
        )
        return DEFAULT_REQUEST_TIMEOUT
    return val


def _retry_settings() -> tuple[int, float]:
    """Read retry settings with finite, non-negative validation and fallback logging."""
    cfg = _collector_config()

    # Retry count
    raw_count = cfg.get("retry_count")
    if raw_count is None:
        retry_count = DEFAULT_RETRY_COUNT
    elif isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
        _log(
            "config_warning",
            error=f"retry_count must be a non-negative integer, got {type(raw_count).__name__}: {raw_count!r}. Using default: {DEFAULT_RETRY_COUNT}",
        )
        retry_count = DEFAULT_RETRY_COUNT
    else:
        retry_count = raw_count

    # Retry delay
    raw_delay = cfg.get("retry_delay_seconds")
    if raw_delay is None:
        retry_delay = DEFAULT_RETRY_DELAY
    elif isinstance(raw_delay, bool) or not isinstance(raw_delay, (int, float)):
        _log(
            "config_warning",
            error=f"retry_delay_seconds must be a number, got {type(raw_delay).__name__}: {raw_delay!r}. Using default: {DEFAULT_RETRY_DELAY}",
        )
        retry_delay = DEFAULT_RETRY_DELAY
    else:
        val_delay = float(raw_delay)
        if math.isnan(val_delay) or math.isinf(val_delay) or val_delay < 0:
            _log(
                "config_warning",
                error=f"retry_delay_seconds must be a finite non-negative number, got {val_delay}. Using default: {DEFAULT_RETRY_DELAY}",
            )
            retry_delay = DEFAULT_RETRY_DELAY
        else:
            retry_delay = val_delay

    return retry_count, retry_delay


def _check_csv_header(path: Path) -> None:
    """Pre-flight check: if CSV exists and has data, verify header matches COLUMNS exactly."""
    if not path.exists():
        return
    with path.open("r", newline="", encoding="utf-8") as src:
        first_line = src.readline()
    if not first_line.strip():
        return  # Empty or whitespace-only file is fine; will be initialized on write

    reader = csv.reader([first_line.strip()])
    try:
        header = next(reader)
    except StopIteration:
        return

    if header != COLUMNS:
        raise DomesticFuelSchemaError(
            f"Pre-flight check failed: CSV header in {path} does not match expected COLUMNS.\n"
            f"Expected: {COLUMNS}\n"
            f"Found:    {header}"
        )


def _last_date_in_csv(path: Path) -> date | None:
    """Find the maximum effective date in the domestic fuel CSV."""
    if not path.exists():
        return None
    last: date | None = None
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                date_str = row.get("effective_date", "").strip() or row.get("date", "").strip()
                if not date_str:
                    continue
                d = date.fromisoformat(date_str)
                if last is None or d > last:
                    last = d
            except (KeyError, ValueError):
                continue
    return last


def _last_prices_by_product(path: Path) -> dict[str, int]:
    """Return the price with the latest effective_date per product from the domestic fuel CSV."""
    if not path.exists():
        return {}
    latest_by_product: dict[str, tuple[date, int]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                prod = row.get("product_name", "").strip()
                price_str = row.get("price_vnd_per_liter", "").strip()
                date_str = row.get("effective_date", "").strip() or row.get("date", "").strip()
                if not prod or not price_str or not date_str:
                    continue
                row_date = date.fromisoformat(date_str)
                price_val = int(price_str)

                if prod not in latest_by_product or row_date > latest_by_product[prod][0]:
                    latest_by_product[prod] = (row_date, price_val)
            except (KeyError, ValueError):
                continue
    return {prod: price for prod, (_dt, price) in latest_by_product.items()}


def _format_price_display(price: int) -> str:
    """Format integer price with dot thousands separator: 25100 -> '25.100 đ'."""
    formatted = f"{price:,}".replace(",", ".")
    return f"{formatted} đ"


def _format_change_display(change: int) -> str:
    """Format price change: +500 -> '+500 đ', -300 -> '-300 đ', 0 -> '0 đ'."""
    if change == 0:
        return "0 đ"
    sign = "+" if change > 0 else ""
    return f"{sign}{change} đ"


def normalize_api_product_name(raw_name: str) -> str | None:
    """Normalize and map an API product name to a canonical PetroTwin name."""
    if not raw_name or not isinstance(raw_name, str):
        return None
    cleaned = " ".join(raw_name.strip().split()).casefold()
    return API_PRODUCT_MAP.get(cleaned)


# ---------------------------------------------------------------------------
# Numeric Validation
# ---------------------------------------------------------------------------


def _validate_positive_int(value: Any, field_name: str, product_name: str) -> int:
    """Validate that value is a positive integer (rejecting bool, non-int float, NaN, Inf, <= 0)."""
    if value is None:
        raise DomesticFuelSchemaError(f"Field {field_name} for {product_name} cannot be null")
    if isinstance(value, bool):
        raise DomesticFuelSchemaError(f"Field {field_name} for {product_name} cannot be a boolean")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise DomesticFuelSchemaError(
                f"Field {field_name} for {product_name} cannot be NaN or Infinity"
            )
        if not value.is_integer():
            raise DomesticFuelSchemaError(
                f"Field {field_name} for {product_name} cannot be a non-integer float: {value}"
            )
        int_val = int(value)
    elif isinstance(value, int):
        int_val = value
    else:
        raise DomesticFuelSchemaError(
            f"Field {field_name} for {product_name} must be an integer, got {type(value).__name__}"
        )

    if int_val <= 0:
        raise DomesticFuelSchemaError(
            f"Field {field_name} for {product_name} must be a positive integer, got {int_val}"
        )
    return int_val


def _validate_optional_region_price(value: Any, field_name: str, product_name: str) -> int | None:
    """Validate regional price: allows None (null), but if present must be a positive integer."""
    if value is None:
        return None
    return _validate_positive_int(value, field_name, product_name)


def _validate_api_change(value: Any, product_name: str) -> int | None:
    """Validate change field if present in API response, accepting ^[+-]?\\d+$ strings or integers."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise DomesticFuelSchemaError(f"Change for {product_name} cannot be a boolean")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise DomesticFuelSchemaError(f"Change for {product_name} cannot be NaN or Infinity")
        if not value.is_integer():
            raise DomesticFuelSchemaError(
                f"Change for {product_name} cannot be a non-integer float: {value}"
            )
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if not CHANGE_REGEX.match(value):
            raise DomesticFuelSchemaError(
                f"Invalid change string format for {product_name}: {value!r}"
            )
        return int(value)
    raise DomesticFuelSchemaError(
        f"Invalid change type for {product_name}: {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# API Parsing and Validation
# ---------------------------------------------------------------------------


def _parse_api_payload(payload: Any) -> tuple[date, list[dict[str, Any]], dict[str, Any]]:
    """Validate and parse the VietFuel API response.

    Returns:
        (price_date, price_records, meta_dict)
    """
    if not isinstance(payload, dict):
        raise DomesticFuelSchemaError("Response payload must be a JSON object")

    # 1. Status and success flags
    if payload.get("success") is not True:
        raise DomesticFuelAPIError(
            "API reported success=False",
            category="source_unavailable",
        )

    status = str(payload.get("status", "")).strip().lower()
    if status != "ok":
        raise DomesticFuelAPIError(
            f"Unexpected API status: {payload.get('status')!r}",
            category="source_unavailable",
        )

    # 2. Metadata validation
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise DomesticFuelSchemaError("Missing or invalid 'meta' object in response")

    # Validate blockedByProtection: must be an explicit boolean
    if "blockedByProtection" not in meta:
        raise DomesticFuelSchemaError("Missing 'blockedByProtection' in meta")
    blocked = meta["blockedByProtection"]
    if not isinstance(blocked, bool):
        raise DomesticFuelSchemaError(
            f"meta.blockedByProtection must be a boolean, got {type(blocked).__name__}: {blocked!r}"
        )
    if blocked is True:
        raise DomesticFuelAPIError(
            "API request was blocked by provider protection (blockedByProtection=True)",
            category="source_unavailable",
        )

    # Validate isStale: must be an explicit boolean
    if "isStale" not in meta:
        raise DomesticFuelSchemaError("Missing 'isStale' in meta")
    is_stale = meta["isStale"]
    if not isinstance(is_stale, bool):
        raise DomesticFuelSchemaError(
            f"meta.isStale must be a boolean, got {type(is_stale).__name__}: {is_stale!r}"
        )
    if is_stale is True:
        raise DomesticFuelAPIError(
            "API returned stale fuel price data (isStale=True)",
            category="stale_data",
        )

    meta_source = meta.get("source")
    if not meta_source or not isinstance(meta_source, str) or not meta_source.strip():
        raise DomesticFuelSchemaError(
            f"Missing or empty meta.source in metadata: {meta_source!r}"
        )

    price_date_str = meta.get("priceDate")
    if not price_date_str or not isinstance(price_date_str, str):
        raise DomesticFuelDateError(
            f"Missing or invalid meta.priceDate: {price_date_str!r}",
            category="schema_error",
        )

    try:
        price_date = date.fromisoformat(price_date_str)
    except ValueError as exc:
        raise DomesticFuelDateError(
            f"Invalid ISO format for meta.priceDate: {price_date_str!r}",
            category="schema_error",
        ) from exc

    # Enforce priceDate is not in the future relative to Vietnam time
    vn_today = _today_vn()
    if price_date > vn_today:
        raise DomesticFuelDateError(
            f"meta.priceDate is in the future in Vietnam timezone: {price_date.isoformat()} > {vn_today.isoformat()}",
            category="future_date",
        )

    # 3. Product items validation
    data = payload.get("data")
    if not isinstance(data, list):
        raise DomesticFuelSchemaError("Missing or invalid 'data' list in response")

    # totalItems must be a real integer (reject float or bool)
    total_items = meta.get("totalItems")
    if total_items is None or isinstance(total_items, bool) or not isinstance(total_items, int):
        raise DomesticFuelSchemaError(
            f"meta.totalItems must be an integer, got {type(total_items).__name__}: {total_items!r}"
        )

    if total_items != len(data):
        raise DomesticFuelSchemaError(
            f"meta.totalItems ({total_items}) does not match len(data) ({len(data)})"
        )

    if len(data) != len(EXPECTED_PRODUCTS):
        raise DomesticFuelSchemaError(
            f"Expected exactly {len(EXPECTED_PRODUCTS)} products in data, got {len(data)}"
        )

    records: list[dict[str, Any]] = []
    seen_products: set[str] = set()

    for item in data:
        if not isinstance(item, dict):
            raise DomesticFuelSchemaError("Item in data must be a JSON dictionary")

        raw_name = item.get("name")
        if not raw_name or not isinstance(raw_name, str):
            raise DomesticFuelSchemaError("Item in data missing 'name'")

        canonical_name = normalize_api_product_name(raw_name)
        if canonical_name is None or canonical_name not in EXPECTED_PRODUCTS:
            raise DomesticFuelSchemaError(f"Unknown or unexpected product in data: {raw_name!r}")

        if canonical_name in seen_products:
            raise DomesticFuelSchemaError(f"Duplicate product in data: {canonical_name}")
        seen_products.add(canonical_name)

        # Validate price: positive integer in VND/lít
        price_int = _validate_positive_int(item.get("price"), "price", canonical_name)

        # Regional prices: null must NOT be coerced to 0
        region1_val = _validate_optional_region_price(item.get("region1"), "region1", canonical_name)
        region2_val = _validate_optional_region_price(item.get("region2"), "region2", canonical_name)

        # Change: validate if provided
        change_val = _validate_api_change(item.get("change"), canonical_name)

        records.append(
            {
                "product_name": canonical_name,
                "price": price_int,
                "region1": region1_val,
                "region2": region2_val,
                "unit": item.get("unit", "VND/lít"),
                "change": change_val,
            }
        )

    if seen_products != EXPECTED_PRODUCTS:
        missing = sorted(EXPECTED_PRODUCTS - seen_products)
        raise DomesticFuelSchemaError(f"Missing expected products in data: {missing}")

    return price_date, records, meta


# ---------------------------------------------------------------------------
# HTTP Fetching with Retries
# ---------------------------------------------------------------------------


def _fetch_api_with_retries(session: requests.Session | None = None) -> Any:
    """Fetch JSON response from VietFuel API with bounded retries for transient failures."""
    endpoint = _api_endpoint()
    req_timeout = _request_timeout()
    connect_timeout = min(10.0, req_timeout)
    read_timeout = req_timeout
    retry_count, retry_delay = _retry_settings()

    close_session = False
    if session is None:
        session = requests.Session()
        close_session = True

    try:
        for attempt in range(retry_count + 1):
            start_time = time.perf_counter()
            try:
                response = session.get(
                    endpoint,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (compatible; PetroTwin-DataCollector/1.0; "
                            "+https://github.com/dmins06/PetroTwin-)"
                        ),
                        "Accept": "application/json",
                    },
                    timeout=(connect_timeout, read_timeout),
                    verify=True,
                )
                duration = time.perf_counter() - start_time

                if response.status_code >= 500:
                    if attempt < retry_count:
                        _log(
                            "fetch_retryable_error",
                            attempt=attempt + 1,
                            status_code=response.status_code,
                            duration_seconds=round(duration, 3),
                        )
                        time.sleep(retry_delay)
                        continue
                    raise DomesticFuelFetchError(
                        f"VietFuel API HTTP server error: {response.status_code}",
                        status_code=response.status_code,
                        deterministic=False,
                    )

                if response.status_code >= 400:
                    raise DomesticFuelFetchError(
                        f"VietFuel API HTTP client error: {response.status_code}",
                        status_code=response.status_code,
                        deterministic=True,
                    )

                try:
                    payload = response.json()
                except Exception as exc:
                    raise DomesticFuelSchemaError(
                        f"Failed to decode JSON from VietFuel API: {exc}"
                    ) from exc

                _log(
                    "fetch_done",
                    status_code=response.status_code,
                    duration_seconds=round(duration, 3),
                    response_size=len(response.content),
                    api_provider=API_PROVIDER,
                )
                return payload

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                duration = time.perf_counter() - start_time
                if attempt < retry_count:
                    _log(
                        "fetch_retryable_error",
                        attempt=attempt + 1,
                        error=str(exc),
                        duration_seconds=round(duration, 3),
                    )
                    time.sleep(retry_delay)
                    continue
                raise DomesticFuelFetchError(
                    f"Network error connecting to VietFuel API: {exc}",
                    deterministic=False,
                ) from exc

    finally:
        if close_session:
            session.close()

    raise DomesticFuelFetchError("VietFuel API fetch exhausted retries without a response")


# ---------------------------------------------------------------------------
# CSV Build & Append
# ---------------------------------------------------------------------------


def _build_csv_rows(
    price_date: date,
    price_records: list[dict[str, Any]],
    extracted_at: str,
    last_prices: dict[str, int] | None = None,
) -> list[dict[str, str]]:
    """Build canonical CSV rows matching the petrotwin-data-v3 schema."""
    today_str = _today_vn().isoformat()
    eff_date_str = price_date.isoformat()
    rows: list[dict[str, str]] = []
    last_prices = last_prices or {}

    for record in price_records:
        product = str(record["product_name"])
        price = int(record["price"])

        # Determine price change from API change field, or from previous historical data if not provided directly
        change_val = record.get("change")
        if change_val is not None:
            change_vnd = str(change_val)
            change_display = _format_change_display(change_val)
        elif product in last_prices:
            change = price - last_prices[product]
            change_vnd = str(change)
            change_display = _format_change_display(change)
        else:
            change_vnd = ""
            change_display = ""

        rows.append(
            {
                "date": today_str,
                "effective_date": eff_date_str,
                "effective_time": DEFAULT_EFFECTIVE_TIME,
                "product_name": product,
                "price_vnd_per_liter": str(price),
                "price_display": _format_price_display(price),
                "price_change_vnd": change_vnd,
                "price_change_display": change_display,
                "unit": UNIT,
                "includes_vat_and_environmental_tax": INCLUDES_VAT,
                "source_url": SOURCE_URL,
                "extracted_at_utc": extracted_at,
            }
        )
    return rows


def _append_to_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    """Atomically append *rows* to *path* with strict header validation."""
    if not rows:
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    file_has_data = False
    existing_content = ""

    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as src:
            existing_content = src.read()

        stripped_content = existing_content.strip()
        if stripped_content:
            # File exists and has data -> validate header matches COLUMNS exactly
            reader = csv.reader(stripped_content.splitlines())
            try:
                first_row = next(reader)
            except StopIteration:
                first_row = []

            if first_row != COLUMNS:
                raise DomesticFuelSchemaError(
                    f"CSV header in {path} does not match expected COLUMNS.\n"
                    f"Expected: {COLUMNS}\n"
                    f"Found:    {first_row}"
                )
            file_has_data = True

    fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=path.stem + "_tmp_", suffix=".csv"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            if file_has_data:
                fh.write(existing_content)
                if not existing_content.endswith("\n"):
                    fh.write("\n")
                writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
                writer.writerows(rows)
            else:
                # File did not exist or was empty -> write header then rows
                writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)

        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Public Entry Point
# ---------------------------------------------------------------------------


def run() -> int:
    """Collect PVOIL domestic fuel prices via VietFuel API and append to CSV.

    Returns:
        Number of new rows added (0 or 4).

    Raises:
        SystemExit: on API failure, validation failure, or I/O error.
    """
    with file_lock("domestic_fuel") as acquired:
        if not acquired:
            _log(
                "lock_busy",
                collector="domestic_fuel",
                reason="Another process holds the domestic_fuel lock; skipping execution",
            )
            return 0

        csv_path = _csv_path()

        # Pre-flight check: ensure existing CSV has a valid header before making network calls
        try:
            _check_csv_header(csv_path)
        except DomesticFuelError as exc:
            _log(
                "preflight_error",
                error=str(exc),
                category=exc.category,
                path=str(csv_path),
                deterministic=exc.deterministic,
            )
            sys.exit(1)

        last_date = _last_date_in_csv(csv_path)
        last_prices = _last_prices_by_product(csv_path)
        endpoint = _api_endpoint()

        _log(
            "collector_start",
            source=SOURCE_URL,
            api_endpoint=endpoint,
            api_provider=API_PROVIDER,
            collection_method=COLLECTION_METHOD,
            last_date=str(last_date) if last_date else None,
        )

        extracted_at = _utcnow_iso()

        # ---- Fetch API ---------------------------------------------------------
        try:
            payload = _fetch_api_with_retries()
        except DomesticFuelError as exc:
            _log(
                "fetch_error",
                error=str(exc),
                category=exc.category,
                status_code=exc.status_code,
                deterministic=exc.deterministic,
            )
            sys.exit(1)
        except Exception as exc:
            _log(
                "fetch_error",
                error=str(exc),
                category="connection_error",
                deterministic=False,
            )
            sys.exit(1)

        # ---- Parse & Validate API ----------------------------------------------
        try:
            price_date, price_records, meta = _parse_api_payload(payload)
        except DomesticFuelError as exc:
            _log(
                "parse_error",
                error=str(exc),
                category=exc.category,
                status_code=exc.status_code,
                deterministic=exc.deterministic,
            )
            sys.exit(1)
        except Exception as exc:
            _log(
                "parse_error",
                error=str(exc),
                category="parsing_failure",
                deterministic=True,
            )
            sys.exit(1)

        _log(
            "parse_done",
            price_date=price_date.isoformat(),
            scraped_at=meta.get("scrapedAt"),
            cache_hit=meta.get("cacheHit"),
            products_found=len(price_records),
            api_provider=API_PROVIDER,
        )

        # ---- Idempotency check: only append if newer than last CSV date --------
        if last_date is not None and price_date <= last_date:
            _log(
                "up_to_date",
                price_date=price_date.isoformat(),
                last_date=str(last_date),
            )
            return 0

        csv_rows = _build_csv_rows(price_date, price_records, extracted_at, last_prices)

        if not csv_rows:
            _log("no_new_rows")
            return 0

        # ---- Atomic write to CSV -----------------------------------------------
        try:
            _append_to_csv(csv_path, csv_rows)
        except DomesticFuelError as exc:
            _log(
                "write_error",
                error=str(exc),
                category=exc.category,
                path=str(csv_path),
                deterministic=exc.deterministic,
            )
            sys.exit(1)
        except OSError as exc:
            _log("write_error", error=str(exc), path=str(csv_path))
            sys.exit(1)

        _log(
            "collector_done",
            price_date=price_date.isoformat(),
            new_rows=len(csv_rows),
            path=str(csv_path),
        )
        return len(csv_rows)


def check_only() -> int:
    """Fetch and validate VietFuel API without accessing local data files."""
    _log("check_start", api_provider=API_PROVIDER)

    # Fetch API
    try:
        payload = _fetch_api_with_retries()
    except DomesticFuelError as exc:
        _log(
            "check_fetch_error",
            error=str(exc),
            category=exc.category,
            status_code=exc.status_code,
            deterministic=exc.deterministic,
        )
        sys.exit(1)
    except Exception as exc:
        _log(
            "check_fetch_error",
            error=str(exc),
            category="connection_error",
            deterministic=False,
        )
        sys.exit(1)

    # Parse and validate
    try:
        price_date, price_records, meta = _parse_api_payload(payload)
    except DomesticFuelError as exc:
        _log(
            "check_parse_error",
            error=str(exc),
            category=exc.category,
            status_code=exc.status_code,
            deterministic=exc.deterministic,
        )
        sys.exit(1)
    except Exception as exc:
        _log(
            "check_parse_error",
            error=str(exc),
            category="parsing_failure",
            deterministic=True,
        )
        sys.exit(1)

    # Print validated API response
    result = {
        "status": "success",
        "mode": "check-only",
        "api_provider": API_PROVIDER,
        "root_source": "PVOIL",
        "price_date": price_date.isoformat(),
        "scraped_at": meta.get("scrapedAt"),
        "cache_hit": meta.get("cacheHit"),
        "products_found": len(price_records),
        "products": price_records,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    _log(
        "check_done",
        price_date=price_date.isoformat(),
        products_found=len(price_records),
        api_provider=API_PROVIDER,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PVOIL domestic fuel price collector"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Fetch and validate VietFuel API without reading or writing CSV",
    )
    args = parser.parse_args()

    if args.check_only:
        check_only()
    else:
        run()

    sys.exit(0)


if __name__ == "__main__":
    main()
