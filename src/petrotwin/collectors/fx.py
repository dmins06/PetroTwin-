"""Collector for USD/VND SBV central reference rate from Algolab.

This collector parses the daily SBV central reference rate as declared by
Algolab (which identifies the State Bank of Vietnam as upstream).
Provenance is tracked as 'provider_declared' and does not claim independent
SBV verification. Commercial-bank rates are never used.

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
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import requests
import yaml
from bs4 import BeautifulSoup
import re

from petrotwin.collectors.locks import file_lock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_CURRENCY = "USD"
QUOTE_CURRENCY = "VND"
SOURCE_ID = "algolab_sbv_central_rate"
SOURCE_NAME = "Algolab"
UPSTREAM_SOURCE = "State Bank of Vietnam"
PROVENANCE_STATUS = "provider_declared"
ALGOLAB_URL = "https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam"
# Backward-compatible name for callers using the previously misspelled constant.
ALGOAB_URL = ALGOLAB_URL

UNIT = "VND per 1 USD"
RATE_TYPE = "SBV central reference rate"
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


class FXSourceUnavailableError(RuntimeError):
    """No verified official source is currently configured."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class FXSourceSchemaError(ValueError):
    """Verified-source response did not match the required schema."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


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


def _fx_config() -> dict[str, object]:
    config_path = _repo_root() / "configs" / "collectors.yaml"
    try:
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
        settings = config.get("fx")
        if not isinstance(settings, dict):
            raise FXSourceUnavailableError("FX source configuration is missing the 'fx' section")
        return settings
    except FXSourceUnavailableError:
        raise
    except (OSError, TypeError, yaml.YAMLError) as exc:
        raise FXSourceUnavailableError(f"Could not load FX source configuration: {exc}") from exc


def _retry_settings() -> tuple[int, float]:
    settings = _fx_config()
    try:
        return max(0, int(settings.get("retry_count", 0))), max(
            0.0, float(settings.get("retry_delay_seconds", 0))
        )
    except (TypeError, ValueError) as exc:
        raise FXSourceUnavailableError(f"Invalid FX retry configuration: {exc}") from exc


def _parse_verified_rate(payload: object) -> tuple[int, date]:
    if not isinstance(payload, dict):
        raise FXSourceSchemaError("Verified FX response must be a JSON object")
    required = {"observation_date", "rate_vnd_per_usd", "unit", "rate_type"}
    missing = sorted(required - payload.keys())
    if missing:
        raise FXSourceSchemaError(f"Verified FX response missing fields: {', '.join(missing)}")
    if payload.get("unit") != UNIT:
        raise FXSourceSchemaError(f"Verified FX response unit must be {UNIT!r}")
    if payload.get("rate_type") != RATE_TYPE:
        raise FXSourceSchemaError("Verified FX response is not an SBV central reference rate")
    try:
        observation_date = date.fromisoformat(str(payload["observation_date"]))
        rate_decimal = float(str(payload["rate_vnd_per_usd"]).replace(",", "").strip())
        if not rate_decimal.is_integer() or rate_decimal <= 0:
            raise ValueError
        rate = int(rate_decimal)
    except (TypeError, ValueError) as exc:
        raise FXSourceSchemaError("Verified FX response has an invalid date or rate") from exc
    if observation_date > date.today():
        raise FXSourceSchemaError("Verified FX observation date is in the future")
    return rate, observation_date


def _validate_freshness(observation_date: date, max_age_days: int = 7) -> None:
    age_days = (date.today() - observation_date).days
    if age_days > max_age_days:
        raise FXSourceSchemaError(f"Algolab central-rate observation is stale: {observation_date.isoformat()} is {age_days} days old")


def _parse_algolab_html(html: str) -> tuple[int, date]:
    if not isinstance(html, str) or not html.strip():
        raise FXSourceSchemaError("Algolab HTML response is empty")

    soup = BeautifulSoup(html, "html.parser")
    visible_text = " ".join(soup.stripped_strings)
    if re.search(r"T[ỷy] giá SBV trung tâm|T[ỷy] giá USD[^\n]*SBV trung tâm", visible_text, re.IGNORECASE) is None:
        raise FXSourceSchemaError("Algolab HTML structure changed: central-rate page heading not found")
    if re.search(r"Nguồn\s*:\s*(?:GSO\s*/\s*)?SBV", visible_text, re.IGNORECASE) is None:
        raise FXSourceSchemaError("Algolab HTML structure changed: SBV source declaration not found")

    # Locate the latest central-rate card/section by searching for the "Mới nhất" or "Latest" label.
    card_match = re.search(r"(?:Mới nhất|Latest)\b", visible_text, re.IGNORECASE)
    if card_match is None:
        raise FXSourceSchemaError("Algolab HTML changed: latest central-rate card not found")

    # Scope extraction to a window around the label to prevent matching unrelated numbers elsewhere.
    start_pos = card_match.start()
    window = visible_text[max(0, start_pos - 60) : start_pos + 350]

    # USD/VND central rate is a 5-digit rate (10,000–99,999 VND/USD, e.g. 25,627 or 25627).
    # Explicit 5-digit pattern prevents accidentally matching timestamps (09:00), index ranks (#1),
    # price change deltas (+15, -5), percentage changes (+0.06%), or 4-digit years (2026).
    rate_match = re.search(r"\b([1-9]\d[.,]\d{3}|[1-9]\d{4})(?:[.,]0+)?\b", window)
    if rate_match is None:
        raise FXSourceSchemaError("Algolab central-rate card contains invalid date or rate")

    date_match = re.search(r"\b([0-3]?\d[/-][0-1]?\d[/-][12]\d{3}|[12]\d{3}-[0-1]\d-[0-3]\d)\b", window)
    if date_match is None:
        raise FXSourceSchemaError("Algolab central-rate card contains invalid date or rate")

    raw_rate = rate_match.group(1)
    raw_date = date_match.group(1)
    try:
        if "/" in raw_date:
            observation_date = datetime.strptime(raw_date, "%d/%m/%Y").date()
        elif "-" in raw_date and len(raw_date.split("-")[0]) == 4:
            observation_date = date.fromisoformat(raw_date)
        else:
            observation_date = datetime.strptime(raw_date, "%d-%m-%Y").date()
        rate_decimal = float(raw_rate.replace(".", "").replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise FXSourceSchemaError("Algolab central-rate card contains invalid date or rate") from exc
    if not rate_decimal.is_integer() or rate_decimal <= 0:
        raise FXSourceSchemaError("Algolab central rate must be a positive integer")
    if not 1_000 <= rate_decimal <= 100_000:
        raise FXSourceSchemaError("Algolab central rate is outside the expected USD/VND range")
    if observation_date > date.today():
        raise FXSourceSchemaError("Algolab central-rate observation date is in the future")
    return int(rate_decimal), observation_date


def _fetch_rate() -> tuple[int, date]:
    settings = _fx_config()
    if settings.get("source_status") not in {"enabled", "verified"}:
        raise FXSourceUnavailableError(
            "Algolab central-rate source is not verified in configuration; "
            "commercial-bank rates will not be used"
        )
    if settings.get("source_id") != SOURCE_ID:
        raise FXSourceUnavailableError("FX source_id must be algolab_sbv_central_rate")
    source_url = str(settings.get("source_url", "")).strip()
    if source_url != ALGOAB_URL:
        raise FXSourceUnavailableError("Unexpected Algolab FX source_url")
    _log("fetch_start", url=source_url, provider=SOURCE_NAME, upstream=UPSTREAM_SOURCE)
    response = requests.get(
        source_url,
        timeout=_request_timeout(),
        headers={"Accept": "text/html", "User-Agent": "PetroTwin-data-collector/1.0"},
    )
    response.raise_for_status()
    rate, observation_date = _parse_algolab_html(response.text)
    try:
        max_age_days = int(settings.get("max_age_days", 7))
    except (TypeError, ValueError) as exc:
        raise FXSourceUnavailableError("Invalid FX max_age_days configuration") from exc
    _validate_freshness(observation_date, max(1, max_age_days))
    _log("fetch_done", date=observation_date.isoformat(), rate=rate, provider=SOURCE_NAME)
    return rate, observation_date


def _fetch_with_retries() -> tuple[int, date]:
    retry_count, retry_delay = _retry_settings()
    for attempt in range(retry_count + 1):
        try:
            return _fetch_rate()
        except (FXSourceUnavailableError, FXSourceSchemaError):
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= retry_count:
                raise
            _log("fetch_retryable_error", attempt=attempt + 1, error=str(exc))
            time.sleep(retry_delay)
    raise FXSourceUnavailableError("FX source fetch exhausted without a result")


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
    with file_lock("fx") as acquired:
        if not acquired:
            _log("lock_busy", collector="fx", reason="Another process holds the fx lock; skipping execution")
            return 0

        today = date.today()
        _log("collector_start", today=today.isoformat(), weekday=today.strftime("%A"))

        try:
            settings = _fx_config()
        except FXSourceUnavailableError as exc:
            _log("source_unavailable", error=str(exc))
            sys.exit(1)
        if settings.get("source_status") == "unavailable":
            _log("source_unavailable", source_id=settings.get("source_id"), optional=settings.get("optional"))
            sys.exit(1)

        # Skip weekends
        if today.weekday() >= 5:  # 5=Saturday, 6=Sunday
            _log("skip_weekend", reason="not a working day, skip", day=today.strftime("%A"))
            return 0

        try:
            rate, observation_date = _fetch_with_retries()
        except requests.HTTPError as exc:
            _log(
                "fetch_error",
                error=str(exc),
                status_code=exc.response.status_code if exc.response is not None else None,
            )
            sys.exit(1)
        except (FXSourceUnavailableError, FXSourceSchemaError, requests.RequestException) as exc:
            _log("source_unavailable" if isinstance(exc, FXSourceUnavailableError) else "fetch_error", error=str(exc))
            sys.exit(1)

        csv_path = _csv_path()
        if _date_exists_in_csv(csv_path, observation_date):
            _log("up_to_date", date=observation_date.isoformat())
            return 0

        row: dict[str, str] = {
            "date": observation_date.isoformat(),
            "base_currency": BASE_CURRENCY,
            "quote_currency": QUOTE_CURRENCY,
            "rate_vnd_per_usd": str(rate),
            "unit": UNIT,
            "rate_type": RATE_TYPE,
            "source_url": str(settings["source_url"]),
            "source_attribution": (
                f"provider={SOURCE_NAME}; source_id={SOURCE_ID}; "
                f"upstream={UPSTREAM_SOURCE}; provenance_status={PROVENANCE_STATUS}"
            ),
            "source_value": _format_source_value(rate),
            "extracted_at_utc": _utcnow_iso(),
        }

        try:
            _append_to_csv(csv_path, [row])
        except OSError as exc:
            _log("write_error", error=str(exc), path=str(csv_path))
            sys.exit(1)

        _log("collector_done", date=observation_date.isoformat(), rate=rate, new_rows=1,
             path=str(csv_path))
        return 1


def main() -> None:
    run()
    sys.exit(0)


if __name__ == "__main__":
    main()
