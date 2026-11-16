"""Collector: Vietnamese domestic fuel prices from COMECO.

Scrapes the latest retail fuel price announcement from https://comeco.vn/
and appends new rows to ``data/raw/comeco_fuel_prices.csv``.

The system collects and processes ONLY these four products:
- E10 RON95-III (Xăng E10 RON 95 Mức 3 -> Xăng E10 RON 95-III)
- E5 RON92-II   (Xăng E5 RON 92 Mức 2 -> Xăng E5 RON 92-II)
- DO 0,001S-V   (Dầu DO 0,001S Mức 5  -> Dầu DO 0,001S-V)
- DO 0,05S-II   (Dầu DO 0,05S Mức 2   -> Dầu DO 0,05S-II)

Important:
- Never map E10 RON 95 Mức 5 (E10 RON95-V) to E10 RON95-III.
- Reject/ignore all other fuel products.
- Requires all 4 target products to be present in the page; fails closed if any is missing.
- Fail closed if announcement date or time cannot be extracted.
- Atomic append to CSV using tempfile and os.replace under file_lock("comeco").
- Supports --check-only read-only diagnostic mode.

Usage::

    python -m petrotwin.collectors.comeco
    python -m petrotwin.collectors.comeco --check-only

Environment variables:
    PETROTWIN_DATA_DIR — optional override for the ``data/raw`` directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import requests
from bs4 import BeautifulSoup
import yaml

from petrotwin.collectors.locks import file_lock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_URL = "https://comeco.vn/"
UNIT = "Đồng/lít thực tế"
INCLUDES_VAT = "true"

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_RETRY_COUNT = 2
DEFAULT_RETRY_DELAY = 5.0

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

# Strict whitelist mapping: normalized raw name -> canonical product name.
# E10 RON 95 Mức 5 is explicitly excluded and must NEVER be mapped to E10 RON95-III.
PRODUCT_MAPPING: dict[str, str] = {
    "xăng e10 ron 95 mức 3": "Xăng E10 RON 95-III",
    "xăng e5 ron 92 mức 2": "Xăng E5 RON 92-II",
    "dầu do 0,001s mức 5": "Dầu DO 0,001S-V",
    "dầu do 0,05s mức 2": "Dầu DO 0,05S-II",
}

EXCLUDED_PRODUCTS: frozenset[str] = frozenset([
    "xăng e10 ron 95 mức 5",
])

TARGET_CANONICAL_PRODUCTS: frozenset[str] = frozenset(PRODUCT_MAPPING.values())

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _log(event: str, **kwargs: object) -> None:
    record = {"event": event, "ts": _utcnow_iso(), **kwargs}
    log.info(json.dumps(record, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


COMECO_RAW_FILENAME = "comeco_fuel_prices.csv"


def _raw_dir() -> Path:
    override = os.environ.get("PETROTWIN_DATA_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else (_repo_root() / path).resolve()
    return _repo_root() / "data" / "raw"


def _csv_path() -> Path:
    return _raw_dir() / COMECO_RAW_FILENAME


def _load_collector_config() -> dict[str, object]:
    config_path = _repo_root() / "configs" / "collectors.yaml"
    try:
        with config_path.open(encoding="utf-8") as stream:
            return (yaml.safe_load(stream) or {}).get("comeco", {})
    except (AttributeError, OSError, TypeError, ValueError, yaml.YAMLError):
        return {}


def _request_timeout() -> float:
    cfg = _load_collector_config()
    return max(1.0, float(cfg.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT)))


def _retry_settings() -> tuple[int, float]:
    cfg = _load_collector_config()
    count = max(0, int(cfg.get("retry_count", DEFAULT_RETRY_COUNT)))
    delay = max(0.0, float(cfg.get("retry_delay_seconds", DEFAULT_RETRY_DELAY)))
    return count, delay


def _get_max_age_days() -> int:
    cfg = _load_collector_config()
    try:
        val = cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
        return max(0, int(val))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS


def _check_freshness(eff_date: date, today: date | None = None) -> tuple[bool, int]:
    """Check if announced date is stale relative to current adjustment cycle.

    Returns:
        (is_stale, days_since_announcement)
    """
    current_date = today or date.today()
    days_since = (current_date - eff_date).days
    max_days = _get_max_age_days()
    is_stale = days_since > max_days
    return is_stale, days_since


def _format_price_display(price: int) -> str:
    """Format integer price with dot thousands separator: 20140 → '20.140 đ'."""
    formatted = f"{price:,}".replace(",", ".")
    return f"{formatted} đ"


def _format_change_display(change: int) -> str:
    """Format price change: +400 → '+400', -1810 → '-1810', 0 → '0'."""
    if change == 0:
        return "0"
    sign = "+" if change > 0 else ""
    return f"{sign}{change}"


def _clean_number(text: str) -> int:
    """Strip whitespace, dots, currency symbols and parse integer."""
    cleaned = re.sub(r"[\s.,]", "", text.strip())
    cleaned = cleaned.replace("đ", "").replace("VND", "").replace("₫", "").strip()
    return int(cleaned)


def _normalize_name_for_lookup(name: str) -> str:
    """Normalize product name for matching against historical records."""
    s = re.sub(r"\bRON\s+(\d+)", r"RON\1", name.strip(), flags=re.IGNORECASE)
    return " ".join(s.split()).casefold()


# ---------------------------------------------------------------------------
# Scraping & Semantic Parsing
# ---------------------------------------------------------------------------

def _fetch_html(
    session: requests.Session | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    retry_delay: float | None = None,
) -> str:
    """Download COMECO homepage HTML with TLS verification and bounded retries."""
    if timeout is None:
        timeout = _request_timeout()
    if retries is None or retry_delay is None:
        default_retries, default_delay = _retry_settings()
        if retries is None:
            retries = default_retries
        if retry_delay is None:
            retry_delay = default_delay

    s = session or requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; PetroTwin-COMECOCollector/1.0; "
            "+https://github.com/dmins06/PetroTwin-)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en;q=0.9",
    }

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = s.get(SOURCE_URL, headers=headers, timeout=timeout, verify=True)
            # Fast fail on 4xx client errors without retrying
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()
            # 5xx server errors trigger retries
            if resp.status_code >= 500:
                resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        except requests.HTTPError as exc:
            last_exc = exc
            if exc.response is not None and 400 <= exc.response.status_code < 500:
                raise
            if attempt < retries:
                time.sleep(retry_delay)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_delay)

    if last_exc:
        raise last_exc
    raise requests.RequestException("Unknown network error fetching COMECO")


def _parse_comeco_html(
    html: str, current_date: date | None = None
) -> tuple[date, str, list[dict[str, object]]]:
    """Parse COMECO HTML to extract effective date, time, and exactly the 4 target products.

    Returns:
        (announcement_date, effective_time, price_records)
        where price_records is a list of dicts:
        [{"product_name": canonical_name, "price": int}, ...]

    Raises:
        ValueError: if the structure is invalid, date is missing/invalid,
                    effective date is in the future,
                    or any of the 4 target products is missing, or duplicate/invalid.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. Semantic anchor: find section title "Giá bán lẻ xăng dầu"
    title_elem = soup.find(string=re.compile(r"Giá\s+bán\s+lẻ\s+xăng\s+dầu", re.I))
    if not title_elem:
        raise ValueError("Could not find 'Giá bán lẻ xăng dầu' section header in COMECO page.")

    # 2. Locate container (div.col-inner or closest semantic container)
    container = title_elem.find_parent("div", class_="col-inner")
    if not container:
        container = title_elem.find_parent(["section", "div"])
    if not container:
        raise ValueError("Could not find pricing section container in COMECO page.")

    container_text = container.get_text(separator=" ", strip=True)

    # 3. Extract effective date and time
    eff_date: date | None = None
    eff_time: str | None = None

    dt_match = re.search(r"(\d{1,2}:\d{2})\s+ngày\s+(\d{1,2})[/-](\d{1,2})[/-](\d{4})", container_text)
    if dt_match:
        eff_time = dt_match.group(1)
        day, month, year = int(dt_match.group(2)), int(dt_match.group(3)), int(dt_match.group(4))
        if 1 <= day <= 31 and 1 <= month <= 12:
            try:
                eff_date = date(year, month, day)
            except ValueError:
                pass
    else:
        d_match = re.search(r"ngày\s+(\d{1,2})[/-](\d{1,2})[/-](\d{4})", container_text)
        if d_match:
            day, month, year = int(d_match.group(1)), int(d_match.group(2)), int(d_match.group(3))
            if 1 <= day <= 31 and 1 <= month <= 12:
                try:
                    eff_date = date(year, month, day)
                except ValueError:
                    pass
        t_match = re.search(r"(\d{1,2})[:hH](\d{2})", container_text)
        if t_match:
            eff_time = f"{int(t_match.group(1)):02d}:{int(t_match.group(2)):02d}"

    if eff_date is None:
        raise ValueError("Could not extract effective date from COMECO pricing section.")
    if eff_time is None:
        raise ValueError("Could not extract effective time from COMECO pricing section.")

    today = current_date or date.today()
    if eff_date > today:
        raise ValueError(
            f"Effective date {eff_date.isoformat()} is in the future relative to current date {today.isoformat()}."
        )

    # 4. Extract product cards
    cards = container.find_all("div", class_="col")
    if not cards:
        headings = container.find_all(["h5", "h4", "h6"])
        cards = [h.parent for h in headings if h.parent is not None]

    found_products: dict[str, int] = {}

    for card in cards:
        h_name = card.find(["h5", "h4", "h6"])
        h_price = card.find(["h2", "h3", "span"])
        if not h_name or not h_price:
            continue

        raw_name = h_name.get_text(separator=" ", strip=True)
        norm_name = " ".join(raw_name.strip().split()).casefold()

        # Exclude Mức 5 (E10 RON95-V) explicitly
        if norm_name in EXCLUDED_PRODUCTS:
            continue

        canonical_name = PRODUCT_MAPPING.get(norm_name)
        if not canonical_name:
            # Unknown or unmapped product
            continue

        if canonical_name in found_products:
            raise ValueError(f"Duplicate product card found for '{canonical_name}' in COMECO page.")

        price_text = h_price.get_text(strip=True)
        try:
            price = _clean_number(price_text)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid price value '{price_text}' for product '{canonical_name}'.") from exc

        if price <= 1000:
            raise ValueError(f"Price {price} for product '{canonical_name}' is suspiciously low or invalid.")

        found_products[canonical_name] = price

    # Strict verification: must contain exactly all 4 target products
    missing = TARGET_CANONICAL_PRODUCTS - set(found_products.keys())
    if missing:
        raise ValueError(f"Missing required target products from COMECO page: {sorted(missing)}")

    price_records = [
        {"product_name": name, "price": found_products[name]}
        for name in sorted(TARGET_CANONICAL_PRODUCTS)
    ]

    return eff_date, eff_time, price_records


