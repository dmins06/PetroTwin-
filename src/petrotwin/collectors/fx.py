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

import argparse
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
ALGOLAB_TV_SYMBOLS_URL = "https://algolab.vn/api/tv/symbols"
ALGOLAB_TV_HISTORY_URL = "https://algolab.vn/api/tv/history"
ALGOLAB_TV_SYMBOL = "VN_FX_RATE_TY_GIA_SBV_TRUNG_TAM"

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

    heading_pattern = (
        r"T[ỷy] giá SBV trung tâm|"
        r"T[ỷy] giá USD[^\n]*SBV trung tâm"
    )
    if re.search(heading_pattern, visible_text, re.IGNORECASE) is None:
        raise FXSourceSchemaError(
            "Algolab HTML structure changed: central-rate page heading not found"
        )

    # Prefer the current Algolab JSON-LD Dataset metadata.
    dataset = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue

        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("@type") == "Dataset":
                dataset = candidate
                break
        if dataset is not None:
            break

    if dataset is not None:
        measured = dataset.get("variableMeasured")
        based_on = dataset.get("isBasedOn")
        description = str(dataset.get("description", ""))

        if not isinstance(measured, dict):
            raise FXSourceSchemaError("Algolab Dataset variableMeasured is invalid")
        if not isinstance(based_on, dict):
            raise FXSourceSchemaError("Algolab Dataset isBasedOn is invalid")

        if not re.search(
            r"T[ỷy] giá USD:\s*T[ỷy] giá SBV trung tâm",
            str(dataset.get("name", "")),
            re.IGNORECASE,
        ):
            raise FXSourceSchemaError("Algolab Dataset name is not the SBV central rate")

        if not re.search(
            r"T[ỷy] giá SBV trung tâm",
            str(measured.get("name", "")),
            re.IGNORECASE,
        ):
            raise FXSourceSchemaError("Algolab Dataset metric is not the SBV central rate")

        if str(measured.get("unitText", "")).strip().upper() != "VND":
            raise FXSourceSchemaError("Algolab Dataset unit must be VND")

        source_name = str(based_on.get("name", "")).strip()
        if re.fullmatch(r"GSO\s*/\s*SBV", source_name, re.IGNORECASE) is None:
            raise FXSourceSchemaError("Algolab Dataset upstream source declaration is invalid")

        source_in_description = re.search(
            r"Nguồn\s*:\s*GSO\s*/\s*SBV",
            description,
            re.IGNORECASE,
        )
        if source_in_description is None:
            raise FXSourceSchemaError("Algolab Dataset description has no GSO / SBV declaration")

        date_modified = str(dataset.get("dateModified", "")).strip()
        try:
            metadata_date = date.fromisoformat(date_modified)
        except ValueError as exc:
            raise FXSourceSchemaError("Algolab Dataset dateModified is invalid") from exc

        rate_match = re.search(
            r"Mới nhất\s+([1-9]\d[.,]\d{3}|[1-9]\d{4})(?:[.,]0+)?\s*VND",
            description,
            re.IGNORECASE,
        )
        date_match = re.search(
            r"\(([0-3]?\d/[0-1]?\d/[12]\d{3})\)",
            description,
        )
        if rate_match is None or date_match is None:
            raise FXSourceSchemaError("Algolab Dataset description has invalid rate or date")

        try:
            rate = int(rate_match.group(1).replace(",", "").replace(".", ""))
            observation_date = datetime.strptime(
                date_match.group(1), "%d/%m/%Y"
            ).date()
        except ValueError as exc:
            raise FXSourceSchemaError("Algolab Dataset rate or date is invalid") from exc

        if observation_date != metadata_date:
            raise FXSourceSchemaError("Algolab Dataset date does not match description date")

        # Confirm the Dataset values are also present in the rendered page.
        normalized_text = re.sub(r"\s+", " ", visible_text)
        display_rate = re.search(
            rf"(?<!\d){rate:,}(?!\d)|(?<!\d){rate}(?!\d)",
            normalized_text,
        )
        display_date = observation_date.strftime("%d/%m/%Y")
        if display_rate is None or display_date not in normalized_text:
            raise FXSourceSchemaError(
                "Algolab Dataset values do not match the visible page"
            )

    else:
        # Backward compatibility for the previous Algolab HTML structure.
        if re.search(
            r"Nguồn\s*:\s*(?:GSO\s*/\s*)?SBV",
            visible_text,
            re.IGNORECASE,
        ) is None:
            raise FXSourceSchemaError(
                "Algolab HTML structure changed: SBV source declaration not found"
            )

        card_match = re.search(r"(?:Mới nhất|Latest)\b", visible_text, re.IGNORECASE)
        if card_match is None:
            raise FXSourceSchemaError(
                "Algolab HTML changed: latest central-rate card not found"
            )

        window = visible_text[
            max(0, card_match.start() - 60):card_match.start() + 350
        ]
        rate_match = re.search(
            r"\b([1-9]\d[.,]\d{3}|[1-9]\d{4})(?:[.,]0+)?\b",
            window,
        )
        date_match = re.search(
            r"\b([0-3]?\d[/-][0-1]?\d[/-][12]\d{3}|[12]\d{3}-[0-1]\d-[0-3]\d)\b",
            window,
        )
        if rate_match is None or date_match is None:
            raise FXSourceSchemaError(
                "Algolab central-rate card contains invalid date or rate"
            )

        raw_date = date_match.group(1)
        try:
            if "/" in raw_date:
                observation_date = datetime.strptime(raw_date, "%d/%m/%Y").date()
            elif len(raw_date.split("-")[0]) == 4:
                observation_date = date.fromisoformat(raw_date)
            else:
                observation_date = datetime.strptime(raw_date, "%d-%m-%Y").date()
            rate = int(rate_match.group(1).replace(".", "").replace(",", ""))
        except ValueError as exc:
            raise FXSourceSchemaError(
                "Algolab central-rate card contains invalid date or rate"
            ) from exc

    if not 1_000 <= rate <= 100_000:
        raise FXSourceSchemaError("Algolab central rate is outside the expected USD/VND range")
    if observation_date > date.today():
        raise FXSourceSchemaError("Algolab central-rate observation date is in the future")

    return rate, observation_date


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


