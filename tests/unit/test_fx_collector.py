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

def algolab_jsonld_html(
    rate="25.639",
    observation_date="16/11/2026",
    date_modified="2026-11-16",
    source="GSO / SBV",
    unit="VND",
):
    description = (
        f"Tỷ giá USD: Tỷ giá SBV trung tâm — số liệu theo ngày "
        f"của Việt Nam, đơn vị VND. Mới nhất {rate} VND "
        f"({observation_date}). Nguồn: {source}."
    )
    dataset = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Tỷ giá USD: Tỷ giá SBV trung tâm (Việt Nam)",
        "description": description,
        "dateModified": date_modified,
        "variableMeasured": {
            "@type": "PropertyValue",
            "name": "Tỷ giá USD: Tỷ giá SBV trung tâm",
            "unitText": unit,
        },
        "isBasedOn": {
            "@type": "CreativeWork",
            "name": source,
        },
    }
    import json

    return f"""
    <html lang="vi">
      <head>
        <script type="application/ld+json">{json.dumps(dataset, ensure_ascii=False)}</script>
      </head>
      <body>
        <h1>Tỷ giá USD: Tỷ giá SBV trung tâm</h1>
        <div>25,639</div>
        <div>16/11/2026</div>
      </body>
    </html>
    """


def test_algolab_jsonld_dataset_is_parsed(monkeypatch):
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {
        "today": classmethod(lambda cls: date(2026, 11, 16))
    }))
    assert fx._parse_algolab_html(algolab_jsonld_html()) == (
        25639,
        date(2026, 11, 16),
    )


@pytest.mark.parametrize("html", [
    algolab_jsonld_html(source="Vietcombank"),
    algolab_jsonld_html(unit="USD"),
    algolab_jsonld_html(date_modified="2026-11-15"),
])
def test_algolab_jsonld_invalid_metadata_fails_closed(monkeypatch, html):
    monkeypatch.setattr(fx, "date", type("FrozenDate", (date,), {
        "today": classmethod(lambda cls: date(2026, 11, 16))
    }))
    with pytest.raises(fx.FXSourceSchemaError):
        fx._parse_algolab_html(html)


def test_algolab_tv_symbol_validation_accepts_sbv_and_rejects_commercial():
    valid_meta = {
        "name": "VN_FX_RATE_TY_GIA_SBV_TRUNG_TAM",
        "ticker": "VN_FX_RATE_TY_GIA_SBV_TRUNG_TAM",
        "description": "Tỷ giá USD: Tỷ giá SBV trung tâm (VND)",
        "type": "economy",
    }
    fx._validate_algolab_tv_symbol(valid_meta)

    with pytest.raises(fx.FXSourceSchemaError):
        fx._validate_algolab_tv_symbol({
            "name": "VN_FX_RATE_TY_GIA_SBV_TRUNG_TAM",
            "ticker": "VN_FX_RATE_TY_GIA_SBV_TRUNG_TAM",
            "description": "Tỷ giá USD: Vietcombank",
            "type": "economy",
        })


