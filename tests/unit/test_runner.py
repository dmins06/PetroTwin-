import pytest

from petrotwin.collectors import runner


def run_runner(monkeypatch, tmp_path, results, config_text):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: results[module])
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("ok", None))
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
    assert code == 0
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
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("ok", None))
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit):
        runner.main()
    assert calls == []


def test_collector_timeout_is_reported(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(runner.subprocess, "run", timed_out)
    rows, error = runner._run_collector(
        "petrotwin.collectors.brent", retry_count=0, timeout_seconds=5
    )
    assert rows == -1
    assert "timed out" in error


def test_pipeline_failure_is_not_success(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: (1, None))
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("failed", "pipeline exploded"))
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1
    log = (tmp_path / "logs" / "collector_run.log").read_text()
    assert '"overall_status": "failed"' in log
    assert "pipeline exploded" in log


def test_loader_failure_is_not_success(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: (1, None))
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("ok", None))
    monkeypatch.setattr(runner, "_run_loader", lambda: ("failed", "loader exploded"))
    monkeypatch.setenv("PETROTWIN_DB_HOST", "db.example")
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1
    log = (tmp_path / "logs" / "collector_run.log").read_text()
    assert '"overall_status": "failed"' in log
    assert "loader exploded" in log


def test_unexpected_pipeline_status_is_failure(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: (1, None))
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("corrupted_output", None))
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1
    log = (tmp_path / "logs" / "collector_run.log").read_text()
    assert '"overall_status": "failed"' in log
    assert '"pipeline_status": "corrupted_output"' in log


def test_unexpected_loader_status_is_failure(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: (1, None))
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("ok", None))
    monkeypatch.setattr(runner, "_run_loader", lambda: ("partial_db_commit", None))
    monkeypatch.setenv("PETROTWIN_DB_HOST", "db.example")
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1
    log = (tmp_path / "logs" / "collector_run.log").read_text()
    assert '"overall_status": "failed"' in log
    assert '"loader_status": "partial_db_commit"' in log


def test_optional_failure_with_unexpected_pipeline_is_failure(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", lambda module, **kwargs: (-1, "FX unavailable") if module == "petrotwin.collectors.fx" else (1, None))
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("unexpected_pipeline_status", None))
    monkeypatch.setattr(runner.sys, "argv", ["runner"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1
    log = (tmp_path / "logs" / "collector_run.log").read_text()
    assert '"overall_status": "failed"' in log


def test_pipeline_only_skips_collectors_and_runs_pipeline(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    collector_calls = []

    def mock_collector(module, **kwargs):
        collector_calls.append(module)
        return (1, None)

    pipeline_calls = []

    def mock_pipeline(*args):
        pipeline_calls.append(args)
        return ("ok", None)

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", mock_collector)
    monkeypatch.setattr(runner, "_run_pipeline", mock_pipeline)
    monkeypatch.delenv("PETROTWIN_DB_HOST", raising=False)
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--pipeline-only"])

    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 0
    assert collector_calls == []  # Collectors must NOT be called in pipeline-only mode
    assert len(pipeline_calls) == 1

    log = (tmp_path / "logs" / "collector_run.log").read_text(encoding="utf-8")
    assert '"mode": "pipeline_only"' in log
    assert '"pipeline_status": "ok"' in log
    assert '"loader_status": "no_db_config"' in log
    assert '"overall_status": "success"' in log


def test_pipeline_only_fails_when_pipeline_fails(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("failed", "schema error"))
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--pipeline-only"])

    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1

    log = (tmp_path / "logs" / "collector_run.log").read_text(encoding="utf-8")
    assert '"mode": "pipeline_only"' in log
    assert '"pipeline_status": "failed"' in log
    assert '"overall_status": "failed"' in log
    assert "schema error" in log


def test_dry_run_skips_pipeline_and_loader(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ndomestic_fuel:\n  optional: false\n",
        encoding="utf-8",
    )
    collector_calls = []

    def mock_collector(module, **kwargs):
        collector_calls.append(module)
        return (5, None)

    pipeline_calls = []

    def mock_pipeline(*args):
        pipeline_calls.append(args)
        return ("ok", None)

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_utcnow_iso", lambda: "2026-11-02T00:00:00Z")
    monkeypatch.setattr(runner, "_run_collector", mock_collector)
    monkeypatch.setattr(runner, "_run_pipeline", mock_pipeline)
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--dry-run"])

    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 0
    assert len(collector_calls) == 3
    assert pipeline_calls == []  # Pipeline must NOT be called in dry-run mode

    log = (tmp_path / "logs" / "collector_run.log").read_text(encoding="utf-8")
    assert '"mode": "dry_run"' in log
    assert '"pipeline_status": "skipped"' in log
    assert '"loader_status": "skipped"' in log
    assert '"overall_status": "success"' in log


def test_conflicting_pipeline_only_and_dry_run(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--pipeline-only", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2


def test_runner_lock_busy_exits_cleanly(monkeypatch, tmp_path):
    import contextlib

    @contextlib.contextmanager
    def mock_file_lock(name, non_blocking=True):
        yield False  # Simulate lock is already held by another process

    monkeypatch.setattr(runner, "file_lock", mock_file_lock)
    monkeypatch.setattr(runner.sys, "argv", ["runner"])

    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 0


def test_load_env_file_populates_missing_vars(monkeypatch, tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text(
        "# Comment line\n"
        "PETROTWIN_DB_HOST=192.168.1.50\n"
        "export PETROTWIN_DB_PORT=3307\n"
        "PETROTWIN_DB_USER='custom_user'\n"
        "PETROTWIN_DB_PASSWORD=\"custom_secret\"\n"
        "PETROTWIN_EXISTING=new_value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PETROTWIN_EXISTING", "original_value")
    monkeypatch.delenv("PETROTWIN_DB_HOST", raising=False)
    monkeypatch.delenv("PETROTWIN_DB_PORT", raising=False)
    monkeypatch.delenv("PETROTWIN_DB_USER", raising=False)
    monkeypatch.delenv("PETROTWIN_DB_PASSWORD", raising=False)

    runner._load_env_file(env_file)

    import os
    assert os.environ["PETROTWIN_DB_HOST"] == "192.168.1.50"
    assert os.environ["PETROTWIN_DB_PORT"] == "3307"
    assert os.environ["PETROTWIN_DB_USER"] == "custom_user"
    assert os.environ["PETROTWIN_DB_PASSWORD"] == "custom_secret"
    # Existing variables should not be overwritten
    assert os.environ["PETROTWIN_EXISTING"] == "original_value"
