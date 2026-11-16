"""Unit tests for the COMECO domestic fuel price collector."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from petrotwin.collectors import comeco


# ---------------------------------------------------------------------------
# HTML Fixtures
# ---------------------------------------------------------------------------

def valid_comeco_html(
    eff_date_str: str = "12/11/2026",
    eff_time_str: str = "15:00",
    include_muc5: bool = True,
    price_overrides: dict[str, str] | None = None,
) -> str:
    """Generate realistic COMECO HTML markup matching the live website DOM."""
    prices = {
        "Xăng E10 RON 95 Mức 5": "21.840",
        "Dầu DO 0,001S Mức 5": "28.620",
        "Xăng E10 RON 95 Mức 3": "19.840",
        "Dầu DO 0,05S Mức 2": "26.820",
        "Xăng E5 RON 92 Mức 2": "19.390",
    }
    if price_overrides:
        prices.update(price_overrides)

    cards_html = []
    for prod_name, price in prices.items():
        if not include_muc5 and "Mức 5" in prod_name and "RON 95" in prod_name:
            continue
        cards_html.append(f"""
        <div id="col-rand{hash(prod_name)}" class="col medium-4 small-12 large-4">
            <div class="col-inner">
                <div class="box has-hover has-hover-box-shadow">
                    <div class="box-text text-center">
                        <div class="box-text-inner">
                            <h5>{prod_name}</h5>
                            <h2>{price}</h2>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        """)

    return f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <title>COMECO - Bán lẻ xăng dầu</title>
    </head>
    <body>
        <div id="content" role="main">
            <div id="section_123456" class="section">
                <div class="section-content relative">
                    <div class="row">
                        <div class="col small-12 large-12">
                            <div class="col-inner text-center">
                                <h2 class="section-title">Giá bán lẻ xăng dầu</h2>
                                <div class="price-banner">
                                    <span style="font-size: 110%;">
                                        <strong>Giá điều chỉnh từ {eff_time_str} ngày {eff_date_str}. Đơn vị tính: đồng/lít.</strong>
                                    </span>
                                </div>
                                <div class="row row-small">
                                    {"".join(cards_html)}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Parsing Tests
# ---------------------------------------------------------------------------

def test_parse_valid_comeco_html():
    html = valid_comeco_html("12/11/2026", "15:00")
    eff_date, eff_time, records = comeco._parse_comeco_html(html)

    assert eff_date == date(2026, 11, 12)
    assert eff_time == "15:00"
    assert len(records) == 4

    prods = {r["product_name"]: r["price"] for r in records}
    assert prods == {
        "Dầu DO 0,001S-V": 28620,
        "Dầu DO 0,05S-II": 26820,
        "Xăng E10 RON 95-III": 19840,
        "Xăng E5 RON 92-II": 19390,
    }


def test_muc_5_is_strictly_excluded_and_never_mapped_to_muc_3():
    """Verify that Xăng E10 RON 95 Mức 5 is ignored and never aliased to Mức 3."""
    html = valid_comeco_html(price_overrides={
        "Xăng E10 RON 95 Mức 5": "30.000",
        "Xăng E10 RON 95 Mức 3": "19.840",
    })
    _, _, records = comeco._parse_comeco_html(html)
    prods = {r["product_name"]: r["price"] for r in records}

    assert "Xăng E10 RON 95-III" in prods
    assert prods["Xăng E10 RON 95-III"] == 19840  # Must be Mức 3, not Mức 5!
    assert "Xăng E10 RON 95 Mức 5" not in prods
    assert "Xăng E10 RON95-V" not in prods


def test_missing_target_product_raises_value_error():
    """If any of the 4 required products is absent, collector must fail closed."""
    # HTML missing Xăng E5 RON 92 Mức 2
    html = """
    <div class="col-inner">
        <h2>Giá bán lẻ xăng dầu</h2>
        <span>15:00 ngày 12/11/2026</span>
        <div class="col"><h5>Xăng E10 RON 95 Mức 3</h5><h2>19.840</h2></div>
        <div class="col"><h5>Dầu DO 0,001S Mức 5</h5><h2>28.620</h2></div>
        <div class="col"><h5>Dầu DO 0,05S Mức 2</h5><h2>26.820</h2></div>
    </div>
    """
    with pytest.raises(ValueError, match="Missing required target products"):
        comeco._parse_comeco_html(html)


def test_duplicate_product_card_raises_value_error():
    """Duplicate cards for the same target product must fail closed."""
    html = """
    <div class="col-inner">
        <h2>Giá bán lẻ xăng dầu</h2>
        <span>15:00 ngày 12/11/2026</span>
        <div class="col"><h5>Xăng E10 RON 95 Mức 3</h5><h2>19.840</h2></div>
        <div class="col"><h5>Xăng E10 RON 95 Mức 3</h5><h2>19.850</h2></div>
        <div class="col"><h5>Xăng E5 RON 92 Mức 2</h5><h2>19.390</h2></div>
        <div class="col"><h5>Dầu DO 0,001S Mức 5</h5><h2>28.620</h2></div>
        <div class="col"><h5>Dầu DO 0,05S Mức 2</h5><h2>26.820</h2></div>
    </div>
    """
    with pytest.raises(ValueError, match="Duplicate product card"):
        comeco._parse_comeco_html(html)


def test_invalid_or_suspicious_price_raises_value_error():
    """Zero, negative, or suspiciously low prices must raise ValueError."""
    # Low price <= 1000
    html_low = valid_comeco_html(price_overrides={"Xăng E5 RON 92 Mức 2": "500"})
    with pytest.raises(ValueError, match="suspiciously low or invalid"):
        comeco._parse_comeco_html(html_low)

    # Non-numeric price
    html_nan = valid_comeco_html(price_overrides={"Xăng E5 RON 92 Mức 2": "Liên hệ"})
    with pytest.raises(ValueError, match="Invalid price value"):
        comeco._parse_comeco_html(html_nan)


def test_missing_effective_date_raises_value_error():
    html = """
    <div class="col-inner">
        <h2>Giá bán lẻ xăng dầu</h2>
        <span>15:00</span>
        <div class="col"><h5>Xăng E10 RON 95 Mức 3</h5><h2>19.840</h2></div>
        <div class="col"><h5>Xăng E5 RON 92 Mức 2</h5><h2>19.390</h2></div>
        <div class="col"><h5>Dầu DO 0,001S Mức 5</h5><h2>28.620</h2></div>
        <div class="col"><h5>Dầu DO 0,05S Mức 2</h5><h2>26.820</h2></div>
    </div>
    """
    with pytest.raises(ValueError, match="Could not extract effective date"):
        comeco._parse_comeco_html(html)


def test_missing_effective_time_raises_value_error():
    """Fails closed when effective time cannot be extracted from HTML."""
    html = """
    <div class="col-inner">
        <h2>Giá bán lẻ xăng dầu</h2>
        <span>ngày 12/11/2026</span>
        <div class="col"><h5>Xăng E10 RON 95 Mức 3</h5><h2>19.840</h2></div>
        <div class="col"><h5>Xăng E5 RON 92 Mức 2</h5><h2>19.390</h2></div>
        <div class="col"><h5>Dầu DO 0,001S Mức 5</h5><h2>28.620</h2></div>
        <div class="col"><h5>Dầu DO 0,05S Mức 2</h5><h2>26.820</h2></div>
    </div>
    """
    with pytest.raises(ValueError, match="Could not extract effective time"):
        comeco._parse_comeco_html(html)


def test_missing_section_anchor_raises_value_error():
    html = "<html><body><h1>Trang chủ COMECO</h1></body></html>"
    with pytest.raises(ValueError, match="Could not find 'Giá bán lẻ xăng dầu'"):
        comeco._parse_comeco_html(html)


# ---------------------------------------------------------------------------
# HTTP & Network Handling Tests
# ---------------------------------------------------------------------------

def test_fetch_html_fast_fail_on_http_403():
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
    mock_session.get.return_value = mock_resp

    with pytest.raises(requests.HTTPError):
        comeco._fetch_html(session=mock_session, retries=2, retry_delay=0)

    # Fast-fail without retry on 4xx client errors
    assert mock_session.get.call_count == 1


def test_fetch_html_retries_on_http_500():
    mock_session = MagicMock()
    mock_resp_fail = MagicMock()
    mock_resp_fail.status_code = 500
    mock_resp_fail.raise_for_status.side_effect = requests.HTTPError(response=mock_resp_fail)

    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.text = "<html>ok</html>"
    mock_resp_ok.encoding = "utf-8"

    # Fails once then succeeds on second attempt
    mock_session.get.side_effect = [mock_resp_fail, mock_resp_ok]

    content = comeco._fetch_html(session=mock_session, retries=2, retry_delay=0)
    assert content == "<html>ok</html>"
    assert mock_session.get.call_count == 2


def test_fetch_html_exhausts_retries_on_timeout():
    mock_session = MagicMock()
    mock_session.get.side_effect = requests.Timeout("Connection timed out")

    with pytest.raises(requests.RequestException, match="timed out"):
        comeco._fetch_html(session=mock_session, retries=2, retry_delay=0)

    assert mock_session.get.call_count == 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# CLI & Execution Modes
# ---------------------------------------------------------------------------

def test_check_only_success(capsys, monkeypatch):
    html = valid_comeco_html("12/11/2026", "15:00")
    monkeypatch.setattr(comeco, "_fetch_html", lambda **kwargs: html)

    code = comeco.check_only()
    assert code == 0

    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert data["event"] == "check_only_success"
    assert data["status"] == "valid"
    assert data["effective_date"] == "2026-11-12"
    assert data["effective_time"] == "15:00"
    assert data["products_count"] == 4


def test_check_only_failure_on_parse_error(capsys, monkeypatch):
    monkeypatch.setattr(comeco, "_fetch_html", lambda **kwargs: "<html><body>broken</body></html>")

    code = comeco.check_only()
    assert code == 1

    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert data["event"] == "check_only_failure"
    assert data["status"] == "invalid"
    assert "error" in data


# ---------------------------------------------------------------------------
# Run Lifecycle & CSV Writing Tests
# ---------------------------------------------------------------------------

def test_run_idempotent_when_up_to_date(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    csv_file.write_text(
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-11-12,2026-11-12,15:00,Xăng E10 RON 95-III,19840,19.840 đ,0,0,Đồng/lít thực tế,true,https://comeco.vn/,2026-11-12T08:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(comeco, "_csv_path", lambda: csv_file)
    monkeypatch.setattr(comeco, "_fetch_html", lambda **kwargs: valid_comeco_html("12/11/2026"))

    added = comeco.run()
    assert added == 0


def test_run_appends_new_records_and_computes_price_changes(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    # Seed historical records with prior prices
    csv_file.write_text(
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-10-22,2026-10-22,15:00,Xăng E10 RON 95-III,20000,20.000 đ,0,0,Đồng/lít thực tế,true,https://pvoil.com.vn/,2026-10-22T08:00:00Z\n"
        "2026-10-22,2026-10-22,15:00,Xăng E5 RON 92-II,19000,19.000 đ,0,0,Đồng/lít thực tế,true,https://pvoil.com.vn/,2026-10-22T08:00:00Z\n"
        "2026-10-22,2026-10-22,15:00,\"Dầu DO 0,001S-V\",28000,28.000 đ,0,0,Đồng/lít thực tế,true,https://pvoil.com.vn/,2026-10-22T08:00:00Z\n"
        "2026-10-22,2026-10-22,15:00,\"Dầu DO 0,05S-II\",26000,26.000 đ,0,0,Đồng/lít thực tế,true,https://pvoil.com.vn/,2026-10-22T08:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(comeco, "_csv_path", lambda: csv_file)
    # New announcement date 12/11/2026
    monkeypatch.setattr(comeco, "_fetch_html", lambda **kwargs: valid_comeco_html("12/11/2026"))

    added = comeco.run()
    assert added == 4

    rows = []
    with csv_file.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == 8  # 4 original + 4 new
    new_rows = {r["product_name"]: r for r in rows[4:]}

    # Verify E10 RON 95-III: 19840 - 20000 = -160
    assert new_rows["Xăng E10 RON 95-III"]["price_vnd_per_liter"] == "19840"
    assert new_rows["Xăng E10 RON 95-III"]["price_change_vnd"] == "-160"
    assert new_rows["Xăng E10 RON 95-III"]["source_url"] == "https://comeco.vn/"

    # Verify E5 RON 92-II: 19390 - 19000 = +390
    assert new_rows["Xăng E5 RON 92-II"]["price_vnd_per_liter"] == "19390"
    assert new_rows["Xăng E5 RON 92-II"]["price_change_vnd"] == "390"
    assert new_rows["Xăng E5 RON 92-II"]["price_change_display"] == "+390"

    # Verify DO 0,001S-V: 28620 - 28000 = +620
    assert new_rows["Dầu DO 0,001S-V"]["price_vnd_per_liter"] == "28620"
    assert new_rows["Dầu DO 0,001S-V"]["price_change_vnd"] == "620"

    # Verify DO 0,05S-II: 26820 - 26000 = +820
    assert new_rows["Dầu DO 0,05S-II"]["price_vnd_per_liter"] == "26820"
    assert new_rows["Dầu DO 0,05S-II"]["price_change_vnd"] == "820"


def test_run_lock_busy_returns_zero(monkeypatch, tmp_path):
    import contextlib

    @contextlib.contextmanager
    def mock_file_lock(name, non_blocking=True):
        yield False

    monkeypatch.setattr(comeco, "file_lock", mock_file_lock)
    added = comeco.run()
    assert added == 0


def test_freshness_detection_fresh_and_stale():
    """Verify freshness checker identifies periods <= 7 days as fresh, > 7 as stale."""
    today = date(2026, 11, 16)
    # 4 days ago -> fresh
    is_stale, days = comeco._check_freshness(date(2026, 11, 12), today=today)
    assert not is_stale
    assert days == 4

    # 10 days ago -> stale
    is_stale, days = comeco._check_freshness(date(2026, 11, 6), today=today)
    assert is_stale
    assert days == 10


def test_run_detects_and_logs_stale_data(monkeypatch, tmp_path):
    """When announcement is older than max_age_days, collector logs source_stale event and never writes."""
    csv_file = tmp_path / "data_price_petro.csv"
    monkeypatch.setattr(comeco, "_csv_path", lambda: csv_file)
    # Announcement date is 2026-10-01 (over a month old)
    monkeypatch.setattr(comeco, "_fetch_html", lambda **kwargs: valid_comeco_html("01/10/2026"))

    events = []
    original_log = comeco._log

    def capture_log(event, **kwargs):
        events.append((event, kwargs))
        original_log(event, **kwargs)

    monkeypatch.setattr(comeco, "_log", capture_log)

    added = comeco.run()
    assert added == 0
    assert not csv_file.exists()

    stale_events = [e for e in events if e[0] == "source_stale"]
    assert len(stale_events) == 1
    assert stale_events[0][1]["category"] == "stale_data"
    assert stale_events[0][1]["effective_date"] == "2026-10-01"


def test_run_does_not_append_stale_announcement_even_when_newer_than_csv(monkeypatch, tmp_path):
    """When existing CSV contains an older date and COMECO returns a newer-than-CSV but stale announcement,
    the collector must stop before appending any rows."""
    csv_file = tmp_path / "comeco_fuel_prices.csv"
    csv_file.write_text(
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-09-01,2026-09-01,15:00,Xăng E10 RON 95-III,19000,19.000 đ,0,0,Đồng/lít thực tế,true,https://comeco.vn/,2026-09-01T08:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(comeco, "_csv_path", lambda: csv_file)
    # Announcement date is 01/10/2026 (newer than CSV's 2026-09-01, but stale relative to current_date 2026-11-16)
    monkeypatch.setattr(comeco, "_fetch_html", lambda **kwargs: valid_comeco_html("01/10/2026", "15:00"))

    events = []
    original_log = comeco._log

    def capture_log(event, **kwargs):
        events.append((event, kwargs))
        original_log(event, **kwargs)

    monkeypatch.setattr(comeco, "_log", capture_log)

    added = comeco.run(current_date=date(2026, 11, 16))
    assert added == 0

    # Verify CSV was not modified (still header + 1 original row)
    with csv_file.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["effective_date"] == "2026-09-01"

    stale_events = [e for e in events if e[0] == "source_stale"]
    assert len(stale_events) == 1
    assert stale_events[0][1]["effective_date"] == "2026-10-01"
    assert stale_events[0][1]["days_since_announcement"] == 46


def test_future_effective_date_raises_value_error():
    """Future effective dates must be rejected."""
    # Effective date 2099-01-01 is in the future
    html = valid_comeco_html("01/01/2099")
    with pytest.raises(ValueError, match="is in the future"):
        comeco._parse_comeco_html(html)


def test_run_appends_when_same_date_but_different_time(monkeypatch, tmp_path):
    """Observations on the same calendar date with different effective times are not duplicates."""
    csv_file = tmp_path / "data_price_petro.csv"
    # Seed 00:00 morning adjustment on 2026-11-12
    csv_file.write_text(
        "date,effective_date,effective_time,product_name,price_vnd_per_liter,price_display,price_change_vnd,price_change_display,unit,includes_vat_and_environmental_tax,source_url,extracted_at_utc\n"
        "2026-11-12,2026-11-12,00:00,Xăng E10 RON 95-III,19800,19.800 đ,0,0,Đồng/lít thực tế,true,https://comeco.vn/,2026-11-12T00:00:00Z\n"
        "2026-11-12,2026-11-12,00:00,Xăng E5 RON 92-II,19300,19.300 đ,0,0,Đồng/lít thực tế,true,https://comeco.vn/,2026-11-12T00:00:00Z\n"
        "2026-11-12,2026-11-12,00:00,\"Dầu DO 0,001S-V\",28500,28.500 đ,0,0,Đồng/lít thực tế,true,https://comeco.vn/,2026-11-12T00:00:00Z\n"
        "2026-11-12,2026-11-12,00:00,\"Dầu DO 0,05S-II\",26700,26.700 đ,0,0,Đồng/lít thực tế,true,https://comeco.vn/,2026-11-12T00:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(comeco, "_csv_path", lambda: csv_file)
    # New announcement is on the SAME date 12/11/2026, but at 15:00
    monkeypatch.setattr(comeco, "_fetch_html", lambda **kwargs: valid_comeco_html("12/11/2026", "15:00"))

    added = comeco.run()
    assert added == 4

    rows = []
    with csv_file.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 8  # 4 morning + 4 afternoon
    assert rows[0]["effective_time"] == "00:00"
    assert rows[4]["effective_time"] == "15:00"
