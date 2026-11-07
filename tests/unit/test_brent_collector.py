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
