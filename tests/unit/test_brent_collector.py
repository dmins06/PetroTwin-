from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from petrotwin.collectors import brent


def test_parse_tab_separated_fred_text():
    text = "1987-05-20\t18.63\n1987-05-21\t.\n1987-05-22\t18.55\n"
    rows = brent._parse_text_response(text, since=None)
    assert len(rows) == 3
    assert rows[0] == {"date": "1987-05-20", "raw_value": "18.63"}
    assert rows[1] == {"date": "1987-05-21", "raw_value": "."}
    assert rows[2] == {"date": "1987-05-22", "raw_value": "18.55"}


def test_parse_comma_separated_fred_csv():
    text = "observation_date,DCOILBRENTEU\n2026-11-02,107.39\n2026-11-03,107.03\n"
    rows = brent._parse_text_response(text, since=None)
    assert len(rows) == 2
    assert rows[0] == {"date": "2026-11-02", "raw_value": "107.39"}
    assert rows[1] == {"date": "2026-11-03", "raw_value": "107.03"}


def test_parse_filters_dates_before_or_equal_to_since():
    text = "2026-10-30,120.15\n2026-11-02,107.39\n2026-11-03,107.03\n"
    rows = brent._parse_text_response(text, since=date(2026, 11, 2))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-11-03"


def test_build_csv_rows_handles_observed_and_missing():
    raw_rows = [
        {"date": "2026-11-02", "raw_value": "107.39"},
        {"date": "2026-11-03", "raw_value": "."},
    ]
    csv_rows = brent._build_csv_rows(raw_rows, extracted_at="2026-11-04T00:00:00Z")
    assert len(csv_rows) == 2

    # Observed row
    assert csv_rows[0]["date"] == "2026-11-02"
    assert csv_rows[0]["price_usd_per_barrel"] == "107.39"
    assert csv_rows[0]["observation_status"] == "observed"
    assert csv_rows[0]["series_id"] == "DCOILBRENTEU"

    # Missing row (holiday/non-trading day)
    assert csv_rows[1]["date"] == "2026-11-03"
    assert csv_rows[1]["price_usd_per_barrel"] == ""
    assert csv_rows[1]["observation_status"] == "missing"
    assert csv_rows[1]["source_value"] == "."


def test_fetch_via_text_uses_csv_endpoint(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "observation_date,DCOILBRENTEU\n2026-11-03,107.03\n"
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: mock_resp)

    rows = brent._fetch_via_text(since=None)
    assert len(rows) == 1
    assert rows[0] == {"date": "2026-11-03", "raw_value": "107.03"}


def test_fetch_via_api(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "observations": [
            {"date": "2026-11-02", "value": "107.39"},
            {"date": "2026-11-03", "value": "."},
        ]
    }
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: mock_resp)

    rows = brent._fetch_via_api("fake_key", since=None)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-11-02"
    assert rows[1]["raw_value"] == "."


def test_brent_run_idempotent_when_up_to_date(monkeypatch, tmp_path):
    csv_file = tmp_path / "brent_crude_prices.csv"
    csv_file.write_text("date,price_usd_per_barrel\n2026-11-03,107.03\n", encoding="utf-8")
    monkeypatch.setattr(brent, "_csv_path", lambda: csv_file)
    monkeypatch.setattr(brent, "_last_date_in_csv", lambda path: date(2026, 11, 3))
    # Return empty because all rows <= last_date
    monkeypatch.setattr(brent, "_fetch_via_text", lambda since: [])

    added = brent.run()
    assert added == 0


def test_brent_run_fails_closed_on_http_error(monkeypatch, tmp_path):
    csv_file = tmp_path / "brent_crude_prices.csv"
    csv_file.write_text("date,price_usd_per_barrel\n2026-10-30,120.15\n", encoding="utf-8")
    original_content = csv_file.read_text(encoding="utf-8")
    monkeypatch.setattr(brent, "_csv_path", lambda: csv_file)

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 500
        raise requests.HTTPError("500 Server Error", response=resp)

    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(SystemExit) as exc:
        brent.run()
    assert exc.value.code == 1
    # Existing data must not be modified
    assert csv_file.read_text(encoding="utf-8") == original_content


