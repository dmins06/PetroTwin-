"""Orchestration runner: fetch all data sources then run pipeline and loader.

Calls collector modules in sequence (brent → fx → domestic_fuel), optionally
runs ``petrotwin.data.pipeline`` and ``petrotwin.data.mysql_loader``, and writes
a structured JSON log entry to ``logs/collector_run.log``.

Execution Modes:
    • Default (full run): Runs collectors → pipeline validation → MySQL loader.
    • Pipeline only (--pipeline-only): Skips collectors; runs pipeline + loader.
      Used by cron at scheduled aggregation windows (e.g. 10:00 ICT, 16:35 ICT)
      to avoid redundant collection after collectors have run on their own schedules.
    • Dry run (--dry-run): Runs collectors only; skips pipeline and loader.

Usage::

    # Full run (fetch + pipeline + loader if DB configured)
    python -m petrotwin.collectors.runner

    # Pipeline only (skip collectors, process current raw CSVs to DB)
    python -m petrotwin.collectors.runner --pipeline-only

    # Dry-run: fetch only, skip pipeline and loader
    python -m petrotwin.collectors.runner --dry-run

Environment variables:
    PETROTWIN_DB_HOST        — if set, the mysql_loader step is executed.
    PETROTWIN_DATA_DIR       — optional override for the ``data/raw`` directory.
    FRED_API_KEY             — optional FRED API key (used by brent collector).
    PETROTWIN_ENV_FILE       — optional path to custom environment file (defaults to /etc/petrotwin/env).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from petrotwin.collectors.locks import file_lock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _log_dir() -> Path:
    return _repo_root() / "logs"


def _log_path() -> Path:
    return _log_dir() / "collector_run.log"


def _print(msg: str) -> None:
    ts = _utcnow_iso()
    print(json.dumps({"ts": ts, "runner": msg}), flush=True)


def _load_env_file(path: Path | None = None) -> None:
    """Load key-value environment variables from file if not already present in os.environ."""
    if path is None:
        override = os.environ.get("PETROTWIN_ENV_FILE")
        if override:
            path = Path(override).expanduser()
            if not path.is_absolute():
                path = (_repo_root() / path).resolve()
        else:
            default_env = Path("/etc/petrotwin/env")
            path = default_env if default_env.is_file() else None

    if path is None or not path.is_file():
        return

    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
    except OSError as exc:
        _print(f"warning: Could not read environment file {path}: {exc}")


# ---------------------------------------------------------------------------
# Individual collector calls
# ---------------------------------------------------------------------------

def _echo_captured_stream(stream: str | bytes | None) -> None:
    """Print lines from a captured process stream, decoding bytes as UTF-8."""
    if not stream:
        return
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", errors="replace")
    if stream.strip():
        for line in stream.splitlines():
            print(line, flush=True)


def _is_deterministic_error(output: str) -> bool:
    """Check if the collector output reports a non-retryable deterministic error."""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            if record.get("deterministic") is True:
                return True
            category = record.get("category")
            if category in {
                "parsing_failure",
                "schema_error",
                "source_declaration_missing",
                "stale_data",
                "future_date",
                "source_unavailable",
            }:
                return True
            event = record.get("event")
            if event in {"parse_error", "schema_error", "source_unavailable"}:
                return True
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return False


def _run_collector(
    module: str,
    retry_count: int = 0,
    retry_delay_seconds: float = 0,
    timeout_seconds: float = 120.0,
) -> tuple[int, str | None]:
    """Run a collector module as a subprocess.

    Returns:
        (new_rows, error_message)  — new_rows is -1 on failure.
    """
    result = None
    for attempt in range(retry_count + 1):
        _print(f"starting {module} attempt={attempt + 1}/{retry_count + 1}")
        try:
            result = subprocess.run(
                [sys.executable, "-m", module],
                cwd=_repo_root(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            _echo_captured_stream(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            _echo_captured_stream(getattr(exc, "stderr", None))
            err = f"{module} timed out after {timeout_seconds} seconds"
            _print(f"error: {err}")
            if attempt < retry_count:
                time.sleep(retry_delay_seconds)
                continue
            return -1, err
        except OSError as exc:
            err = f"{module} could not start: {exc}"
            _print(f"error: {err}")
            if attempt < retry_count:
                time.sleep(retry_delay_seconds)
                continue
            return -1, err

        # Echo output from every attempt so the retry history remains visible.
        _echo_captured_stream(result.stdout)
        _echo_captured_stream(result.stderr)

        if result.returncode == 0:
            break
        err = f"{module} exited with code {result.returncode}"
        _print(f"error: {err}")

        # Skip retrying when the collector reported a deterministic parsing or schema error
        if _is_deterministic_error(result.stdout):
            _print(f"deterministic error detected for {module}; skipping retries")
            return -1, err

        if attempt < retry_count:
            time.sleep(retry_delay_seconds)
    else:
        return -1, err

    if result is None or result.returncode != 0:
        return -1, err

    # Try to parse "new_rows" from the last JSON log line of stdout
    new_rows = 0
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "new_rows" in obj:
                new_rows = int(obj["new_rows"])
                break
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    _print(f"done {module} new_rows={new_rows}")
    return new_rows, None


# ---------------------------------------------------------------------------
# Pipeline and loader
# ---------------------------------------------------------------------------

def _run_pipeline(collectors_config_path: Path | None = None) -> tuple[str, str | None]:
    """Run petrotwin.data.pipeline under a shared pipeline lock."""
    with file_lock("pipeline") as acquired:
        if not acquired:
            err = "Another pipeline execution is already in progress; skipping"
            _print(f"lock_busy: {err}")
            return "failed", err

        _print("starting pipeline")
        raw_dir = Path(os.environ.get("PETROTWIN_DATA_DIR", str(_repo_root() / "data" / "raw")))
        if not raw_dir.is_absolute():
            raw_dir = (_repo_root() / raw_dir).resolve()
        try:
            pipeline_command = [sys.executable, "-m", "petrotwin.data.pipeline", "--raw-dir", str(raw_dir)]
            if collectors_config_path is not None:
                pipeline_command.extend(["--collectors-config", str(collectors_config_path)])
            result = subprocess.run(
                pipeline_command,
                cwd=_repo_root(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            err = "pipeline timed out after 300 seconds"
            _print(f"error: {err}")
            return "failed", err
        except OSError as exc:
            err = f"pipeline could not start: {exc}"
            _print(f"error: {err}")
            return "failed", err

        if result.stdout.strip():
            for line in result.stdout.splitlines():
                print(line, flush=True)
        if result.stderr.strip():
            for line in result.stderr.splitlines():
                print(line, flush=True)

        if result.returncode != 0:
            err = f"pipeline exited with code {result.returncode}"
            _print(f"error: {err}")
            return "failed", err

        _print("pipeline ok")
        return "ok", None


def _run_loader() -> tuple[str, str | None]:
    """Run petrotwin.data.mysql_loader --load. Returns (status, error)."""
    _print("starting mysql_loader")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "petrotwin.data.mysql_loader", "--load"],
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        err = "mysql_loader timed out after 300 seconds"
        _print(f"error: {err}")
        return "failed", err
    except OSError as exc:
        err = f"mysql_loader could not start: {exc}"
        _print(f"error: {err}")
        return "failed", err

    if result.stdout.strip():
        for line in result.stdout.splitlines():
            print(line, flush=True)
    if result.stderr.strip():
        for line in result.stderr.splitlines():
            print(line, flush=True)

    if result.returncode != 0:
        err = f"mysql_loader exited with code {result.returncode}"
        _print(f"error: {err}")
        return "failed", err

    _print("mysql_loader ok")
    return "ok", None


# ---------------------------------------------------------------------------
# Log writing
# ---------------------------------------------------------------------------

def _write_log_entry(entry: dict) -> None:
    """Append a JSON log entry (one line) to the collector run log."""
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PetroTwin data collector orchestration runner"
    )
    parser.add_argument(
        "--pipeline-only",
        action="store_true",
        help="Run pipeline validation and MySQL loader only; skip all collectors.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run collectors and write raw CSVs, then skip pipeline and loader.",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Collector YAML config (default: <repo>/configs/collectors.yaml).",
    )
    args = parser.parse_args()

    if args.pipeline_only and args.dry_run:
        parser.error("Cannot combine --pipeline-only and --dry-run")

    # Load environment variables (such as MySQL credentials) from file if needed
    _load_env_file()

    # Enforce shared runner lock to prevent overlapping runs
    with file_lock("runner") as acquired:
        if not acquired:
            _print("lock_busy: Another runner process is already active; exiting cleanly")
            sys.exit(0)

        repo = _repo_root()
        config_path = args.config or repo / "configs" / "collectors.yaml"
        if not config_path.is_absolute():
            config_path = (repo / config_path).resolve()

        mode = "pipeline_only" if args.pipeline_only else ("dry_run" if args.dry_run else "full")
        run_at = _utcnow_iso()
        _print(f"runner_start mode={mode}")

        try:
            with config_path.open(encoding="utf-8") as stream:
                collector_config = yaml.safe_load(stream) or {}
        except (OSError, yaml.YAMLError) as exc:
            _print(f"config_error path={config_path} error={exc}")
            sys.exit(2)

        def run_configured(module: str, section: str) -> tuple[int, str | None]:
            settings = collector_config.get(section, {})
            req_timeout = max(1.0, float(settings.get("request_timeout_seconds", 30)))
            configured_runner_timeout = settings.get("runner_timeout_seconds")
            if configured_runner_timeout is not None:
                runner_timeout = max(1.0, float(configured_runner_timeout))
            else:
                runner_timeout = max(req_timeout + 30.0, 60.0)

            return _run_collector(
                module,
                retry_count=max(0, int(settings.get("retry_count", 0))),
                retry_delay_seconds=max(0.0, float(settings.get("retry_delay_seconds", 0))),
                timeout_seconds=runner_timeout,
            )

        def collector_is_optional(section: str) -> bool:
            settings = collector_config.get(section, {})
            return settings.get("optional", False) is True

        # ---- Collectors Stage ----------------------------------------------
        if args.pipeline_only:
            _print("pipeline_only=true: skipping collector invocations")
            brent_rows = 0
            fx_rows = 0
            fuel_rows = 0
            brent_err = None
            fx_err = None
            fuel_err = None
            required_collector_failed = False
            optional_collector_failed: dict[str, str | None] = {}
            first_error: str | None = None
        else:
            brent_rows, brent_err = run_configured("petrotwin.collectors.brent", "brent")
            fx_rows, fx_err = run_configured("petrotwin.collectors.fx", "fx")
            fuel_rows, fuel_err = run_configured("petrotwin.collectors.domestic_fuel", "domestic_fuel")

            collector_results = {
                "brent": (brent_rows, brent_err, collector_is_optional("brent")),
                "fx": (fx_rows, fx_err, collector_is_optional("fx")),
                "domestic_fuel": (fuel_rows, fuel_err, collector_is_optional("domestic_fuel")),
            }
            required_collector_failed = any(
                rows == -1 and not optional for rows, _error, optional in collector_results.values()
            )
            optional_collector_failed = {
                name: error for name, (rows, error, optional) in collector_results.items()
                if rows == -1 and optional
            }
            first_error = brent_err or fx_err or fuel_err

        # ---- Pipeline Stage ------------------------------------------------
        pipeline_status = "skipped"
        loader_status: str = "skipped"

        if args.dry_run:
            _print("dry_run=true: skipping pipeline and loader")
            pipeline_status = "skipped"
            loader_status = "skipped"
        elif required_collector_failed:
            _print("one or more required collectors failed; skipping pipeline and loader")
            pipeline_status = "skipped"
            loader_status = "skipped"
        else:
            pipeline_status, pipeline_err = _run_pipeline(config_path)
            if pipeline_err:
                first_error = pipeline_err

            # ---- Loader Stage ----------------------------------------------
            db_host = os.environ.get("PETROTWIN_DB_HOST", "").strip()
            if not db_host:
                loader_status = "no_db_config"
                _print("PETROTWIN_DB_HOST not set; skipping loader")
            elif pipeline_status != "ok":
                loader_status = "skipped"
                _print("pipeline did not succeed; skipping loader")
            else:
                loader_status, loader_err = _run_loader()
                if loader_err:
                    first_error = loader_err

        # ---- Structured log entry ------------------------------------------
        log_entry: dict = {
            "run_at_utc": run_at,
            "mode": mode,
            "brent_new_rows": brent_rows if brent_rows >= 0 else 0,
            "fx_new_rows": fx_rows if fx_rows >= 0 else 0,
            "fuel_new_rows": fuel_rows if fuel_rows >= 0 else 0,
            "pipeline_status": pipeline_status,
            "loader_status": loader_status,
            "optional_collectors_failed": sorted(optional_collector_failed),
            "error": first_error,
        }

        if args.dry_run:
            stages_accepted = pipeline_status == "skipped" and loader_status == "skipped"
        else:
            stages_accepted = pipeline_status == "ok" and loader_status in {"ok", "no_db_config"}

        if not stages_accepted and first_error is None:
            first_error = f"unexpected stage status: pipeline={pipeline_status}, loader={loader_status}"

        if required_collector_failed or not stages_accepted:
            overall_status = "failed"
        elif optional_collector_failed:
            overall_status = "partial"
        else:
            overall_status = "success"

        overall_ok = overall_status in {"success", "partial"}
        log_entry["overall_status"] = overall_status
        log_entry["error"] = first_error
        _write_log_entry(log_entry)
        _print(
            f"runner_done mode={mode} overall_status={overall_status} "
            f"overall_ok={overall_ok} optional_failures={sorted(optional_collector_failed)}"
        )
        sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
