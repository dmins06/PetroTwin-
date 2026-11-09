"""Collector: Vietnamese domestic fuel prices from PVOIL.

Scrapes the latest retail fuel price announcement from
https://pvoil.com.vn/tin-gia-xang-dau and appends new rows to
``data/raw/data_price_petro.csv``.

Because PVOIL does not provide a public API, this module uses
``requests`` + ``beautifulsoup4`` to parse the HTML page.  If scraping
fails for any reason (changed markup, HTTP error, missing data) the
script logs a clear error and exits with code 1 — it never writes
partial data to the CSV.

Domestic fuel prices in Vietnam are typically revised on Thursdays at
15:00 ICT by the Ministry of Industry and Trade (MOIT).

Usage::

    python -m petrotwin.collectors.domestic_fuel

Environment variables:
    PETROTWIN_DATA_DIR — optional override for the ``data/raw`` directory.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence
import urllib3
import requests
from bs4 import BeautifulSoup
import yaml

from petrotwin.collectors.locks import file_lock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_URL = "https://pvoil.com.vn/tin-gia-xang-dau"
UNIT = "Đồng/lít thực tế"
INCLUDES_VAT = "true"
DEFAULT_EFFECTIVE_TIME = "15:00"

REQUEST_TIMEOUT = 30

# Canonical product names (must match pipeline.py PRODUCT_IDS keys)
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
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _raw_dir() -> Path:
    override = os.environ.get("PETROTWIN_DATA_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else (_repo_root() / path).resolve()
    return _repo_root() / "data" / "raw"


def _request_timeout() -> float:
    config_path = _repo_root() / "configs" / "collectors.yaml"
    try:
        with config_path.open(encoding="utf-8") as stream:
            settings = (yaml.safe_load(stream) or {}).get("domestic_fuel", {})
        return max(1.0, float(settings.get("request_timeout_seconds", REQUEST_TIMEOUT)))
    except (AttributeError, OSError, TypeError, ValueError, yaml.YAMLError):
        return REQUEST_TIMEOUT


def _csv_path() -> Path:
    return _raw_dir() / "data_price_petro.csv"


def _last_date_in_csv(path: Path) -> date | None:
    if not path.exists():
        return None
    last: date | None = None
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                d = date.fromisoformat(row["date"].strip())
                if last is None or d > last:
                    last = d
            except (KeyError, ValueError):
                continue
    return last


def _format_price_display(price: int) -> str:
    """Format integer price with dot thousands separator: 25100 → '25.100 đ'."""
    # Use Vietnamese convention: dot as thousands separator
    formatted = f"{price:,}".replace(",", ".")
    return f"{formatted} đ"


def _format_change_display(change: int) -> str:
    """Format price change: +500 → '+500 đ', -300 → '-300 đ', 0 → ''."""
    if change == 0:
        return ""
    sign = "+" if change > 0 else ""
    return f"{sign}{change} đ"


def _clean_number(text: str) -> int:
    """Strip whitespace, dots, commas from a Vietnamese number string and parse."""
    cleaned = re.sub(r"[\s.,]", "", text.strip())
    cleaned = cleaned.replace("đ", "").replace("VND", "").strip()
    return int(cleaned)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _fetch_html() -> str:
    """Download PVOIL fuel prices through the verified origin connection."""
    pool = urllib3.HTTPSConnectionPool(
        "103.21.120.100",
        server_hostname="www.pvoil.com.vn",
        assert_hostname="www.pvoil.com.vn",
        timeout=urllib3.Timeout(total=_request_timeout()),
    )

    try:
        response = pool.request(
            "GET",
            "/api/oilprice/load-view",
            headers={
                "Host": "www.pvoil.com.vn",
                "User-Agent": (
                    "Mozilla/5.0 (compatible; PetroTwin-DataCollector/1.0; "
                    "+https://github.com/dmins06/PetroTwin-)"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "vi,en;q=0.9",
                "Referer": "https://www.pvoil.com.vn/",
            },
            retries=False,
        )

        if response.status >= 400:
            error_response = requests.Response()
            error_response.status_code = response.status
            error_response.url = (
                "https://www.pvoil.com.vn/api/oilprice/load-view"
            )
            error_response._content = response.data
            error_response.reason = "PVOIL origin HTTP error"
            error_response.encoding = "utf-8"
            error_response.raise_for_status()

        return response.data.decode("utf-8", errors="replace")

    except urllib3.exceptions.HTTPError as exc:
        raise requests.RequestException(
            f"PVOIL origin connection failed: {exc}"
        ) from exc

    finally:
        pool.close()


def _parse_date_from_text(text: str) -> date | None:
    """Try to extract a date (dd/mm/yyyy or yyyy-mm-dd) from a string."""
    # Vietnamese format: ngày dd/mm/yyyy
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if m:
        try:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            # Validate that we have d/m/y order (day <= 31, month <= 12)
            if day <= 31 and month <= 12:
                return date(year, month, day)
        except ValueError:
            pass
    # ISO format
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m2:
        try:
            return date.fromisoformat(m2.group(0))
        except ValueError:
            pass
    return None


def _parse_effective_time(text: str) -> str:
    """Extract HH:MM from text, defaulting to DEFAULT_EFFECTIVE_TIME."""
    m = re.search(r"(\d{1,2})[hH:](\d{2})", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        return f"{hour:02d}:{minute:02d}"
    return DEFAULT_EFFECTIVE_TIME


def _normalize_product_name(raw: str) -> str | None:
    """Map a product name to a canonical KNOWN_PRODUCTS entry."""
    normalized = " ".join(raw.strip().split()).casefold()

    for canonical in KNOWN_PRODUCTS:
        if normalized == " ".join(canonical.split()).casefold():
            return canonical

    return None


def _scrape_prices(html: str) -> tuple[date, list[dict[str, str | int]]]:
    """Parse PVOIL price table and return its effective date and records."""
    soup = BeautifulSoup(html, "html.parser")
    announcement_date: date | None = None
    price_records: list[dict[str, str | int]] = []

    # Extract date from page headings first.
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        announcement_date = _parse_date_from_text(
            tag.get_text(separator=" ", strip=True)
        )
        if announcement_date is not None:
            break

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        table_text = table.get_text(separator=" ", strip=True)

        if announcement_date is None:
            announcement_date = _parse_date_from_text(table_text)

        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue

            cell_texts = [
                cell.get_text(separator=" ", strip=True)
                for cell in cells
            ]

            # Support both:
            # product | price | change
            # index | product | price | change
            product_idx = 1 if cell_texts[0].strip().isdigit() else 0
            price_idx = product_idx + 1
            change_idx = product_idx + 2

            if change_idx >= len(cell_texts):
                continue

            product_name = _normalize_product_name(
                cell_texts[product_idx]
            )
            if product_name is None:
                continue

            try:
                price = _clean_number(cell_texts[price_idx])
            except (ValueError, TypeError):
                continue

            if price <= 1000:
                continue

            change_raw = re.sub(
                r"[^\d+-]", "", cell_texts[change_idx]
            )
            change = 0
            if change_raw and change_raw not in ("+", "-"):
                try:
                    change = int(change_raw)
                except ValueError:
                    pass

            price_records.append({
                "product_name": product_name,
                "price": price,
                "change": change,
            })

    # Check price rows first so an unavailable/changed page fails clearly.
    if not price_records:
        raise ValueError(
            "Could not parse any product price rows from PVOIL table."
        )

    if announcement_date is None:
        raise ValueError(
            "Could not extract PVOIL effective date from price table."
        )

    product_names = [
        record["product_name"] for record in price_records
    ]
    if len(product_names) != len(set(product_names)):
        raise ValueError("Duplicate product rows found in PVOIL table.")

    return announcement_date, price_records

# ---------------------------------------------------------------------------
# CSV write
# ---------------------------------------------------------------------------

def _build_csv_rows(
    announcement_date: date,
    price_records: list[dict[str, str | int]],
    extracted_at: str,
) -> list[dict[str, str]]:
    today_str = date.today().isoformat()
    eff_date_str = announcement_date.isoformat()
    rows: list[dict[str, str]] = []
    for record in price_records:
        product = str(record["product_name"])
        price = int(record["price"])  # type: ignore[arg-type]
        change = int(record["change"])  # type: ignore[arg-type]
        rows.append(
            {
                "date": today_str,
                "effective_date": eff_date_str,
                "effective_time": DEFAULT_EFFECTIVE_TIME,
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
    """Atomically append *rows* to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=path.stem + "_tmp_", suffix=".csv"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            if path.exists():
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
# Public entry point
# ---------------------------------------------------------------------------

