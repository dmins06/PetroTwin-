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
        "  source_status: verified\n"
        "  source_url: https://official.example/sbv-rate\n"
        "  source_attribution: Verified publisher attribution\n"
        "  retry_count: 2\n"
        "  retry_delay_seconds: 0\n"
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
        fx._parse_verified_rate(payload, {})


def test_valid_verified_payload_is_parsed_and_date_is_checked():
    rate, observed = fx._parse_verified_rate({
        "observation_date": "2026-01-02",
        "rate_vnd_per_usd": "24,500",
        "unit": fx.UNIT,
        "rate_type": fx.RATE_TYPE,
    }, {})
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
