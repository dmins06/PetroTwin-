from datetime import date

import pytest
import requests

from petrotwin.collectors import fx


def unavailable_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "fx:\n  source_status: unavailable\n  retry_count: 3\n  retry_delay_seconds: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fx, "_repo_root", lambda: tmp_path)


def verified_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "fx:\n"
        "  source_id: algolab_sbv_central_rate\n"
        "  source_status: verified\n"
        f"  source_url: {fx.ALGOAB_URL}\n"
        "  retry_count: 2\n"
        "  retry_delay_seconds: 0\n"
        "  max_age_days: 7\n"
        "  request_timeout_seconds: 7\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fx, "_repo_root", lambda: tmp_path)


def test_unavailable_source_fails_closed(monkeypatch, tmp_path):
    unavailable_config(monkeypatch, tmp_path)
    monkeypatch.setattr(fx.requests, "get", lambda *args, **kwargs: pytest.fail("HTTP must not be called"))
    with pytest.raises(fx.FXSourceUnavailableError, match="commercial-bank"):
        fx._fetch_with_retries()


def test_verified_source_timeout_is_retried(monkeypatch, tmp_path):
    verified_config(monkeypatch, tmp_path)
    attempts = []

    def timeout(*args, **kwargs):
        attempts.append(kwargs)
        raise requests.Timeout("slow")

    monkeypatch.setattr(fx.requests, "get", timeout)
    with pytest.raises(requests.Timeout):
        fx._fetch_with_retries()
    assert len(attempts) == 3
    assert all(call["timeout"] == 7 for call in attempts)


@pytest.mark.parametrize("payload", [
    {"rate_vnd_per_usd": 24500, "unit": fx.UNIT, "rate_type": fx.RATE_TYPE},
    {"observation_date": "2026-01-01", "unit": fx.UNIT, "rate_type": fx.RATE_TYPE},
    {"observation_date": "2026-01-01", "rate_vnd_per_usd": 24500,
     "unit": "bank transfer VND/USD", "rate_type": fx.RATE_TYPE},
    {"observation_date": "2026-01-01", "rate_vnd_per_usd": 24500,
     "unit": fx.UNIT, "rate_type": "bank transfer rate"},
    {"observation_date": "not-a-date", "rate_vnd_per_usd": 24500,
     "unit": fx.UNIT, "rate_type": fx.RATE_TYPE},
])
def test_missing_or_wrong_schema_is_not_written(payload):
    with pytest.raises(fx.FXSourceSchemaError):
        fx._parse_verified_rate(payload)


def test_valid_verified_payload_is_parsed_and_date_is_checked():
    rate, observed = fx._parse_verified_rate({
        "observation_date": "2026-01-02",
        "rate_vnd_per_usd": "24,500",
        "unit": fx.UNIT,
        "rate_type": fx.RATE_TYPE,
    })
    assert rate == 24500
    assert observed == date(2026, 1, 2)


def test_no_fake_row_on_unavailable_source(monkeypatch, tmp_path):
    unavailable_config(monkeypatch, tmp_path)
    csv_path = tmp_path / "usd_vnd_rates.csv"
    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {"today": classmethod(lambda cls: date(2026, 1, 5))}))

    with pytest.raises(SystemExit) as exc:
        fx.run()
    assert exc.value.code == 1
    assert not csv_path.exists()


def algolab_html(rate="25,627", observation_date="04/11/2026"):
    return f"""
    <html><body>
      <h1>Tỷ giá SBV trung tâm</h1>
      <div>VND · Nguồn: GSO / SBV</div>
      <div>Mới nhất</div><div>{rate}</div><div>{observation_date}</div>
    </body></html>
    """


def test_algolab_html_is_parsed(monkeypatch):
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {
        "today": classmethod(lambda cls: date(2026, 11, 4))
    }))
    rate, observation_date = fx._parse_algolab_html(algolab_html())
    assert (rate, observation_date) == (25627, date(2026, 11, 4))


def test_algolab_html_structure_change_fails_closed():
    with pytest.raises(fx.FXSourceSchemaError, match="structure changed"):
        fx._parse_algolab_html("<html><body>Tỷ giá USD</body></html>")


@pytest.mark.parametrize("html", [
    algolab_html("25,627", "31/02/2026"),
    algolab_html("not-a-rate", "04/11/2026"),
    algolab_html("0", "04/11/2026"),
])
def test_algolab_invalid_date_or_rate_fails_closed(html):
    with pytest.raises(fx.FXSourceSchemaError):
        fx._parse_algolab_html(html)


def test_commercial_buy_sell_rates_are_not_selected():
    html = algolab_html() + """
    <div>Tỷ giá mua</div><div>25,800</div>
    <div>Tỷ giá bán</div><div>25,900</div>
    """
    assert fx._parse_algolab_html(html)[0] == 25627


def test_stale_algolab_data_is_rejected(monkeypatch):
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {
        "today": classmethod(lambda cls: date(2026, 11, 10))
    }))
    with pytest.raises(fx.FXSourceSchemaError, match="stale"):
        fx._validate_freshness(date(2026, 11, 1), 7)