def test_brent_backfill_recovers_missing_dates_in_chronological_order_and_is_idempotent(monkeypatch, tmp_path):
    import csv

    csv_file = tmp_path / "brent_crude_prices.csv"
    with csv_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=brent.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows([
            {
                "date": "2026-10-14",
                "product_name": brent.PRODUCT_NAME,
                "price_usd_per_barrel": "126.55",
                "unit": brent.UNIT,
                "source_url": brent.SOURCE_URL,
                "series_id": brent.SERIES_ID,
                "publisher": brent.PUBLISHER,
                "source_value": "126.55",
                "observation_status": "observed",
                "extracted_at_utc": "2026-10-20T00:00:00Z",
            },
            {
                "date": "2026-10-19",
                "product_name": brent.PRODUCT_NAME,
                "price_usd_per_barrel": "118.82",
                "unit": brent.UNIT,
                "source_url": brent.SOURCE_URL,
                "series_id": brent.SERIES_ID,
                "publisher": brent.PUBLISHER,
                "source_value": "118.82",
                "observation_status": "observed",
                "extracted_at_utc": "2026-10-20T00:00:00Z",
            },
        ])

    fred_csv = (
        "observation_date,DCOILBRENTEU\n"
        "2026-10-14,999.99\n"  # Must NEVER overwrite existing 126.55!
        "2026-10-15,126.35\n"
        "2026-10-16,119.33\n"
        "2026-10-19,118.82\n"
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = fred_csv
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: mock_resp)
    monkeypatch.setattr(brent, "_csv_path", lambda: csv_file)
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    # 1. Dry-run does not modify the CSV
    before_bytes = csv_file.read_bytes()
    dry_rows = brent.backfill(start_date=date(2026, 10, 14), end_date=date(2026, 10, 19), dry_run=True)
    assert [r["date"] for r in dry_rows] == ["2026-10-15", "2026-10-16"]
    assert csv_file.read_bytes() == before_bytes

    # 2. Actual backfill inserts 2026-10-15 and 2026-10-16 in chronological order without overwriting 2026-10-14
    recovered = brent.backfill(start_date=date(2026, 10, 14), end_date=date(2026, 10, 19), dry_run=False)
    assert len(recovered) == 2

    with csv_file.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["date"] for r in rows] == ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]
    assert rows[0]["price_usd_per_barrel"] == "126.55"  # Existing value preserved!
    assert rows[0]["extracted_at_utc"] == "2026-10-20T00:00:00Z"
    assert rows[1]["price_usd_per_barrel"] == "126.35"
    assert rows[2]["price_usd_per_barrel"] == "119.33"

    # 3. Repeated backfill is idempotent
    after_first_backfill = csv_file.read_bytes()
    second_run = brent.backfill(start_date=date(2026, 10, 14), end_date=date(2026, 10, 19), dry_run=False)
    assert second_run == []
    assert csv_file.read_bytes() == after_first_backfill


def test_brent_backfill_preserves_missing_marker_and_logs_unpublished_dates(monkeypatch, tmp_path):
    import csv

    csv_file = tmp_path / "brent_crude_prices.csv"
    with csv_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=brent.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow({
            "date": "2026-11-10",
            "product_name": brent.PRODUCT_NAME,
            "price_usd_per_barrel": "90.16",
            "unit": brent.UNIT,
            "source_url": brent.SOURCE_URL,
            "series_id": brent.SERIES_ID,
            "publisher": brent.PUBLISHER,
            "source_value": "90.16",
            "observation_status": "observed",
            "extracted_at_utc": "2026-11-11T00:00:00Z",
        })

    fred_csv = (
        "observation_date,DCOILBRENTEU\n"
        "2026-11-10,90.16\n"
        "2026-11-11,.\n"
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = fred_csv
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: mock_resp)
    monkeypatch.setattr(brent, "_csv_path", lambda: csv_file)
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    events = []
    orig_log = brent._log
    monkeypatch.setattr(brent, "_log", lambda event, **kw: (events.append((event, kw)), orig_log(event, **kw)))

    recovered = brent.backfill(start_date=date(2026, 11, 10), end_date=date(2026, 11, 13), dry_run=False)
    assert len(recovered) == 1
    assert recovered[0]["date"] == "2026-11-11"
    assert recovered[0]["observation_status"] == "missing"
    assert recovered[0]["price_usd_per_barrel"] == ""
    assert recovered[0]["source_value"] == "."

    unpub = [kw for ev, kw in events if ev == "unpublished_dates"]
    assert len(unpub) == 1
    assert unpub[0]["unpublished_weekdays"] == ["2026-11-12", "2026-11-13"]


def test_brent_get_with_retries_retries_timeout(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "collectors.yaml").write_text(
        "brent:\n  retry_count: 2\n  retry_delay_seconds: 0\n  request_timeout_seconds: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(brent, "_repo_root", lambda: tmp_path)

    attempts = []

    def flaky_get(url, **kwargs):
        attempts.append(url)
        if len(attempts) < 3:
            raise requests.Timeout("timed out")
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "observation_date,DCOILBRENTEU\n2026-11-03,107.03\n"
        return resp

    monkeypatch.setattr(requests, "get", flaky_get)
    rows = brent._fetch_via_text(since=None)
    assert len(attempts) == 3
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-11-03"
