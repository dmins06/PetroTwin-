"""Orchestration runner: fetch all data sources then run pipeline and loader.

Calls the three collector modules in sequence (brent → fx → domestic_fuel),
optionally runs ``petrotwin.data.pipeline`` and ``petrotwin.data.mysql_loader``,
and writes a structured JSON log entry to ``logs/collector_run.log``.

Usage::

    # Full run (fetch + pipeline + loader if DB configured)
    python -m petrotwin.collectors.runner

    # Dry-run: fetch only, skip pipeline and loader
    python -m petrotwin.collectors.runner --dry-run

Environment variables:
    PETROTWIN_DB_HOST        — if set, the mysql_loader step is executed.
    PETROTWIN_DATA_DIR       — optional override for the ``data/raw`` directory.
    FRED_API_KEY             — optional FRED API key (used by brent collector).
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


# ---------------------------------------------------------------------------
# Individual collector calls
# ---------------------------------------------------------------------------

def _run_collector(
    module: str,
    retry_count: int = 0,
    retry_delay_seconds: float = 0,
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
            )
        except OSError as exc:
            err = f"{module} could not start: {exc}"
            _print(f"error: {err}")
            if attempt < retry_count:
                time.sleep(retry_delay_seconds)
                continue
            return -1, err

        # Echo output from every attempt so the retry history remains visible.
        if result.stdout.strip():
            for line in result.stdout.splitlines():
                print(line, flush=True)
        if result.stderr.strip():
            for line in result.stderr.splitlines():
                print(line, flush=True)

        if result.returncode == 0:
            break
        err = f"{module} exited with code {result.returncode}"
        _print(f"error: {err}")
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

def _run_pipeline() -> tuple[str, str | None]:
    """Run petrotwin.data.pipeline.  Returns (status, error)."""
    _print("starting pipeline")
    raw_dir = Path(os.environ.get("PETROTWIN_DATA_DIR", str(_repo_root() / "data" / "raw")))
    if not raw_dir.is_absolute():
        raw_dir = (_repo_root() / raw_dir).resolve()
    result = subprocess.run(
        [sys.executable, "-m", "petrotwin.data.pipeline", "--raw-dir", str(raw_dir)],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
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
    """Run petrotwin.data.mysql_loader --load.  Returns (status, error)."""
    _print("starting mysql_loader")
    result = subprocess.run(
        [sys.executable, "-m", "petrotwin.data.mysql_loader", "--load"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
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
        "--dry-run",
        action="store_true",
        help="Run collectors and write raw CSVs, then skip pipeline and loader.",
    )
    args = parser.parse_args()

    run_at = _utcnow_iso()
    _print(f"runner_start dry_run={args.dry_run}")

    # Collector retry settings live in the same checked-in config as the
    # source IDs and timeout values; retries are safe because each collector
    # checks whether its records already exist before appending.
    config_path = _repo_root() / "configs" / "collectors.yaml"
    try:
        with config_path.open(encoding="utf-8") as stream:
            collector_config = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError) as exc:
        _print(f"config_error path={config_path} error={exc}")
        sys.exit(2)

    def run_configured(module: str, section: str) -> tuple[int, str | None]:
        settings = collector_config.get(section, {})
        return _run_collector(
            module,
            retry_count=max(0, int(settings.get("retry_count", 0))),
            retry_delay_seconds=max(0.0, float(settings.get("retry_delay_seconds", 0))),
        )

    # ---- Collectors --------------------------------------------------------
    brent_rows, brent_err = run_configured("petrotwin.collectors.brent", "brent")
    fx_rows, fx_err = run_configured("petrotwin.collectors.fx", "fx")
    fuel_rows, fuel_err = run_configured("petrotwin.collectors.domestic_fuel", "domestic_fuel")

    any_collector_failed = any(
        rows == -1 for rows in [brent_rows, fx_rows, fuel_rows]
    )
    first_error: str | None = brent_err or fx_err or fuel_err

    # ---- Pipeline ----------------------------------------------------------
    pipeline_status = "skipped"
    loader_status: str = "skipped"

    if args.dry_run:
        _print("dry_run=true: skipping pipeline and loader")
        pipeline_status = "skipped"
        loader_status = "skipped"
    elif any_collector_failed:
        _print("one or more collectors failed; skipping pipeline and loader")
        pipeline_status = "skipped"
        loader_status = "skipped"
    else:
        pipeline_status, pipeline_err = _run_pipeline()
        if pipeline_err and first_error is None:
            first_error = pipeline_err

        # ---- Loader --------------------------------------------------------
        db_host = os.environ.get("PETROTWIN_DB_HOST", "").strip()
        if not db_host:
            loader_status = "no_db_config"
            _print("PETROTWIN_DB_HOST not set; skipping loader")
        elif pipeline_status != "ok":
            loader_status = "skipped"
            _print("pipeline did not succeed; skipping loader")
        else:
            loader_status, loader_err = _run_loader()
            if loader_err and first_error is None:
                first_error = loader_err

    # ---- Structured log entry ----------------------------------------------
    log_entry: dict = {
        "run_at_utc": run_at,
        "brent_new_rows": brent_rows if brent_rows >= 0 else 0,
        "fx_new_rows": fx_rows if fx_rows >= 0 else 0,
        "fuel_new_rows": fuel_rows if fuel_rows >= 0 else 0,
        "pipeline_status": pipeline_status,
        "loader_status": loader_status,
        "error": first_error,
    }
    _write_log_entry(log_entry)
    _print(f"log_written path={_log_path()}")

    overall_ok = (
        not any_collector_failed
        and pipeline_status in ("ok", "skipped")
        and loader_status in ("ok", "skipped", "no_db_config")
    )
    _print(f"runner_done overall_ok={overall_ok}")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