def test_duplicate_observation_date_is_not_written(monkeypatch, tmp_path):
    csv_path = tmp_path / "usd_vnd_rates.csv"
    csv_path.write_text("date,rate_vnd_per_usd\n2026-11-04,25627\n", encoding="utf-8")
    before = csv_path.read_text(encoding="utf-8")
    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    assert fx._date_exists_in_csv(csv_path, date(2026, 11, 4))
    assert csv_path.read_text(encoding="utf-8") == before


def test_failed_collection_does_not_modify_existing_history(monkeypatch, tmp_path):
    csv_path = tmp_path / "usd_vnd_rates.csv"
    original = "date,rate_vnd_per_usd\n2026-10-30,25630\n"
    csv_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    monkeypatch.setattr(fx, "_fetch_with_retries", lambda: (_ for _ in ()).throw(fx.FXSourceSchemaError("parse failed")))
    assert csv_path.read_text(encoding="utf-8") == original


def realistic_algolab_html(
    rate="25,627",
    observation_date="04/11/2026",
    delta="+15",
    delta_pct="+0.06%",
    time_str="08:30",
) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head><title>Tỷ giá SBV trung tâm | Algolab Macro</title></head>
    <body>
      <header>
        <nav class="navbar">
          <div class="brand">Algolab Macro Data</div>
          <div class="breadcrumb">Trang chủ / Vĩ mô / Tỷ giá USD - SBV trung tâm</div>
        </nav>
      </header>
      <main class="container">
        <section class="page-header">
          <h1 class="title">Tỷ giá SBV trung tâm</h1>
          <div class="meta">
            <span class="unit">Đơn vị: VND per 1 USD</span>
            <span class="source">Nguồn: GSO / SBV</span>
            <span class="frequency">Tần suất: Hàng ngày làm việc</span>
          </div>
        </section>

        <section class="cards-grid">
          <!-- Metric card: Latest central rate -->
          <div class="card card-primary" id="latest-card">
            <div class="card-header">
              <span class="badge">#1 Chỉ số</span>
              <span class="card-title">Mới nhất lúc {time_str}</span>
            </div>
            <div class="card-body">
              <div class="rate-display">
                <span class="currency-symbol">USD/VND:</span>
                <span class="rate-value text-bold">{rate}</span>
              </div>
              <div class="rate-change text-success">
                <span class="delta-abs">{delta} VND</span>
                <span class="delta-pct">({delta_pct})</span>
              </div>
            </div>
            <div class="card-footer">
              <span class="label">Ngày công bố:</span>
              <time class="obs-date" datetime="2026-11-04">{observation_date}</time>
            </div>
          </div>

          <!-- Secondary metric card with other numbers that should not be captured -->
          <div class="card" id="range-card">
            <div class="card-title">Biên độ 52 tuần</div>
            <div class="high">Cao nhất: 25,650</div>
            <div class="low">Thấp nhất: 23,800</div>
            <div class="avg">Trung bình: 24,500</div>
          </div>
        </section>

        <!-- Unrelated market rates table (commercial rates) -->
        <section class="commercial-rates-section">
          <h2>Tỷ giá tham khảo ngân hàng thương mại</h2>
          <table>
            <thead>
              <tr><th>Ngân hàng</th><th>Tỷ giá mua</th><th>Tỷ giá bán</th></tr>
            </thead>
            <tbody>
              <tr><td>Vietcombank</td><td>25,800</td><td>25,900</td></tr>
              <tr><td>BIDV</td><td>25,810</td><td>25,910</td></tr>
            </tbody>
          </table>
        </section>
      </main>
    </body>
    </html>
    """


def test_realistic_algolab_html_is_parsed(monkeypatch):
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {
        "today": classmethod(lambda cls: date(2026, 11, 4))
    }))
    html = realistic_algolab_html()
    rate, observation_date = fx._parse_algolab_html(html)
    assert rate == 25627
    assert observation_date == date(2026, 11, 4)


def test_unrelated_numbers_are_not_captured_as_rate(monkeypatch):
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {
        "today": classmethod(lambda cls: date(2026, 11, 4))
    }))
    html = realistic_algolab_html(rate="25,627", delta="-50", delta_pct="-0.19%", time_str="09:15")
    rate, observation_date = fx._parse_algolab_html(html)
    assert rate == 25627
    assert observation_date == date(2026, 11, 4)


def test_provenance_is_provider_declared(monkeypatch, tmp_path):
    verified_config(monkeypatch, tmp_path)
    csv_path = tmp_path / "usd_vnd_rates.csv"
    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {
        "today": classmethod(lambda cls: date(2026, 11, 4))
    }))
    monkeypatch.setattr(fx, "_fetch_with_retries", lambda: (25627, date(2026, 11, 4)))

    rows_added = fx.run()
    assert rows_added == 1
    content = csv_path.read_text(encoding="utf-8")
    assert "provenance_status=provider_declared" in content
    assert "upstream=State Bank of Vietnam" in content
    assert "provider=Algolab" in content