def test_fx_backfill_recovers_missing_dates_preserves_existing_and_reports_unrecoverable(monkeypatch, tmp_path):
    import csv
    from datetime import datetime, timezone

    verified_config(monkeypatch, tmp_path)
    csv_path = tmp_path / "usd_vnd_rates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fx.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows([
            {
                "date": "2026-10-22",
                "base_currency": "USD",
                "quote_currency": "VND",
                "rate_vnd_per_usd": "25632",
                "unit": fx.UNIT,
                "rate_type": fx.RATE_TYPE,
                "source_url": fx.ALGOLAB_URL,
                "source_attribution": "Algolab page identifies source as GSO / State Bank of Vietnam (SBV)",
                "source_value": "25,632",
                "extracted_at_utc": "2026-10-23T06:02:10Z",
            },
            {
                "date": "2026-10-28",
                "base_currency": "USD",
                "quote_currency": "VND",
                "rate_vnd_per_usd": "25631",
                "unit": fx.UNIT,
                "rate_type": fx.RATE_TYPE,
                "source_url": fx.ALGOLAB_URL,
                "source_attribution": "provider=Algolab; source_id=algolab_sbv_central_rate; upstream=State Bank of Vietnam; provenance_status=provider_declared",
                "source_value": "25,631",
                "extracted_at_utc": "2026-10-28T09:00:00Z",
            },
        ])

    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    # Upstream returns 2026-10-22, 2026-10-23, 2026-10-27, 2026-10-28 (2026-10-26 is absent -> holiday/unrecoverable)
    monkeypatch.setattr(
        fx,
        "_fetch_history_with_retries",
        lambda start, end, include_saturdays=False: [
            (date(2026, 10, 22), 25632),
            (date(2026, 10, 23), 25627),
            (date(2026, 10, 27), 25633),
            (date(2026, 10, 28), 25631),
        ],
    )

    # 1. Dry-run does not modify the CSV
    before = csv_path.read_bytes()
    dry_res = fx.backfill(start_date=date(2026, 10, 22), end_date=date(2026, 10, 28), dry_run=True)
    assert [r["date"] for r in dry_res["recovered_rows"]] == ["2026-10-23", "2026-10-27"]
    assert dry_res["unrecoverable_dates"] == ["2026-10-26"]
    assert csv_path.read_bytes() == before

    # 2. Write backfill inserts missing rows in chronological order without overwriting existing rows
    res = fx.backfill(start_date=date(2026, 10, 22), end_date=date(2026, 10, 28), dry_run=False)
    assert [r["date"] for r in res["recovered_rows"]] == ["2026-10-23", "2026-10-27"]
    assert res["unrecoverable_dates"] == ["2026-10-26"]

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["date"] for r in rows] == ["2026-10-22", "2026-10-23", "2026-10-27", "2026-10-28"]
    # Existing 2026-10-22 row attribution and timestamp preserved verbatim
    assert rows[0]["source_attribution"] == "Algolab page identifies source as GSO / State Bank of Vietnam (SBV)"
    assert rows[0]["extracted_at_utc"] == "2026-10-23T06:02:10Z"
    assert rows[1]["rate_vnd_per_usd"] == "25627"
    assert "provenance_status=provider_declared" in rows[1]["source_attribution"]
    assert rows[2]["rate_vnd_per_usd"] == "25633"

    # 3. Repeated backfill is idempotent
    after = csv_path.read_bytes()
    res2 = fx.backfill(start_date=date(2026, 10, 22), end_date=date(2026, 10, 28), dry_run=False)
    assert res2["recovered_rows"] == []
    assert csv_path.read_bytes() == after


def test_fx_run_catches_up_missed_working_days_and_is_idempotent(monkeypatch, tmp_path):
    """Regression: When daily runs were missed (e.g. 2026-10-22 -> 2026-11-16), fx.run()
    must automatically catch up the missed SBV working days alongside the latest rate."""
    import csv

    verified_config(monkeypatch, tmp_path)
    csv_path = tmp_path / "usd_vnd_rates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fx.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(
            fx._build_csv_row(
                date(2026, 10, 22),
                25632,
                fx.ALGOLAB_URL,
                "2026-10-23T06:02:10Z",
            )
        )

    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    monkeypatch.setattr(
        fx,
        "date",
        type("FrozenDate", (date,), {"today": classmethod(lambda cls: date(2026, 11, 16))}),
    )
    monkeypatch.setattr(fx, "_fetch_with_retries", lambda: (25639, date(2026, 11, 16)))
    monkeypatch.setattr(
        fx,
        "_fetch_history_with_retries",
        lambda start, end, include_saturdays=False: [
            (date(2026, 11, 11), 25611),
            (date(2026, 11, 12), 25623),
            (date(2026, 11, 13), 25637),
            (date(2026, 11, 16), 25639),
        ],
    )

    added = fx.run()
    assert added == 4

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["date"] for r in rows] == [
        "2026-10-22",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-16",
    ]
    assert [r["rate_vnd_per_usd"] for r in rows] == ["25632", "25611", "25623", "25637", "25639"]

    # Repeated run is idempotent
    after = csv_path.read_bytes()
    assert fx.run() == 0
    assert csv_path.read_bytes() == after


