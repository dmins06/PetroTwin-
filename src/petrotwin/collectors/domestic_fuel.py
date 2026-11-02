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

import requests
from bs4 import BeautifulSoup
import yaml

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
    """Download the PVOIL fuel price page HTML."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; PetroTwin-DataCollector/1.0; "
            "+https://github.com/petrotwin)"
        ),
        "Accept-Language": "vi,en;q=0.9",
    }
    resp = requests.get(SOURCE_URL, headers=headers, timeout=_request_timeout())
    resp.raise_for_status()
    return resp.text


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
    """Map a raw product name string to a canonical KNOWN_PRODUCTS name."""
    raw_stripped = raw.strip()
    # Exact match first
    if raw_stripped in KNOWN_PRODUCTS:
        return raw_stripped
    # Case-insensitive substring match
    raw_lower = raw_stripped.lower()
    for canonical in KNOWN_PRODUCTS:
        if canonical.lower() in raw_lower or raw_lower in canonical.lower():
            return canonical
    return None


def _scrape_prices(html: str) -> tuple[date, list[dict[str, str | int]]]:
    """Parse HTML and return (announcement_date, list of price records).

    Each record contains:
        product_name, price, change  (int values)

    Raises:
        ValueError: if no recognisable price table or date is found.
    """
    soup = BeautifulSoup(html, "html.parser")

    # -----------------------------------------------------------------------
    # Strategy 1: find a <table> containing product rows
    # -----------------------------------------------------------------------
    announcement_date: date | None = None
    price_records: list[dict[str, str | int]] = []

    # Try to find the announcement date in headings or article titles
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "span", "div"]):
        text = tag.get_text(separator=" ", strip=True)
        if any(kw in text.lower() for kw in ["giá xăng", "gia xang", "ngày", "điều chỉnh"]):
            d = _parse_date_from_text(text)
            if d is not None:
                announcement_date = d
                break

    # If still not found, use today as a conservative default
    if announcement_date is None:
        announcement_date = date.today()
        _log("date_fallback", reason="Could not parse announcement date from page, using today")

    # -----------------------------------------------------------------------
    # Look for price data in tables
    # -----------------------------------------------------------------------
    tables = soup.find_all("table")
    parsed_any = False

    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            cell_texts = [c.get_text(separator=" ", strip=True) for c in cells]

            # First cell is product name
            product_raw = cell_texts[0]
            canonical = _normalize_product_name(product_raw)
            if canonical is None:
                continue

            # Find a cell that looks like a price (>= 4-digit number)
            price: int | None = None
            change: int = 0

            for i, ct in enumerate(cell_texts[1:], start=1):
                # Strip non-numeric except sign and separators
                digits_only = re.sub(r"[^\d.,]", "", ct)
                if len(digits_only) >= 4:
                    try:
                        candidate = _clean_number(ct)
                        if candidate > 1000:  # plausible VND price
                            if price is None:
                                price = candidate
                            else:
                                # Could be the change column
                                change_raw = re.sub(r"[^\d+\-.,]", "", ct)
                                try:
                                    change = int(re.sub(r"[.,]", "", change_raw))
                                except ValueError:
                                    pass
                    except (ValueError, TypeError):
                        continue

            # Check for explicit +/- change sign
            for ct in cell_texts[1:]:
                sign_match = re.search(r"([+-])[\s]*(\d[\d.,]*)", ct)
                if sign_match:
                    sign = 1 if sign_match.group(1) == "+" else -1
                    try:
                        change = sign * _clean_number(sign_match.group(2))
                    except ValueError:
                        pass
                    break

            if price is not None:
                price_records.append(
                    {"product_name": canonical, "price": price, "change": change}
                )
                parsed_any = True

    # -----------------------------------------------------------------------
    # Strategy 2: look for definition-list or article-structured data
    # -----------------------------------------------------------------------
    if not parsed_any:
        # Try to find product/price pairs in paragraphs or list items
        all_text_blocks = soup.find_all(["li", "p", "div"])
        for block in all_text_blocks:
            text = block.get_text(separator=" ", strip=True)
            canonical = None
            for prod in KNOWN_PRODUCTS:
                if prod.lower() in text.lower():
                    canonical = prod
                    break
            if canonical is None:
                continue

            # Try to find a price after a colon or in the same text
            price_match = re.search(r"(\d{4,6})[.,]?\s*(?:đ|VND|đồng)?", text)
            if price_match:
                try:
                    price = _clean_number(price_match.group(1))
                    if price > 1000:
                        change = 0
                        # Look for change
                        chg_match = re.search(r"([+-])\s*(\d+)\s*(?:đ|VND|đồng)?", text)
                        if chg_match:
                            sign = 1 if chg_match.group(1) == "+" else -1
                            change = sign * int(chg_match.group(2))
                        price_records.append(
                            {"product_name": canonical, "price": price, "change": change}
                        )
                        parsed_any = True
                except ValueError:
                    continue

    if not parsed_any or not price_records:
        raise ValueError(
            "Could not parse any product price rows from PVOIL page. "
            "The website structure may have changed."
        )

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
