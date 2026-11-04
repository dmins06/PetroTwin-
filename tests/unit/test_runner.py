import pytest

from petrotwin.collectors import runner


def run_runner(monkeypatch, tmp_path, results, config_text):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: results[module])
    monkeypatch.setattr(runner, "_run_pipeline", lambda: ("ok", None))
    monkeypatch.setattr(runner, "_run_loader", lambda: ("ok", None))
    monkeypatch.delenv("PETROTWIN_DB_HOST", raising=False)
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    return exc.value.code


def test_optional_fx_failure_runs_pipeline(monkeypatch, tmp_path):
    code = run_runner(monkeypatch, tmp_path, {
        "petrotwin.collectors.brent": (1, None),
        "petrotwin.collectors.fx": (-1, "FX unavailable"),
        "petrotwin.collectors.domestic_fuel": (2, None),
    }, "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n")
    assert code == 1
    log = (tmp_path / "logs" / "collector_run.log").read_text(encoding="utf-8")
    assert '"optional_collectors_failed": ["fx"]' in log
    assert '"overall_status": "partial"' in log
    assert '"pipeline_status": "ok"' in log


def test_required_failure_skips_pipeline(monkeypatch, tmp_path):
    calls = []
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: (-1, "failed") if module == "petrotwin.collectors.brent" else (1, None))
    monkeypatch.setattr(runner, "_run_pipeline", lambda: calls.append("pipeline") or ("ok", None))
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit):
        runner.main()
    assert calls == []
