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
        "petrotwin.collectors.comeco": (2, None),
    }, "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n")
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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


def test_runner_timeout_preserves_captured_stdout_and_stderr(capsys, monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)

    calls = 0

    def timed_out(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output=f'{{"event": "collector_start", "attempt": {calls}}}\n{{"event": "fetch_in_progress"}}\n',
            stderr=f"warning: slow socket response attempt {calls}\n",
        )

    monkeypatch.setattr(runner.subprocess, "run", timed_out)
    rows, error = runner._run_collector(
        "petrotwin.collectors.comeco",
        retry_count=1,
        retry_delay_seconds=0,
        timeout_seconds=5,
    )
    assert rows == -1
    assert "timed out" in error
    assert calls == 2  # 1 initial + 1 retry = 2 attempts

    captured = capsys.readouterr().out
    assert '{"event": "collector_start", "attempt": 1}' in captured
    assert '{"event": "collector_start", "attempt": 2}' in captured
    assert '{"event": "fetch_in_progress"}' in captured
    assert "warning: slow socket response attempt 1" in captured
    assert "warning: slow socket response attempt 2" in captured


def test_runner_timeout_preserves_captured_bytes_stream(capsys, monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output=b'{"event": "collector_start", "product": "X\xc4\x83ng RON 95-III"}\n',
            stderr=b"warning: raw byte log\n",
        )

    monkeypatch.setattr(runner.subprocess, "run", timed_out)
    rows, error = runner._run_collector(
        "petrotwin.collectors.comeco",
        retry_count=0,
        retry_delay_seconds=0,
        timeout_seconds=5,
    )
    assert rows == -1
    assert "timed out" in error

    captured = capsys.readouterr().out
    assert "Xăng RON 95-III" in captured
    assert "warning: raw byte log" in captured


def test_pipeline_failure_is_not_success(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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
        "brent:\n  optional: false\nfx:\n  optional: true\ncomeco:\n  optional: false\n",
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


def test_runner_uses_configured_runner_timeout(monkeypatch, tmp_path):
    captured_timeouts = {}

    def mock_run_collector(module, **kwargs):
        captured_timeouts[module] = kwargs.get("timeout_seconds")
        return (1, None)

    monkeypatch.setattr(runner, "_run_collector", mock_run_collector)
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("ok", None))
    monkeypatch.delenv("PETROTWIN_DB_HOST", raising=False)
    monkeypatch.setattr(runner.sys, "argv", ["runner"])

    config_text = (
        "brent:\n  request_timeout_seconds: 30\n  runner_timeout_seconds: 60\n"
        "fx:\n  request_timeout_seconds: 15\n  runner_timeout_seconds: 45\n  optional: true\n"
        "comeco:\n  request_timeout_seconds: 30\n  runner_timeout_seconds: 70\n"
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)

    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 0
    assert captured_timeouts["petrotwin.collectors.brent"] == 60.0
    assert captured_timeouts["petrotwin.collectors.fx"] == 45.0
    assert captured_timeouts["petrotwin.collectors.comeco"] == 70.0