def _first_date_in_csv(path: Path) -> date | None:
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


def _existing_dates_in_csv(path: Path) -> dict[date, int]:
    """Return mapping of existing observation dates to their integer rate in *path*."""
    if not path.exists():
        return {}
    existing: dict[date, int] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                d = date.fromisoformat(row["date"].strip())
                rate = int(float(row["rate_vnd_per_usd"].strip()))
                existing[d] = rate
            except (KeyError, ValueError):
                continue
    return existing


def _validate_algolab_tv_symbol(payload: object) -> None:
    """Validate that Algolab's UDF symbol metadata represents the SBV central reference rate."""
    if not isinstance(payload, dict):
        raise FXSourceSchemaError("Algolab UDF symbol metadata must be a JSON object")
    if payload.get("name") != ALGOLAB_TV_SYMBOL or payload.get("ticker") != ALGOLAB_TV_SYMBOL:
        raise FXSourceSchemaError(f"Unexpected Algolab UDF symbol: {payload.get('name')!r}")
    if payload.get("type") != "economy":
        raise FXSourceSchemaError("Algolab UDF symbol type must be 'economy'")
    description = str(payload.get("description", ""))
    if not re.search(r"T[ỷy] giá USD:\s*T[ỷy] giá SBV trung tâm\s*\(VND\)", description, re.IGNORECASE):
        raise FXSourceSchemaError("Algolab UDF symbol description does not match SBV central rate (VND)")


