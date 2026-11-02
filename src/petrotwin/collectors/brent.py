"""Collector: Brent crude oil prices from FRED (series DCOILBRENTEU).

Fetches new daily observations from the U.S. Energy Information Administration
via the FRED API (with API key) or the plain-text file download (without key),
and appends them to ``data/raw/brent_crude_prices.csv``.

Usage::

    python -m petrotwin.collectors.brent

Environment variables:
    FRED_API_KEY  — optional FRED API key; falls back to unauthenticated
                    plain-text file download when not set.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import requests
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERIES_ID = "DCOILBRENTEU"
PRODUCT_NAME = "Crude Oil Prices: Brent - Europe"
UNIT = "Dollars per Barrel"
PUBLISHER = "U.S. Energy Information Administration (EIA), distributed by FRED"
SOURCE_URL = "https://fred.stlouisfed.org/data/DCOILBRENTEU"

FRED_TEXT_URL = "https://fred.stlouisfed.org/data/DCOILBRENTEU"
FRED_JSON_URL = "https://api.stlouisfed.org/fred/series/observations"

COLUMNS: list[str] = [
    "date",
    "product_name",
    "price_usd_per_barrel",
    "unit",
    "source_url",
    "series_id",
    "publisher",
    "source_value",
    "observation_status",
    "extracted_at_utc",
]

REQUEST_TIMEOUT = 30
MISSING_MARKER = "."

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(message)s",
)
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
    """Return the repository root containing ``src/`` and ``data/``."""
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
            settings = (yaml.safe_load(stream) or {}).get("brent", {})
        return max(1.0, float(settings.get("request_timeout_seconds", REQUEST_TIMEOUT)))
    except (AttributeError, OSError, TypeError, ValueError, yaml.YAMLError):
        return REQUEST_TIMEOUT


def _csv_path() -> Path:
    return _raw_dir() / "brent_crude_prices.csv"


def _last_date_in_csv(path: Path) -> date | None:
    """Return the latest date found in the CSV, or None if empty/missing."""
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


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _parse_text_response(text: str, since: date | None) -> list[dict[str, str]]:
    """Parse the plain-text FRED file format into a list of observation dicts."""
    rows: list[dict[str, str]] = []
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})\t(.+)$")
    for line in text.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        obs_date_str, raw_value = m.group(1), m.group(2).strip()
        try:
            obs_date = date.fromisoformat(obs_date_str)
        except ValueError:
            continue
        if since is not None and obs_date <= since:
            continue
        rows.append({"date": obs_date_str, "raw_value": raw_value})
    return rows


def _fetch_via_text(since: date | None) -> list[dict[str, str]]:
    """Download plain-text file from FRED (no API key required)."""
    _log("fetch_start", method="text_file", url=FRED_TEXT_URL)
    resp = requests.get(FRED_TEXT_URL, timeout=_request_timeout())
    resp.raise_for_status()
    rows = _parse_text_response(resp.text, since)
    _log("fetch_done", method="text_file", raw_rows=len(rows))
    return rows


def _fetch_via_api(api_key: str, since: date | None) -> list[dict[str, str]]:
    """Fetch observations from FRED JSON API using an API key."""
    params: dict[str, str] = {
        "series_id": SERIES_ID,
        "api_key": api_key,
        "file_type": "json",
    }
    if since is not None:
        obs_start = (since + timedelta(days=1)).isoformat()
        params["observation_start"] = obs_start
    # vintage_dates: only request data as of today to avoid future revisions
    params["vintage_dates"] = date.today().isoformat()

    _log("fetch_start", method="api", observation_start=params.get("observation_start"))
    resp = requests.get(FRED_JSON_URL, params=params, timeout=_request_timeout())
    resp.raise_for_status()
    payload = resp.json()

    rows: list[dict[str, str]] = []
    for obs in payload.get("observations", []):
        obs_date_str = obs.get("date", "")
        raw_value = obs.get("value", MISSING_MARKER)
        try:
            obs_date = date.fromisoformat(obs_date_str)
        except ValueError:
            continue
        if since is not None and obs_date <= since:
            continue
        rows.append({"date": obs_date_str, "raw_value": raw_value})

    _log("fetch_done", method="api", raw_rows=len(rows))
    return rows


def _build_csv_rows(
    raw_rows: list[dict[str, str]],
    extracted_at: str,
) -> list[dict[str, str]]:
    """Convert raw fetched rows into final CSV-ready dicts."""
    result: list[dict[str, str]] = []
    for r in raw_rows:
        raw_val = r["raw_value"]
        if raw_val == MISSING_MARKER:
            price_str = ""
            status = "missing"
        else:
            try:
                price_str = str(float(raw_val))
            except ValueError:
                price_str = raw_val
            status = "observed"

        result.append(
            {
                "date": r["date"],
                "product_name": PRODUCT_NAME,
                "price_usd_per_barrel": price_str,
                "unit": UNIT,
                "source_url": SOURCE_URL,
                "series_id": SERIES_ID,
                "publisher": PUBLISHER,
                "source_value": raw_val,
                "observation_status": status,
                "extracted_at_utc": extracted_at,
            }
        )
    return result


def _append_to_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    """Atomically append *rows* to *path*, preserving existing content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file, then rename (atomic on POSIX)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=path.stem + "_tmp_", suffix=".csv"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            # Copy existing content verbatim
            if path.exists():
                with path.open(newline="", encoding="utf-8") as src:
                    content = src.read()
                fh.write(content)
                # Ensure file ends with newline before appending
                if content and not content.endswith("\n"):
                    fh.write("\n")
            else:
                # Brand new file — write header
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
    """Fetch new Brent observations and append to CSV.

    Returns:
        Number of new rows added.

    Raises:
        SystemExit: on HTTP error or I/O failure.
    """
    csv_path = _csv_path()
    last_date = _last_date_in_csv(csv_path)
    _log("collector_start", series=SERIES_ID, last_date=str(last_date) if last_date else None)

    api_key = os.environ.get("FRED_API_KEY", "").strip()
    extracted_at = _utcnow_iso()

    try:
        if api_key:
            raw_rows = _fetch_via_api(api_key, last_date)
        else:
            raw_rows = _fetch_via_text(last_date)
    except requests.HTTPError as exc:
        _log("fetch_error", error=str(exc), status_code=exc.response.status_code if exc.response is not None else None)
        sys.exit(1)
    except requests.RequestException as exc:
        _log("fetch_error", error=str(exc))
        sys.exit(1)

    if not raw_rows:
        _log("up_to_date", series=SERIES_ID, last_date=str(last_date) if last_date else None)
        return 0

    csv_rows = _build_csv_rows(raw_rows, extracted_at)

    try:
        _append_to_csv(csv_path, csv_rows)
    except OSError as exc:
        _log("write_error", error=str(exc), path=str(csv_path))
        sys.exit(1)

    _log("collector_done", series=SERIES_ID, new_rows=len(csv_rows), path=str(csv_path))
    return len(csv_rows)


def main() -> None:
    new_rows = run()
    sys.exit(0)


if __name__ == "__main__":
    main()