def run() -> int:
    """Scrape PVOIL and append new fuel price rows to CSV.

    Returns:
        Number of new rows added.

    Raises:
        SystemExit: on scraping failure, HTTP error, or I/O failure.
    """
    with file_lock("domestic_fuel") as acquired:
        if not acquired:
            _log("lock_busy", collector="domestic_fuel", reason="Another process holds the domestic_fuel lock; skipping execution")
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
                error=str(exc),
                status_code=exc.response.status_code if exc.response is not None else None,
            )
            sys.exit(1)
        except requests.RequestException as exc:
            _log("fetch_error", error=str(exc))
            sys.exit(1)

        # ---- Parse page --------------------------------------------------------
        try:
            announcement_date, price_records = _scrape_prices(html)
        except Exception as exc:
            _log("parse_error", error=str(exc))
            sys.exit(1)

        _log(
            "parse_done",
            announcement_date=announcement_date.isoformat(),
            products_found=len(price_records),
        )

        # ---- Filter: only rows newer than last CSV date -------------------------
        if last_date is not None and announcement_date <= last_date:
            _log(
                "up_to_date",
                announcement_date=announcement_date.isoformat(),
                last_date=str(last_date),
            )
            return 0

        csv_rows = _build_csv_rows(announcement_date, price_records, extracted_at)

        if not csv_rows:
            _log("no_new_rows")
            return 0

        # ---- Write (atomic) ----------------------------------------------------
        try:
            _append_to_csv(csv_path, csv_rows)
        except OSError as exc:
            _log("write_error", error=str(exc), path=str(csv_path))
            sys.exit(1)

        _log(
            "collector_done",
            announcement_date=announcement_date.isoformat(),
            new_rows=len(csv_rows),
            path=str(csv_path),
        )
        return len(csv_rows)


def main() -> None:
    run()
    sys.exit(0)


if __name__ == "__main__":
    main()
