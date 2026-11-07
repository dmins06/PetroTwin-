from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from petrotwin.collectors import domestic_fuel


def valid_pvoil_html(
    announcement_date="05/11/2026",
    effective_time="15h00",
) -> str:
    products = [
        ("Xăng RON 95-III", "21.500", "+150"),
        ("Xăng E5 RON 92-II", "20.200", "+120"),
        ("Xăng RON 95-IV", "21.800", "+150"),
        ("Xăng E10 RON 95-III", "21.400", "+140"),
        ("Dầu DO 0,05S-II", "18.900", "-80"),
        ("Dầu DO 0,005S-IV", "19.200", "-80"),
        ("Dầu DO 0,001S-V", "19.500", "-80"),
        ("Dầu KO", "18.500", "-50"),
        ("Dầu FO 3,5%S", "15.800", "+20"),
        ("Dầu FO 3%S", "16.100", "+20"),
    ]
    rows_html = "".join(
        f"<tr><td>{name}</td><td>{price} đ</td><td>{change} đ</td></tr>"
        for name, price, change in products
    )
    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Tin giá xăng dầu PVOIL</title></head>
    <body>
      <h1>Thông báo điều chỉnh giá xăng dầu ngày {announcement_date} từ {effective_time}</h1>
      <table>
        <thead>
          <tr><th>Tên sản phẩm</th><th>Giá bán lẻ</th><th>Thay đổi</th></tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </body>
    </html>
    """


def test_parse_date_from_text():
    assert domestic_fuel._parse_date_from_text("ngày 05/11/2026") == date(2026, 11, 5)
    assert domestic_fuel._parse_date_from_text("áp dụng 2026-11-05") == date(2026, 11, 5)
    assert domestic_fuel._parse_date_from_text("không có ngày") is None


def test_clean_number():
    assert domestic_fuel._clean_number("21.500 đ") == 21500
    assert domestic_fuel._clean_number(" 18,900 VND ") == 18900
    assert domestic_fuel._clean_number("15800") == 15800


def test_scrape_prices_all_known_products():
    html = valid_pvoil_html()
    ann_date, records = domestic_fuel._scrape_prices(html)
    assert ann_date == date(2026, 11, 5)
    assert len(records) == 10
    product_names = {r["product_name"] for r in records}
    assert product_names == set(domestic_fuel.KNOWN_PRODUCTS)


def test_scrape_prices_structure_change_fails_closed():
    html = "<html><body><h1>Không có bảng giá</h1><p>Website đang bảo trì</p></body></html>"
    with pytest.raises(ValueError, match="Could not parse any product price"):
        domestic_fuel._scrape_prices(html)


def test_domestic_fuel_run_idempotent(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    csv_file.write_text("date,effective_date\n2026-11-05,2026-11-05\n", encoding="utf-8")
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)
    monkeypatch.setattr(domestic_fuel, "_last_date_in_csv", lambda path: date(2026, 11, 5))
    monkeypatch.setattr(domestic_fuel, "_fetch_html", lambda: valid_pvoil_html("05/11/2026"))

    added = domestic_fuel.run()
    assert added == 0


def test_domestic_fuel_fails_closed_on_http_403(monkeypatch, tmp_path):
    csv_file = tmp_path / "data_price_petro.csv"
    csv_file.write_text("date,effective_date\n2026-10-29,2026-10-29\n", encoding="utf-8")
    original = csv_file.read_text(encoding="utf-8")
    monkeypatch.setattr(domestic_fuel, "_csv_path", lambda: csv_file)

    def mock_fetch():
        resp = MagicMock()
        resp.status_code = 403
        raise requests.HTTPError("403 Forbidden (Cloudflare Challenge)", response=resp)

    monkeypatch.setattr(domestic_fuel, "_fetch_html", mock_fetch)

    with pytest.raises(SystemExit) as exc:
        domestic_fuel.run()
    assert exc.value.code == 1
    # Existing CSV must remain intact
    assert csv_file.read_text(encoding="utf-8") == original
