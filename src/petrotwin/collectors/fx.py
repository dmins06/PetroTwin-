"""Collector: USD/VND exchange rate from Vietcombank (SBV central reference).

Fetches the current USD/VND transfer rate from Vietcombank's public API
(which reflects the State Bank of Vietnam central reference rate) and appends
it to ``data/raw/usd_vnd_rates.csv``.

The SBV rate is published once per working day.  This script is idempotent:
if today's date is already present in the CSV it exits cleanly without writing
anything.  It also skips weekends.

Usage::

    python -m petrotwin.collectors.fx

Environment variables:
    PETROTWIN_DATA_DIR — optional override for the ``data/raw`` directory.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import requests
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_CURRENCY = "USD"
QUOTE_CURRENCY = "VND"
UNIT = "VND per 1 USD"
RATE_TYPE = "SBV central reference rate"
SOURCE_URL = "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam"
SOURCE_ATTRIBUTION = "Algolab page identifies source as GSO / State Bank of Vietnam (SBV)"

VIETCOMBANK_API_URL = "https://vapi.vnappmob.com/api/v2/exchange_rate/vietcombank"
VCB_FALLBACK_URL = "https://portal.vietcombank.com.vn/UserControls/TVPortal.TyGia/pXML.aspx?b=10"

REQUEST_TIMEOUT = 15

COLUMNS: list[str] = [
    "date",
    "base_currency",
    "quote_currency",
    "rate_vnd_per_usd",
    "unit",
    "rate_type",
    "source_url",
    "source_attribution",
    "source_value",
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
            settings = (yaml.safe_load(stream) or {}).get("fx", {})
        return max(1.0, float(settings.get("request_timeout_seconds", REQUEST_TIMEOUT)))
    except (AttributeError, OSError, TypeError, ValueError, yaml.YAMLError):
        return REQUEST_TIMEOUT


def _csv_path() -> Path:
    return _raw_dir() / "usd_vnd_rates.csv"


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


def _date_exists_in_csv(path: Path, target: date) -> bool:
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                if date.fromisoformat(row["date"].strip()) == target:
                    return True
            except (KeyError, ValueError):
                continue
    return False


def _format_source_value(rate: int) -> str:
    """Format integer rate with comma thousands separator: 25100 → '25,100'."""
    return f"{rate:,}"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _fetch_vietcombank_api() -> int:
    """Fetch USD transfer rate from Vietcombank vnappmob API.

    Returns:
        Rate as integer VND per 1 USD.

    Raises:
        ValueError: if the USD row or transfer field is not found.
        requests.RequestException: on HTTP/network error.
    """
    _log("fetch_start", method="vietcombank_api", url=VIETCOMBANK_API_URL)
    resp = requests.get(VIETCOMBANK_API_URL, timeout=_request_timeout())
    resp.raise_for_status()
    payload = resp.json()

    results = payload.get("results", [])
    for item in results:
        if str(item.get("currency", "")).strip().upper() == "USD":
            transfer_raw = item.get("transfer", "")
            if not transfer_raw:
                raise ValueError("USD 'transfer' field is empty in Vietcombank API response")
            # Strip commas/spaces and convert
            rate = int(float(str(transfer_raw).replace(",", "").replace(" ", "")))
            _log("fetch_done", method="vietcombank_api", rate=rate)
            return rate

    raise ValueError("USD entry not found in Vietcombank API response")


def _fetch_vcb_fallback() -> int:
    """Fallback: parse Vietcombank XML portal for USD rate.

    Returns:
        Rate as integer VND per 1 USD.
    """
    import xml.etree.ElementTree as ET

    _log("fetch_start", method="vcb_xml_fallback", url=VCB_FALLBACK_URL)
    resp = requests.get(VCB_FALLBACK_URL, timeout=_request_timeout())
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    for exrate in root.iter("Exrate"):
        if exrate.get("CurrencyCode", "").strip().upper() == "USD":
            transfer_raw = exrate.get("Transfer", "")
            if not transfer_raw:
                raise ValueError("USD Transfer attribute is empty in VCB XML fallback")
            rate = int(float(str(transfer_raw).replace(",", "").replace(" ", "")))
            _log("fetch_done", method="vcb_xml_fallback", rate=rate)
            return rate

    raise ValueError("USD entry not found in Vietcombank XML fallback")


def _fetch_rate() -> int:
    """Try primary API, then fallback. Raises on both failures."""
    try:
        return _fetch_vietcombank_api()
    except Exception as primary_exc:
        _log("fetch_primary_failed", error=str(primary_exc))
        _log("fetch_trying_fallback")
        try:
            return _fetch_vcb_fallback()
        except Exception as fallback_exc:
            _log("fetch_fallback_failed", error=str(fallback_exc))
            raise RuntimeError(
                f"Both primary ({primary_exc}) and fallback ({fallback_exc}) fetch attempts failed"
            ) from fallback_exc


# ---------------------------------------------------------------------------
# CSV write
# ---------------------------------------------------------------------------

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
    """Fetch USD/VND rate and append to CSV if not already present today.

    Returns:
        Number of new rows added (0 or 1).
    """
    today = date.today()
    _log("collector_start", today=today.isoformat(), weekday=today.strftime("%A"))

    # Skip weekends
    if today.weekday() >= 5:  # 5=Saturday, 6=Sunday
        _log("skip_weekend", reason="not a working day, skip", day=today.strftime("%A"))
        return 0

    csv_path = _csv_path()

    # Idempotency: skip if today already exists
    if _date_exists_in_csv(csv_path, today):
        _log("up_to_date", date=today.isoformat())
        return 0

    extracted_at = _utcnow_iso()

    try:
        rate = _fetch_rate()
    except requests.HTTPError as exc:
        _log(
            "fetch_error",
            error=str(exc),
            status_code=exc.response.status_code if exc.response is not None else None,
        )
        sys.exit(1)
    except Exception as exc:
        _log("fetch_error", error=str(exc))
        sys.exit(1)

    row: dict[str, str] = {
        "date": today.isoformat(),
        "base_currency": BASE_CURRENCY,
        "quote_currency": QUOTE_CURRENCY,
        "rate_vnd_per_usd": str(rate),
        "unit": UNIT,
        "rate_type": RATE_TYPE,
        "source_url": SOURCE_URL,
        "source_attribution": SOURCE_ATTRIBUTION,
        "source_value": _format_source_value(rate),
        "extracted_at_utc": extracted_at,
    }

    try:
        _append_to_csv(csv_path, [row])
    except OSError as exc:
        _log("write_error", error=str(exc), path=str(csv_path))
        sys.exit(1)

    _log("collector_done", date=today.isoformat(), rate=rate, path=str(csv_path))
    return 1


def main() -> None:
    run()
    sys.exit(0)


if __name__ == "__main__":
    main()