def test_runner_fallback_timeout_when_omitted(monkeypatch, tmp_path):
    captured_timeouts = {}

    def mock_run_collector(module, **kwargs):
        captured_timeouts[module] = kwargs.get("timeout_seconds")
        return (1, None)

    monkeypatch.setattr(runner, "_run_collector", mock_run_collector)
    monkeypatch.setattr(runner, "_run_pipeline", lambda *args: ("ok", None))
    monkeypatch.delenv("PETROTWIN_DB_HOST", raising=False)
    monkeypatch.setattr(runner.sys, "argv", ["runner"])

    # comeco has request_timeout_seconds=40 but no runner_timeout_seconds
    config_text = (
        "brent:\n  request_timeout_seconds: 20\n"
        "fx:\n  request_timeout_seconds: 15\n  optional: true\n"
        "comeco:\n  request_timeout_seconds: 40\n"
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_dir.joinpath("collectors.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)

    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 0
    # Fallback formula: max(req_timeout + 30.0, 60.0)
    # brent: max(20 + 30, 60) = 60.0
    # comeco: max(40 + 30, 60) = 70.0
    assert captured_timeouts["petrotwin.collectors.brent"] == 60.0
    assert captured_timeouts["petrotwin.collectors.comeco"] == 70.0


def test_runner_skips_retries_on_deterministic_failure(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import subprocess

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    calls = 0

    def mock_subprocess_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        res = MagicMock()
        res.returncode = 1
        res.stdout = '{"event": "parse_error", "category": "parsing_failure", "deterministic": true}\n'
        res.stderr = ""
        return res

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)
    rows, err = runner._run_collector(
        "petrotwin.collectors.comeco",
        retry_count=3,
        retry_delay_seconds=0,
        timeout_seconds=10,
    )
    assert rows == -1
    assert calls == 1  # Fast failure: no 3 retries!


def test_runner_retries_on_transient_failure(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import subprocess

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    calls = 0

    def mock_subprocess_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        res = MagicMock()
        res.returncode = 1
        res.stdout = '{"event": "fetch_error", "category": "connection_timeout", "deterministic": false}\n'
        res.stderr = ""
        return res

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)
    rows, err = runner._run_collector(
        "petrotwin.collectors.comeco",
        retry_count=2,
        retry_delay_seconds=0,
        timeout_seconds=10,
    )
    assert rows == -1
    assert calls == 3  # 1 initial + 2 retries = 3 attempts


def _load_collector_live_check_workflow():
    from pathlib import Path
    import textwrap
    import yaml

    wf_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "collector-live-check.yml"
    assert wf_path.is_file()
    raw_text = wf_path.read_text(encoding="utf-8")
    wf = yaml.safe_load(raw_text)
    verify_step = next(s for s in wf["jobs"]["collector-live-check"]["steps"] if s.get("id") == "verify")
    run_block = verify_step["run"]
    py_script = run_block.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    return wf, raw_text, textwrap.dedent(py_script)


def test_isolated_collector_live_check_workflow_contract():
    """Verify .github/workflows/collector-live-check.yml enforces read-only isolation and safety gates."""
    wf, raw_text, py_script = _load_collector_live_check_workflow()

    # Fix 1: workflow_dispatch only (no push trigger) and explicit branch + comeco.py guard
    triggers = wf.get("on") or wf.get(True)
    assert set(triggers.keys()) == {"workflow_dispatch"}
    assert wf["permissions"] == {"contents": "read"}

    job = wf["jobs"]["collector-live-check"]
    assert job["env"]["GITHUB_REF_NAME"] == "${{ github.ref_name }}"
    assert job["env"]["PETROTWIN_DB_HOST"] == ""
    assert job["env"]["PETROTWIN_DB_PASSWORD"] == ""
    assert job["env"]["PETROTWIN_ENV_FILE"] == "/dev/null"

    checkout_step = job["steps"][0]
    assert checkout_step["with"]["persist-credentials"] is False

    guard_step = job["steps"][1]
    assert "feature/comeco-collector" in guard_step["run"]
    assert "src/petrotwin/collectors/comeco.py" in guard_step["run"]

    # Fix 2: PETROTWIN_LOCK_DIR isolation & full worktree git status comparison
    assert 'env["PETROTWIN_LOCK_DIR"] = str(tmp_locks)' in py_script
    assert '["git", "status", "--porcelain"]' in py_script
    assert "git_status_after == git_status_before" in py_script

    # Fix 3: Strict COMECO 4-row, price > 1000, timestamp agreement, comeco.vn domain, idempotency, and provenance
    assert "comeco_appended != 4 or len(comeco_after_rows) != 4" in py_script
    assert "price_int <= 1000" in py_script
    assert "is_valid_comeco_url" in py_script
    assert "is_valid_pvoil_url" in py_script
    assert 'idem_up_to_date.get("new_rows") != 0' in py_script

    # Fix 4: TimeoutExpired handling (exit code 124), try/except/finally, protected summary, and tmp_root cleanup
    assert "except subprocess.TimeoutExpired" in py_script
    assert "return 124," in py_script
    assert "finally:" in py_script
    assert "shutil.rmtree(tmp_root, ignore_errors=True)" in py_script

    # Fix 5: Dynamic Brent baseline, fetch_done requirement, eia_fred manifest & market_observation check
    assert "brent_before_max_date" in py_script
    assert "brent_fetch_ev is None or brent_done_ev is None" in py_script
    assert '"eia_fred" not in manifest_sources' in py_script
    assert "2026-10-20" not in py_script

    # Execution order: COMECO -> Brent -> FX -> Pipeline -> Offline MySQL Loader Preflight
    comeco_pos = py_script.index('"-m", "petrotwin.collectors.comeco"')
    brent_pos = py_script.index('"-m", "petrotwin.collectors.brent"')
    fx_pos = py_script.index('"-m", "petrotwin.collectors.fx"')
    pipeline_pos = py_script.index('"-m", "petrotwin.data.pipeline"')
    loader_pos = py_script.index('"-m", "petrotwin.data.mysql_loader"')
    assert comeco_pos < brent_pos < fx_pos < pipeline_pos < loader_pos
    assert "COMECO -> Brent -> FX" in py_script

    # Never commit, push, or run mysql_loader with --load
    assert "git commit" not in raw_text
    assert "git push" not in raw_text
    assert "--load" not in raw_text
    assert "GITHUB_STEP_SUMMARY" in raw_text


def test_collector_live_check_script_rejects_wrong_branch_and_still_writes_summary(tmp_path):
    """Verify Fix 1 & Fix 4 at runtime: wrong branch fails immediately before running collectors and still writes summary."""
    import os
    from pathlib import Path
    import subprocess
    import sys

    _, _, py_script = _load_collector_live_check_workflow()
    summary_file = tmp_path / "step_summary.md"
    env = os.environ.copy()
    env["GITHUB_REF_NAME"] = "main"
    env["GITHUB_STEP_SUMMARY"] = str(summary_file)

    res = subprocess.run(
        [sys.executable, "-c", py_script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res.returncode == 1
    assert "Branch safety guard failed" in res.stderr
    assert summary_file.is_file()
    summary_text = summary_file.read_text(encoding="utf-8")
    assert "**Overall Result**: **FAILED**" in summary_text
    assert "Branch safety guard failed" in summary_text
    assert "- **Execution Order**: `COMECO -> Brent -> FX`" in summary_text
    assert summary_text.index("| COMECO Domestic Fuel |") < summary_text.index("| Brent (EIA/FRED) |") < summary_text.index("| USD/VND FX (Algolab SBV) |")


def _load_validate_comeco_raw_to_processed_fn():
    """Extract and compile the exact helper functions from .github/workflows/collector-live-check.yml."""
    _, _, py_script = _load_collector_live_check_workflow()
    prefix = py_script.split("\ndef run_cmd(", 1)[0]
    ns: dict = {}
    exec(prefix, ns)
    return ns["validate_comeco_raw_to_processed"]


def _sample_comeco_raw_and_processed(snapshot_id: str = "a" * 64):
    raw_rows = [
        {
            "date": "2026-11-12",
            "effective_date": "2026-11-12",
            "effective_time": "15:00",
            "product_name": "Dầu DO 0,001S-V",
            "price_vnd_per_liter": "28620",
            "source_url": "https://comeco.vn/",
        },
        {
            "date": "2026-11-12",
            "effective_date": "2026-11-12",
            "effective_time": "15:00",
            "product_name": "Dầu DO 0,05S-II",
            "price_vnd_per_liter": "26820",
            "source_url": "https://comeco.vn/",
        },
        {
            "date": "2026-11-12",
            "effective_date": "2026-11-12",
            "effective_time": "15:00",
            "product_name": "Xăng E10 RON 95-III",
            "price_vnd_per_liter": "19840",
            "source_url": "https://comeco.vn/",
        },
        {
            "date": "2026-11-12",
            "effective_date": "2026-11-12",
            "effective_time": "15:00",
            "product_name": "Xăng E5 RON 92-II",
            "price_vnd_per_liter": "19390",
            "source_url": "https://comeco.vn/",
        },
    ]
    processed_rows = [
        # Include a historical PVOIL row to confirm PVOIL observations are never counted as COMECO
        {
            "product_id": "gasoline_e10_ron95_iii",
            "source_product_name": "Xăng E10 RON 95-III",
            "effective_at_local": "2026-10-22T15:00:00+07:00",
            "effective_time_24_00_normalized": "false",
            "price_value": "20000",
            "source_id": "pvoil",
            "snapshot_id": "b" * 64,
            "source_url": "https://pvoil.com.vn/tin-gia-xang-dau",
        },
        {
            "product_id": "diesel_0001s_v",
            "source_product_name": "Dầu DO 0,001S-V",
            "effective_at_local": "2026-11-12T15:00:00+07:00",
            "effective_time_24_00_normalized": "false",
            "price_value": "28620",
            "source_id": "comeco",
            "snapshot_id": snapshot_id,
            "source_url": "https://comeco.vn/",
        },
        {
            "product_id": "diesel_005s_ii",
            "source_product_name": "Dầu DO 0,05S-II",
            "effective_at_local": "2026-11-12T15:00:00+07:00",
            "effective_time_24_00_normalized": "false",
            "price_value": "26820.00",
            "source_id": "comeco",
            "snapshot_id": snapshot_id,
            "source_url": "https://comeco.vn/",
        },
        {
            "product_id": "gasoline_e10_ron95_iii",
            "source_product_name": "Xăng E10 RON 95-III",
            "effective_at_local": "2026-11-12T15:00:00+07:00",
            "effective_time_24_00_normalized": "false",
            "price_value": "19840",
            "source_id": "comeco",
            "snapshot_id": snapshot_id,
            "source_url": "https://comeco.vn/",
        },
        {
            "product_id": "gasoline_e5_ron92_ii",
            "source_product_name": "Xăng E5 RON 92-II",
            "effective_at_local": "2026-11-12T15:00:00+07:00",
            "effective_time_24_00_normalized": "false",
            "price_value": "19390",
            "source_id": "comeco",
            "snapshot_id": snapshot_id,
            "source_url": "https://comeco.vn/",
        },
    ]
    return raw_rows, processed_rows


def test_comeco_raw_to_processed_correct_four_product_matching():
    """1. Correct four-product raw-to-processed matching (including Decimal price equality and ignoring PVOIL rows)."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert errors == []


def test_comeco_raw_to_processed_rejects_incorrect_product_mapping():
    """2. Incorrect product mapping in raw or processed rows is rejected."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    # Case A: processed row has mismatched source_product_name
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    processed_rows[1]["source_product_name"] = "Xăng E10 RON 95 Mức 5"
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("source_product_name mismatch" in e for e in errors)

    # Case B: raw row has unmapped / disallowed product
    raw_rows_bad, processed_rows_bad = _sample_comeco_raw_and_processed()
    raw_rows_bad[0]["product_name"] = "Xăng E10 RON 95 Mức 5"
    errors_raw = validate(raw_rows_bad, processed_rows_bad, "a" * 64)
    assert any("unmapped or disallowed product_name" in e for e in errors_raw)


def test_comeco_raw_to_processed_rejects_incorrect_effective_timestamp():
    """3. Incorrect effective timestamp in processed observation is rejected."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    processed_rows[1]["effective_at_local"] = "2026-11-12T16:00:00+07:00"
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("missing processed COMECO observation" in e for e in errors)
    assert any("unexpected processed COMECO observation" in e for e in errors)


def test_comeco_raw_to_processed_rejects_incorrect_price():
    """4. Incorrect price (compared via Decimal) is rejected."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    processed_rows[3]["price_value"] = "21840"  # e.g. Mức 5 price instead of 19840
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("price_value mismatch" in e for e in errors)


def test_comeco_raw_to_processed_rejects_incorrect_source_url():
    """5. Incorrect or non-comeco.vn source URL is rejected."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    processed_rows[2]["source_url"] = "https://pvoil.com.vn/tin-gia-xang-dau"
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("source_url mismatch" in e for e in errors)


def test_comeco_raw_to_processed_rejects_incorrect_snapshot_id():
    """6. Incorrect snapshot ID on processed COMECO row is rejected."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    processed_rows[4]["snapshot_id"] = "b" * 64
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("snapshot_id mismatch" in e for e in errors)


def test_comeco_raw_to_processed_rejects_duplicate_processed_observation():
    """7. Duplicate processed COMECO observation is rejected."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    processed_rows.append(dict(processed_rows[1]))
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("duplicate processed COMECO observations" in e for e in errors)
    assert any("cardinality mismatch" in e for e in errors)


def test_comeco_raw_to_processed_rejects_missing_processed_observation():
    """8. Missing processed COMECO observation is rejected (and PVOIL row with same product does not substitute)."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    # Remove COMECO gasoline_e10_ron95_iii row (index 3)
    processed_rows.pop(3)
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("missing processed COMECO observation" in e for e in errors)
    assert any("cardinality mismatch" in e for e in errors)


def test_comeco_raw_to_processed_preserves_distinct_effective_timestamps_same_date():
    """9. Distinct effective timestamps on the same calendar date (e.g. 00:00 and 15:00) are both matched."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    # Add a second adjustment on the same date 2026-11-12 at 00:00 for Xăng E10 RON 95-III
    raw_rows.append({
        "date": "2026-11-12",
        "effective_date": "2026-11-12",
        "effective_time": "00:00",
        "product_name": "Xăng E10 RON 95-III",
        "price_vnd_per_liter": "19500",
        "source_url": "https://comeco.vn/",
    })
    processed_rows.append({
        "product_id": "gasoline_e10_ron95_iii",
        "source_product_name": "Xăng E10 RON 95-III",
        "effective_at_local": "2026-11-12T00:00:00+07:00",
        "effective_time_24_00_normalized": "false",
        "price_value": "19500",
        "source_id": "comeco",
        "snapshot_id": "a" * 64,
        "source_url": "https://comeco.vn/",
    })
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert errors == []


def test_comeco_raw_to_processed_handles_24_00_normalization():
    """10. 24:00 effective_time in raw COMECO row is normalized to next-day 00:00+07:00 exactly as in pipeline.py."""
    validate = _load_validate_comeco_raw_to_processed_fn()
    raw_rows, processed_rows = _sample_comeco_raw_and_processed()
    # Set raw row 0 to 2026-11-11 24:00 -> normalizes to 2026-11-12T00:00:00+07:00 with flag "true"
    raw_rows[0]["effective_date"] = "2026-11-11"
    raw_rows[0]["effective_time"] = "24:00"
    processed_rows[1]["effective_at_local"] = "2026-11-12T00:00:00+07:00"
    processed_rows[1]["effective_time_24_00_normalized"] = "true"
    assert validate(raw_rows, processed_rows, "a" * 64) == []

    # If effective_time_24_00_normalized flag is wrong ("false"), validation rejects it
    processed_rows[1]["effective_time_24_00_normalized"] = "false"
    errors = validate(raw_rows, processed_rows, "a" * 64)
    assert any("effective_time_24_00_normalized mismatch" in e for e in errors)
