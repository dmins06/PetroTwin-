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

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import requests
import yaml

from petrotwin.collectors.locks import file_lock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERIES_ID = "DCOILBRENTEU"
PRODUCT_NAME = "Crude Oil Prices: Brent - Europe"
UNIT = "Dollars per Barrel"
PUBLISHER = "U.S. Energy Information Administration (EIA), distributed by FRED"
SOURCE_URL = "https://fred.stlouisfed.org/data/DCOILBRENTEU"

FRED_TEXT_URL = "https://fred.stlouisfed.org/data/DCOILBRENTEU"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU"
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


def _retry_settings() -> tuple[int, float]:
    config_path = _repo_root() / "configs" / "collectors.yaml"
    try:
        with config_path.open(encoding="utf-8") as stream:
            settings = (yaml.safe_load(stream) or {}).get("brent", {})
        retry_count = max(0, int(settings.get("retry_count", 0)))
        retry_delay = max(0.0, float(settings.get("retry_delay_seconds", 0)))
        return retry_count, retry_delay
    except (AttributeError, OSError, TypeError, ValueError, yaml.YAMLError):
        return 0, 0.0


def _get_with_retries(url: str, **kwargs: object) -> requests.Response:
    retry_count, retry_delay = _retry_settings()
    for attempt in range(retry_count + 1):
        try:
            return requests.get(url, **kwargs)  # type: ignore[arg-type]
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= retry_count:
                raise
            _log("fetch_retryable_error", attempt=attempt + 1, url=url, error=str(exc))
            time.sleep(retry_delay)
    raise requests.RequestException(f"Brent fetch exhausted retries for {url}")


def _csv_path() -> Path:
    return _raw_dir() / "brent_crude_prices.csv"


def _first_date_in_csv(path: Path) -> date | None:
    """Return the earliest date found in the CSV, or None if empty/missing."""
    if not path.exists():
        return None
    first: date | None = None
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                d = date.fromisoformat(row["date"].strip())
                if first is None or d < first:
                    first = d
            except (KeyError, ValueError):
                continue
    return first


def _existing_dates_in_csv(path: Path) -> set[date]:
    """Return the set of all valid dates present in *path*."""
    if not path.exists():
        return set()
    dates: set[date] = set()
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                dates.add(date.fromisoformat(row["date"].strip()))
            except (KeyError, ValueError):
                continue
    return dates


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

def _parse_text_response(
    text: str,
    since: date | None,
    until: date | None = None,
) -> list[dict[str, str]]:
    """Parse FRED observation data (supports both tab-delimited and CSV formats)."""
    rows: list[dict[str, str]] = []
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})[\t,](.+)$")
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
        if until is not None and obs_date > until:
            continue
        rows.append({"date": obs_date_str, "raw_value": raw_value})
    return rows


def _fetch_via_text(
    since: date | None,
    until: date | None = None,
) -> list[dict[str, str]]:
    """Download plain-text/CSV file from FRED (no API key required)."""
    # Prefer fredgraph.csv direct data endpoint; fall back to FRED_TEXT_URL if needed
    for candidate_url, method in [(FRED_CSV_URL, "csv_file"), (FRED_TEXT_URL, "text_file")]:
        _log("fetch_start", method=method, url=candidate_url)
        try:
            resp = _get_with_retries(candidate_url, timeout=_request_timeout())
            resp.raise_for_status()
            rows = _parse_text_response(resp.text, since, until=until)
            if rows or candidate_url == FRED_TEXT_URL:
                _log("fetch_done", method=method, raw_rows=len(rows))
                return rows
        except requests.RequestException as exc:
            if candidate_url == FRED_TEXT_URL:
                raise
            _log("fetch_fallback", error=str(exc), next_url=FRED_TEXT_URL)
    return []


def _fetch_via_api(
    api_key: str,
    since: date | None,
    until: date | None = None,
) -> list[dict[str, str]]:
    """Fetch observations from FRED JSON API using an API key."""
    params: dict[str, str] = {
        "series_id": SERIES_ID,
        "api_key": api_key,
        "file_type": "json",
    }
    if since is not None:
        obs_start = (since + timedelta(days=1)).isoformat()
        params["observation_start"] = obs_start
    if until is not None:
        params["observation_end"] = until.isoformat()
    # vintage_dates: only request data as of today to avoid future revisions
    params["vintage_dates"] = date.today().isoformat()

    _log(
        "fetch_start",
        method="api",
        observation_start=params.get("observation_start"),
        observation_end=params.get("observation_end"),
    )
    resp = _get_with_retries(FRED_JSON_URL, params=params, timeout=_request_timeout())
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
        if until is not None and obs_date > until:
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