def _parse_algolab_tv_history(
    payload: object,
    start_date: date | None = None,
    end_date: date | None = None,
    include_saturdays: bool = False,
) -> list[tuple[date, int]]:
    """Parse Algolab UDF daily history payload into sorted (observation_date, rate) pairs."""
    if not isinstance(payload, dict):
        raise FXSourceSchemaError("Algolab UDF history response must be a JSON object")
    status = payload.get("s")
    if status == "no_data":
        return []
    if status != "ok":
        raise FXSourceSchemaError(f"Algolab UDF history returned invalid status: {status!r}")
    times = payload.get("t")
    closes = payload.get("c")
    if not isinstance(times, list) or not isinstance(closes, list) or len(times) != len(closes):
        raise FXSourceSchemaError("Algolab UDF history 't' and 'c' arrays are missing or mismatched")

    today = date.today()
    by_date: dict[date, int] = {}
    for ts_raw, close_raw in zip(times, closes):
        if isinstance(ts_raw, bool) or not isinstance(ts_raw, (int, float)):
            raise FXSourceSchemaError("Algolab UDF history timestamp must be numeric")
        if isinstance(close_raw, bool) or not isinstance(close_raw, (int, float)):
            raise FXSourceSchemaError("Algolab UDF history close value must be numeric")
        obs_date = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc).date()
        if obs_date > today:
            raise FXSourceSchemaError(f"Algolab UDF history contains future date: {obs_date.isoformat()}")
        rate_float = float(close_raw)
        if not rate_float.is_integer():
            raise FXSourceSchemaError(f"Algolab UDF history rate is not an integer: {close_raw!r}")
        rate = int(rate_float)
        if not 1_000 <= rate <= 100_000:
            raise FXSourceSchemaError(f"Algolab UDF history rate outside valid USD/VND range: {rate}")
        if start_date is not None and obs_date < start_date:
            continue
        if end_date is not None and obs_date > end_date:
            continue
        max_weekday = 5 if include_saturdays else 4
        if obs_date.weekday() > max_weekday:
            continue
        if obs_date in by_date and by_date[obs_date] != rate:
            raise FXSourceSchemaError(
                f"Conflicting Algolab UDF history values for {obs_date.isoformat()}: {by_date[obs_date]} vs {rate}"
            )
        by_date[obs_date] = rate

    return sorted(by_date.items(), key=lambda item: item[0])


def _fetch_history_range(
    start_date: date,
    end_date: date,
    include_saturdays: bool = False,
) -> list[tuple[date, int]]:
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

    timeout = _request_timeout()
    headers = {
        "Accept": "application/json",
        "Referer": source_url,
        "User-Agent": "PetroTwin-data-collector/1.0",
    }
    _log(
        "history_fetch_start",
        symbol=ALGOLAB_TV_SYMBOL,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    sym_resp = requests.get(
        ALGOLAB_TV_SYMBOLS_URL,
        params={"symbol": ALGOLAB_TV_SYMBOL},
        headers=headers,
        timeout=timeout,
    )
    sym_resp.raise_for_status()
    try:
        sym_payload = sym_resp.json()
    except ValueError as exc:
        raise FXSourceSchemaError("Algolab UDF symbol response is not valid JSON") from exc
    _validate_algolab_tv_symbol(sym_payload)

    from_ts = int(datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).timestamp())
    to_dt = end_date + timedelta(days=1)
    to_ts = int(datetime(to_dt.year, to_dt.month, to_dt.day, tzinfo=timezone.utc).timestamp())

    hist_resp = requests.get(
        ALGOLAB_TV_HISTORY_URL,
        params={
            "symbol": ALGOLAB_TV_SYMBOL,
            "resolution": "D",
            "from": str(from_ts),
            "to": str(to_ts),
        },
        headers=headers,
        timeout=timeout,
    )
    hist_resp.raise_for_status()
    try:
        hist_payload = hist_resp.json()
    except ValueError as exc:
        raise FXSourceSchemaError("Algolab UDF history response is not valid JSON") from exc

    pairs = _parse_algolab_tv_history(
        hist_payload,
        start_date=start_date,
        end_date=end_date,
        include_saturdays=include_saturdays,
    )
    _log("history_fetch_done", count=len(pairs), provider=SOURCE_NAME)
    return pairs


def _fetch_history_with_retries(
    start_date: date,
    end_date: date,
    include_saturdays: bool = False,
) -> list[tuple[date, int]]:
    retry_count, retry_delay = _retry_settings()
    for attempt in range(retry_count + 1):
        try:
            return _fetch_history_range(start_date, end_date, include_saturdays=include_saturdays)
        except (FXSourceUnavailableError, FXSourceSchemaError):
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= retry_count:
                raise
            _log("fetch_retryable_error", attempt=attempt + 1, error=str(exc))
            time.sleep(retry_delay)
    raise FXSourceUnavailableError("FX history fetch exhausted without a result")


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