def test_fx_run_degrades_without_advancing_date_on_history_failure_and_recovers_on_retry(monkeypatch, tmp_path):
    """Fail-closed catch-up: when history catch-up times out, fx.run() preserves CSV and last_date,
    logs history_catchup_degraded, exits 1, and recovers all missing dates on the next run."""
    import csv

    verified_config(monkeypatch, tmp_path)
    csv_path = tmp_path / "usd_vnd_rates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fx.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(
            fx._build_csv_row(
                date(2026, 10, 22),
                25632,
                fx.ALGOLAB_URL,
                "2026-10-23T06:02:10Z",
            )
        )
    before_bytes = csv_path.read_bytes()

    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    monkeypatch.setattr(
        fx,
        "date",
        type("FrozenDate", (date,), {"today": classmethod(lambda cls: date(2026, 11, 16))}),
    )
    monkeypatch.setattr(fx, "_fetch_with_retries", lambda: (25639, date(2026, 11, 16)))

    events = []
    orig_log = fx._log
    monkeypatch.setattr(fx, "_log", lambda ev, **kw: (events.append((ev, kw)), orig_log(ev, **kw)))

    # Attempt 1: history endpoint times out -> fail closed, CSV unchanged, last_date remains 2026-10-22
    monkeypatch.setattr(
        fx,
        "_fetch_history_with_retries",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout("history timeout")),
    )
    with pytest.raises(SystemExit) as exc:
        fx.run()
    assert exc.value.code == 1
    assert csv_path.read_bytes() == before_bytes
    assert fx._last_date_in_csv(csv_path) == date(2026, 10, 22)
    degraded = [kw for ev, kw in events if ev == "history_catchup_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["reason"] == "history_catchup_failed"
    assert degraded[0]["preserved_last_date"] == "2026-10-22"

    # Attempt 2: history endpoint succeeds -> catches up missed working days + observation_date
    monkeypatch.setattr(
        fx,
        "_fetch_history_with_retries",
        lambda start, end, include_saturdays=False: [
            (date(2026, 11, 11), 25611),
            (date(2026, 11, 12), 25623),
            (date(2026, 11, 13), 25637),
            (date(2026, 11, 16), 25639),
        ],
    )
    added = fx.run()
    assert added == 4
    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["date"] for r in rows] == [
        "2026-10-22",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-16",
    ]
    assert [r["rate_vnd_per_usd"] for r in rows] == ["25632", "25611", "25623", "25637", "25639"]

    # Attempt 3: idempotent repeat
    after_recovery = csv_path.read_bytes()
    assert fx.run() == 0
    assert csv_path.read_bytes() == after_recovery


@pytest.mark.parametrize("history_payload", [
    [],  # empty / no_data
    [(date(2026, 11, 11), 25611), (date(2026, 11, 12), 25623)],  # missing observation_date 2026-11-16
])
def test_fx_run_rejects_incomplete_history_missing_observation_date_or_empty(
    monkeypatch, tmp_path, history_payload
):
    """Incomplete history responses (empty/no_data or missing confirmed observation_date) must fail
    closed without modifying CSV or advancing last_date."""
    import csv

    verified_config(monkeypatch, tmp_path)
    csv_path = tmp_path / "usd_vnd_rates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fx.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(
            fx._build_csv_row(
                date(2026, 10, 22),
                25632,
                fx.ALGOLAB_URL,
                "2026-10-23T06:02:10Z",
            )
        )
    before_bytes = csv_path.read_bytes()

    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    monkeypatch.setattr(
        fx,
        "date",
        type("FrozenDate", (date,), {"today": classmethod(lambda cls: date(2026, 11, 16))}),
    )
    monkeypatch.setattr(fx, "_fetch_with_retries", lambda: (25639, date(2026, 11, 16)))
    monkeypatch.setattr(
        fx,
        "_fetch_history_with_retries",
        lambda start, end, include_saturdays=False: history_payload,
    )

    events = []
    orig_log = fx._log
    monkeypatch.setattr(fx, "_log", lambda ev, **kw: (events.append((ev, kw)), orig_log(ev, **kw)))

    with pytest.raises(SystemExit) as exc:
        fx.run()
    assert exc.value.code == 1
    assert csv_path.read_bytes() == before_bytes
    degraded = [kw for ev, kw in events if ev == "history_catchup_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["reason"] == "incomplete_history_response"


def test_fx_run_fails_closed_on_rate_conflict_between_html_and_history(monkeypatch, tmp_path):
    """When HTML and UDF history disagree on observation_date's rate, fx.run() must fail closed
    without swallowing FXSourceSchemaError or modifying CSV."""
    import csv

    verified_config(monkeypatch, tmp_path)
    csv_path = tmp_path / "usd_vnd_rates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fx.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(
            fx._build_csv_row(
                date(2026, 10, 22),
                25632,
                fx.ALGOLAB_URL,
                "2026-10-23T06:02:10Z",
            )
        )
    before_bytes = csv_path.read_bytes()

    monkeypatch.setattr(fx, "_csv_path", lambda: csv_path)
    monkeypatch.setattr(
        fx,
        "date",
        type("FrozenDate", (date,), {"today": classmethod(lambda cls: date(2026, 11, 16))}),
    )
    monkeypatch.setattr(fx, "_fetch_with_retries", lambda: (25639, date(2026, 11, 16)))
    monkeypatch.setattr(
        fx,
        "_fetch_history_with_retries",
        lambda start, end, include_saturdays=False: [
            (date(2026, 11, 13), 25637),
            (date(2026, 11, 16), 25700),  # Conflicts with HTML 25639!
        ],
    )

    with pytest.raises(SystemExit) as exc:
        fx.run()
    assert exc.value.code == 1
    assert csv_path.read_bytes() == before_bytes


