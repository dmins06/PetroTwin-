"""Collector for the USD/VND State Bank of Vietnam central reference rate.

The collector is fail-closed until an official, documented SBV source and
response schema are configured and verified.  It deliberately does not use
commercial-bank rates or commercial-bank endpoints as substitutes.

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
    """Raised when no verified SBV central-rate source is configured."""


class FXSourceSchemaError(ValueError):
    """Raised when a verified source response does not match the strict schema."""


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
        retry_count = max(0, int(settings.get("retry_count", 0)))
        retry_delay = max(0.0, float(settings.get("retry_delay_seconds", 0)))
    except (TypeError, ValueError) as exc:
        raise FXSourceUnavailableError(f"Invalid FX retry configuration: {exc}") from exc
    return retry_count, retry_delay


def _parse_verified_rate(payload: object, settings: dict[str, object]) -> tuple[int, date]:
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
        rate_raw = str(payload["rate_vnd_per_usd"]).replace(",", "").strip()
        rate_decimal = float(rate_raw)
        if not rate_decimal.is_integer() or rate_decimal <= 0:
            raise ValueError
        rate = int(rate_decimal)
    except (TypeError, ValueError) as exc:
        raise FXSourceSchemaError("Verified FX response has an invalid date or rate") from exc

    if observation_date > date.today():
        raise FXSourceSchemaError("Verified FX observation date is in the future")
    return rate, observation_date


def _fetch_rate() -> tuple[int, date]:
    settings = _fx_config()
    if settings.get("source_status") != "verified":
        raise FXSourceUnavailableError(
            "No verified SBV central reference-rate source is configured; "
            "commercial-bank or secondary-page rates will not be used"
        )

    source_url = str(settings.get("source_url", "")).strip()
    if not source_url.startswith("https://"):
        raise FXSourceUnavailableError("Verified FX source_url must be an HTTPS URL")

    _log("fetch_start", url=source_url, source_status="verified")
    response = requests.get(source_url, timeout=_request_timeout())
    response.raise_for_status()
    try:
        payload = response.json()
    except (requests.JSONDecodeError, ValueError) as exc:
        raise FXSourceSchemaError("Verified FX response is not valid JSON") from exc
    rate, observation_date = _parse_verified_rate(payload, settings)
    _log("fetch_done", date=observation_date.isoformat(), rate=rate)
    return rate, observation_date


def _fetch_with_retries() -> tuple[int, date]:
    retry_count, retry_delay = _retry_settings()
    for attempt in range(retry_count + 1):
        try:
            return _fetch_rate()
        except FXSourceUnavailableError:
            raise
        except FXSourceSchemaError:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            _log("fetch_retryable_error", attempt=attempt + 1, error=str(exc))
            if attempt >= retry_count:
                raise
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
        rate, observation_date = _fetch_with_retries()
    except requests.HTTPError as exc:
        _log(
            "fetch_error",
            error=str(exc),
            status_code=exc.response.status_code if exc.response is not None else None,
        )
        sys.exit(1)
    except (FXSourceUnavailableError, FXSourceSchemaError, requests.RequestException) as exc:
        _log("fetch_error", error=str(exc))
        sys.exit(1)

    csv_path = _csv_path()
    if _date_exists_in_csv(csv_path, observation_date):
        _log("up_to_date", date=observation_date.isoformat())
        return 0

    settings = _fx_config()
    row: dict[str, str] = {
        "date": observation_date.isoformat(),
        "base_currency": BASE_CURRENCY,
        "quote_currency": QUOTE_CURRENCY,
        "rate_vnd_per_usd": str(rate),
        "unit": UNIT,
        "rate_type": RATE_TYPE,
        "source_url": str(settings["source_url"]),
        "source_attribution": str(settings["source_attribution"]),
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