def _insert_rows_chronologically(path: Path, rows: Sequence[dict[str, str]]) -> int:
    """Atomically insert *rows* for missing dates into *path* in chronological order.

    Existing rows are preserved verbatim and never overwritten.
    """
    existing_dates = _existing_dates_in_csv(path)
    to_insert: list[dict[str, str]] = []
    seen_new: set[date] = set()
    for row in rows:
        d = date.fromisoformat(row["date"].strip())
        if d in existing_dates or d in seen_new:
            continue
        seen_new.add(d)
        to_insert.append(row)

    if not to_insert:
        return 0

    existing_rows: list[dict[str, str]] = []
    fieldnames = list(COLUMNS)
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            existing_rows = list(reader)

    merged = existing_rows + to_insert
    merged.sort(key=lambda r: r["date"].strip())

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=path.stem + "_tmp_", suffix=".csv"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(merged)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return len(to_insert)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def backfill(
    start_date: date | None = None,
    end_date: date | None = None,
    dry_run: bool = False,
) -> list[dict[str, str]]:
    """Recover missing historical Brent observations within [start_date, end_date].

    Existing observations are never overwritten. FRED missing-value markers ('.')
    are preserved as observation_status='missing'. Dates after the latest
    published FRED date are logged as unpublished rather than collector failures.
    """
    import contextlib

    lock_ctx = contextlib.nullcontext(True) if dry_run else file_lock("brent")
    with lock_ctx as acquired:
        if not acquired:
            _log("lock_busy", collector="brent", reason="Another process holds the brent lock; skipping execution")
            return []

        csv_path = _csv_path()
        first_date = _first_date_in_csv(csv_path)
        existing_dates = _existing_dates_in_csv(csv_path)
        effective_start = start_date or first_date
        since_bound = (effective_start - timedelta(days=1)) if effective_start is not None else None

        _log(
            "backfill_start",
            series=SERIES_ID,
            start_date=effective_start.isoformat() if effective_start else None,
            end_date=end_date.isoformat() if end_date else None,
            dry_run=dry_run,
        )

        api_key = os.environ.get("FRED_API_KEY", "").strip()
        extracted_at = _utcnow_iso()

        try:
            if api_key:
                raw_rows = _fetch_via_api(api_key, since_bound, until=end_date)
            else:
                raw_rows = _fetch_via_text(since_bound, until=end_date)
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

        latest_source_date: date | None = None
        missing_raw_rows: list[dict[str, str]] = []
        for r in raw_rows:
            obs_date = date.fromisoformat(r["date"])
            if latest_source_date is None or obs_date > latest_source_date:
                latest_source_date = obs_date
            if effective_start is not None and obs_date < effective_start:
                continue
            if end_date is not None and obs_date > end_date:
                continue
            if obs_date not in existing_dates:
                missing_raw_rows.append(r)

        if end_date is not None and latest_source_date is not None and end_date > latest_source_date:
            unpublished: list[str] = []
            cur = max(latest_source_date + timedelta(days=1), effective_start or (latest_source_date + timedelta(days=1)))
            while cur <= end_date:
                if cur.weekday() < 5 and cur not in existing_dates:
                    unpublished.append(cur.isoformat())
                cur += timedelta(days=1)
            if unpublished:
                _log(
                    "unpublished_dates",
                    series=SERIES_ID,
                    latest_published_date=latest_source_date.isoformat(),
                    unpublished_weekdays=unpublished,
                )

        if not missing_raw_rows:
            _log("backfill_up_to_date", series=SERIES_ID, new_rows=0, dry_run=dry_run)
            return []

        csv_rows = _build_csv_rows(missing_raw_rows, extracted_at)
        if dry_run:
            _log(
                "backfill_dry_run",
                series=SERIES_ID,
                recoverable_rows=len(csv_rows),
                dates=[r["date"] for r in csv_rows],
            )
            return csv_rows

        try:
            inserted = _insert_rows_chronologically(csv_path, csv_rows)
        except OSError as exc:
            _log("write_error", error=str(exc), path=str(csv_path))
            sys.exit(1)

        _log(
            "backfill_done",
            series=SERIES_ID,
            new_rows=inserted,
            dates=[r["date"] for r in csv_rows],
            path=str(csv_path),
        )
        return csv_rows


def run() -> int:
    """Fetch new Brent observations and append to CSV.

    Returns:
        Number of new rows added.

    Raises:
        SystemExit: on HTTP error or I/O failure.
    """
    with file_lock("brent") as acquired:
        if not acquired:
            _log("lock_busy", collector="brent", reason="Another process holds the brent lock; skipping execution")
            return 0

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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Brent crude oil collector (FRED DCOILBRENTEU)")
    parser.add_argument("--backfill", action="store_true", help="Recover missing historical dates in range")
    parser.add_argument("--start-date", type=date.fromisoformat, default=None, help="Backfill start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=date.fromisoformat, default=None, help="Backfill end date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Report recoverable rows without writing to CSV")
    args = parser.parse_args(argv)
    if args.backfill or args.start_date or args.end_date or args.dry_run:
        backfill(start_date=args.start_date, end_date=args.end_date, dry_run=args.dry_run)
    else:
        run()
    sys.exit(0)


if __name__ == "__main__":
    main()