def test_parse_algolab_tv_history_rejects_conflicting_duplicates_and_invalid_values(monkeypatch):
    """_parse_algolab_tv_history must fail closed on conflicting duplicates, out-of-range rates,
    non-integer rates, future dates, and non-ok status."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        fx,
        "date",
        type("FrozenDate", (date,), {"today": classmethod(lambda cls: date(2026, 11, 16))}),
    )
    ts_nov12 = int(datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp())
    ts_nov17 = int(datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp())

    # 1. Non-ok status
    with pytest.raises(fx.FXSourceSchemaError, match="invalid status"):
        fx._parse_algolab_tv_history({"s": "error", "t": [], "c": []})

    # 2. Conflicting duplicates for the same date
    with pytest.raises(fx.FXSourceSchemaError, match="Conflicting Algolab UDF history values"):
        fx._parse_algolab_tv_history({"s": "ok", "t": [ts_nov12, ts_nov12], "c": [25623, 25699]})

    # 3. Non-integer rate
    with pytest.raises(fx.FXSourceSchemaError, match="not an integer"):
        fx._parse_algolab_tv_history({"s": "ok", "t": [ts_nov12], "c": [25623.5]})

    # 4. Out-of-range rate
    with pytest.raises(fx.FXSourceSchemaError, match="outside valid USD/VND range"):
        fx._parse_algolab_tv_history({"s": "ok", "t": [ts_nov12], "c": [500]})

    # 5. Future date
    with pytest.raises(fx.FXSourceSchemaError, match="future date"):
        fx._parse_algolab_tv_history({"s": "ok", "t": [ts_nov17], "c": [25639]})


def test_insert_rows_chronologically_rejects_conflicting_duplicate_dates(tmp_path):
    """_insert_rows_chronologically must fail closed on conflicting duplicate dates in input or vs CSV."""
    import csv

    csv_path = tmp_path / "usd_vnd_rates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fx.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(
            fx._build_csv_row(date(2026, 11, 11), 25611, fx.ALGOLAB_URL, "2026-11-16T00:00:00Z")
        )
    before_bytes = csv_path.read_bytes()

    # Conflict between two new rows in input
    with pytest.raises(fx.FXSourceSchemaError, match="Conflicting duplicate FX rows"):
        fx._insert_rows_chronologically(
            csv_path,
            [
                fx._build_csv_row(date(2026, 11, 12), 25623, fx.ALGOLAB_URL, "2026-11-16T00:00:00Z"),
                fx._build_csv_row(date(2026, 11, 12), 25999, fx.ALGOLAB_URL, "2026-11-16T00:00:00Z"),
            ],
        )
    assert csv_path.read_bytes() == before_bytes

    # Conflict between new row and existing CSV row
    with pytest.raises(fx.FXSourceSchemaError, match="Existing FX rate"):
        fx._insert_rows_chronologically(
            csv_path,
            [fx._build_csv_row(date(2026, 11, 11), 25999, fx.ALGOLAB_URL, "2026-11-16T00:00:00Z")],
        )
    assert csv_path.read_bytes() == before_bytes