# ---------------------------------------------------------------------------
# CSV Helpers
# ---------------------------------------------------------------------------

def _read_prices_from_csv(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    latest: dict[str, tuple[date, int]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            p_name = row.get("product_name", "").strip()
            eff_date_str = row.get("effective_date", "").strip() or row.get("date", "").strip()
            price_str = row.get("price_vnd_per_liter", "").strip()
            if not p_name or not eff_date_str or not price_str:
                continue
            try:
                eff_date = date.fromisoformat(eff_date_str)
                price = int(price_str)
            except (ValueError, TypeError):
                continue
            lookup_key = _normalize_name_for_lookup(p_name)
            if lookup_key not in latest or eff_date >= latest[lookup_key][0]:
                latest[lookup_key] = (eff_date, price)
    return {k: v[1] for k, v in latest.items()}


def _get_latest_prices_by_product(path: Path) -> dict[str, int]:
    """Read CSV to determine latest price for each product; fall back to historical file if needed."""
    prices = _read_prices_from_csv(path)
    if len(prices) < len(TARGET_CANONICAL_PRODUCTS):
        # Fall back to baseline historical file in same directory if available
        historical_path = path.parent / "data_price_petro.csv"
        if historical_path.is_file() and historical_path != path:
            hist_prices = _read_prices_from_csv(historical_path)
            for k, v in hist_prices.items():
                if k not in prices:
                    prices[k] = v
    return prices


def _last_effective_datetime_in_csv(path: Path) -> tuple[date, str] | None:
    if not path.is_file():
        return None
    latest: tuple[date, str] | None = None
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            d_str = row.get("effective_date", "").strip() or row.get("date", "").strip()
            t_str = row.get("effective_time", "").strip() or "00:00"
            if not d_str:
                continue
            try:
                d = date.fromisoformat(d_str)
                if latest is None or (d, t_str) > latest:
                    latest = (d, t_str)
            except ValueError:
                continue
    return latest


def _last_date_in_csv(path: Path) -> date | None:
    latest = _last_effective_datetime_in_csv(path)
    return latest[0] if latest else None


def _build_csv_rows(
    announcement_date: date,
    effective_time: str,
    price_records: list[dict[str, object]],
    extracted_at: str,
    previous_prices: dict[str, int],
) -> list[dict[str, str]]:
    today_str = date.today().isoformat()
    eff_date_str = announcement_date.isoformat()
    rows: list[dict[str, str]] = []

    for record in price_records:
        product = str(record["product_name"])
        price = int(record["price"])  # type: ignore[arg-type]
        lookup_key = _normalize_name_for_lookup(product)
        if lookup_key in previous_prices:
            change = price - previous_prices[lookup_key]
        else:
            change = 0

        rows.append(
            {
                "date": today_str,
                "effective_date": eff_date_str,
                "effective_time": effective_time,
                "product_name": product,
                "price_vnd_per_liter": str(price),
                "price_display": _format_price_display(price),
                "price_change_vnd": str(change),
                "price_change_display": _format_change_display(change),
                "unit": UNIT,
                "includes_vat_and_environmental_tax": INCLUDES_VAT,
                "source_url": SOURCE_URL,
                "extracted_at_utc": extracted_at,
            }
        )
    return rows


def _append_to_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    """Atomically append rows to path using tempfile and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=path.stem + "_tmp_", suffix=".csv"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            if path.is_file() and path.stat().st_size > 0:
                with path.open(newline="", encoding="utf-8") as src:
                    content = src.read()
                fh.write(content)
                if content and not content.endswith("\n"):
                    fh.write("\n")
            else:
                writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
                writer.writeheader()

            writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
            writer.writerows(rows)

        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Public Entry Points
# ---------------------------------------------------------------------------

def check_only(current_date: date | None = None) -> int:
    """Execute read-only validation check against COMECO without touching CSV or locks.

    Returns:
        0 if fetch and validation succeed, 1 on any failure.
    """
    try:
        html = _fetch_html()
        eff_date, eff_time, price_records = _parse_comeco_html(html, current_date=current_date)
        is_stale, days_since = _check_freshness(eff_date, today=current_date)
        result = {
            "event": "check_only_success",
            "source_url": SOURCE_URL,
            "effective_date": eff_date.isoformat(),
            "effective_time": eff_time,
            "products_count": len(price_records),
            "products": [
                {"product_name": r["product_name"], "price": r["price"]}
                for r in price_records
            ],
            "freshness": "stale" if is_stale else "fresh",
            "is_stale": is_stale,
            "days_since_announcement": days_since,
            "status": "valid",
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        result = {
            "event": "check_only_failure",
            "error": str(exc),
            "status": "invalid",
        }
        print(json.dumps(result, ensure_ascii=False))
        return 1


def run(current_date: date | None = None) -> int:
    """Scrape COMECO and append new fuel price rows to CSV.

    Returns:
        Number of new rows added (0 if up to date or lock busy).

    Raises:
        SystemExit: on scraping failure, HTTP error, or I/O failure.
    """
    with file_lock("comeco") as acquired:
        if not acquired:
            _log("lock_busy", collector="comeco", reason="Another process holds the comeco lock; skipping execution")
            return 0

        csv_path = _csv_path()
        last_date = _last_date_in_csv(csv_path)
        _log("collector_start", source=SOURCE_URL, last_date=str(last_date) if last_date else None)

        extracted_at = _utcnow_iso()

        # ---- Fetch HTML --------------------------------------------------------
        try:
            html = _fetch_html()
        except requests.HTTPError as exc:
            _log(
                "fetch_error",
                category="http_error",
                deterministic=(exc.response is not None and 400 <= exc.response.status_code < 500),
                error=str(exc),
                status_code=exc.response.status_code if exc.response is not None else None,
            )
            sys.exit(1)
        except requests.RequestException as exc:
            _log("fetch_error", category="network_error", error=str(exc))
            sys.exit(1)

        # ---- Parse page --------------------------------------------------------
        try:
            eff_date, eff_time, price_records = _parse_comeco_html(html, current_date=current_date)
        except Exception as exc:
            _log("parse_error", category="parsing_failure", deterministic=True, error=str(exc))
            sys.exit(1)

        # ---- Freshness validation ----------------------------------------------
        is_stale, days_since = _check_freshness(eff_date, today=current_date)
        if is_stale:
            _log(
                "source_stale",
                category="stale_data",
                deterministic=True,
                effective_date=eff_date.isoformat(),
                days_since_announcement=days_since,
                max_age_days=_get_max_age_days(),
                message="COMECO retail price announcement is older than expected adjustment period",
            )
            # Never write stale data as a new observation, even if newer than last CSV date
            return 0

        _log(
            "parse_done",
            effective_date=eff_date.isoformat(),
            effective_time=eff_time,
            products_found=len(price_records),
            is_stale=is_stale,
            days_since_announcement=days_since,
        )

        # ---- Idempotency check: not newer than last effective timestamp ---------
        last_eff = _last_effective_datetime_in_csv(csv_path)
        if last_eff is not None and (eff_date, eff_time) <= last_eff:
            _log(
                "up_to_date",
                effective_date=eff_date.isoformat(),
                effective_time=eff_time,
                last_effective=f"{last_eff[0].isoformat()} {last_eff[1]}",
                is_stale=is_stale,
                days_since_announcement=days_since,
                new_rows=0,
            )
            return 0

        prev_prices = _get_latest_prices_by_product(csv_path)
        csv_rows = _build_csv_rows(eff_date, eff_time, price_records, extracted_at, prev_prices)

        if not csv_rows:
            _log("no_new_rows", new_rows=0)
            return 0

        # ---- Write (atomic) ----------------------------------------------------
        try:
            _append_to_csv(csv_path, csv_rows)
        except OSError as exc:
            _log("write_error", error=str(exc), path=str(csv_path))
            sys.exit(1)

        _log(
            "collector_done",
            effective_date=eff_date.isoformat(),
            new_rows=len(csv_rows),
            path=str(csv_path),
        )
        return len(csv_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="COMECO domestic fuel price collector")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Read-only mode: fetches and validates COMECO HTML, prints JSON diagnostics without writing CSV or acquiring locks.",
    )
    args = parser.parse_args()

    if args.check_only:
        code = check_only()
        sys.exit(code)

    run()
    sys.exit(0)


if __name__ == "__main__":
    main()