def _insert_rows_chronologically(path: Path, rows: Sequence[dict[str, str]]) -> int:
    """Atomically insert *rows* for missing dates into *path* in chronological order.

    Existing observations are preserved verbatim and never overwritten.
    """
    existing_dates = set(_existing_dates_in_csv(path).keys())
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
    include_saturdays: bool = False,
) -> dict[str, object]:
    """Recover missing historical USD/VND SBV central reference rates from Algolab.

    Never overwrites existing observations and never uses commercial bank rates.
    Reports weekdays that are absent from both local storage and Algolab (such as
    Vietnamese public holidays / SBV non-trading days) as unrecoverable.
    """
    import contextlib

    lock_ctx = contextlib.nullcontext(True) if dry_run else file_lock("fx")
    with lock_ctx as acquired:
        if not acquired:
            _log("lock_busy", collector="fx", reason="Another process holds the fx lock; skipping execution")
            return {"recovered_rows": [], "unrecoverable_dates": []}

        try:
            settings = _fx_config()
        except FXSourceUnavailableError as exc:
            _log("source_unavailable", error=str(exc))
            sys.exit(1)
        if settings.get("source_status") == "unavailable":
            _log("source_unavailable", source_id=settings.get("source_id"), optional=settings.get("optional"))
            sys.exit(1)

        csv_path = _csv_path()
        existing_map = _existing_dates_in_csv(csv_path)
        effective_start = start_date or _first_date_in_csv(csv_path) or date.today()
        effective_end = end_date or _last_date_in_csv(csv_path) or date.today()

        _log(
            "backfill_start",
            start_date=effective_start.isoformat(),
            end_date=effective_end.isoformat(),
            dry_run=dry_run,
            include_saturdays=include_saturdays,
        )

        try:
            fetched_pairs = _fetch_history_with_retries(
                effective_start,
                effective_end,
                include_saturdays=include_saturdays,
            )
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

        extracted_at = _utcnow_iso()
        fetched_by_date = dict(fetched_pairs)

        # Verify no conflict with existing observations; never overwrite existing dates
        for obs_date, rate in fetched_pairs:
            if obs_date in existing_map and existing_map[obs_date] != rate:
                raise FXSourceSchemaError(
                    f"Existing FX rate for {obs_date.isoformat()} ({existing_map[obs_date]}) "
                    f"differs from fetched rate ({rate}); refusing to overwrite"
                )

        csv_rows: list[dict[str, str]] = []
        for obs_date, rate in fetched_pairs:
            if obs_date in existing_map:
                continue
            csv_rows.append(
                {
                    "date": obs_date.isoformat(),
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
                    "extracted_at_utc": extracted_at,
                }
            )

        # Identify weekdays in [effective_start, effective_end] absent from both CSV and Algolab
        unrecoverable_dates: list[str] = []
        cur = effective_start
        while cur <= effective_end:
            if cur.weekday() < 5 and cur not in existing_map and cur not in fetched_by_date:
                unrecoverable_dates.append(cur.isoformat())
            cur += timedelta(days=1)

        if unrecoverable_dates:
            _log(
                "backfill_unrecoverable_dates",
                count=len(unrecoverable_dates),
                dates=unrecoverable_dates,
            )

        if dry_run:
            _log(
                "backfill_dry_run",
                recoverable_rows=len(csv_rows),
                dates=[r["date"] for r in csv_rows],
                unrecoverable_weekdays=len(unrecoverable_dates),
            )
            return {
                "recovered_rows": csv_rows,
                "unrecoverable_dates": unrecoverable_dates,
            }

        if not csv_rows:
            _log("backfill_up_to_date", new_rows=0)
            return {
                "recovered_rows": [],
                "unrecoverable_dates": unrecoverable_dates,
            }

        try:
            inserted = _insert_rows_chronologically(csv_path, csv_rows)
        except OSError as exc:
            _log("write_error", error=str(exc), path=str(csv_path))
            sys.exit(1)

        _log(
            "backfill_done",
            new_rows=inserted,
            dates=[r["date"] for r in csv_rows],
            unrecoverable_weekdays=len(unrecoverable_dates),
            path=str(csv_path),
        )
        return {
            "recovered_rows": csv_rows,
            "unrecoverable_dates": unrecoverable_dates,
        }


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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="USD/VND SBV central reference rate collector (Algolab)")
    parser.add_argument("--backfill", action="store_true", help="Recover missing historical dates in range")
    parser.add_argument("--start-date", type=date.fromisoformat, default=None, help="Backfill start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=date.fromisoformat, default=None, help="Backfill end date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Report recoverable rows without writing to CSV")
    parser.add_argument("--include-saturdays", action="store_true", help="Include Saturday observations when present in Algolab series")
    args = parser.parse_args(argv)
    if args.backfill or args.start_date or args.end_date or args.dry_run:
        backfill(
            start_date=args.start_date,
            end_date=args.end_date,
            dry_run=args.dry_run,
            include_saturdays=args.include_saturdays,
        )
    else:
        run()
    sys.exit(0)


if __name__ == "__main__":
    main()
