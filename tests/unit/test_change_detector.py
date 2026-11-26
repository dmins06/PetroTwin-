"""Unit tests for event-driven data-change detection and GitHub Actions workflow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import yaml

from petrotwin.collectors.change_detector import (
    EVENT_TYPE_NEW_DATA,
    SourceCheckResult,
    compute_content_fingerprint,
    detect_source_changes,
    load_fingerprint_state,
    record_published_fingerprints,
    trigger_repository_dispatch,
)


def _seed_state(tmp_path: Path) -> Path:
    state_file = tmp_path / "fingerprints.json"
    record_published_fingerprints(
        {
            "brent": compute_content_fingerprint({"brent": "2026-11-17:92.94"}),
            "fx": compute_content_fingerprint({"fx": "2026-11-24:25668"}),
            "domestic_fuel": compute_content_fingerprint({"fuel": "2026-11-19:19840"}),
        },
        state_path=state_file,
    )
    return state_file


def _unchanged_checkers():
    return {
        "brent": lambda: SourceCheckResult(
            "brent", True, compute_content_fingerprint({"brent": "2026-11-17:92.94"}), "2026-11-17"
        ),
        "fx": lambda: SourceCheckResult(
            "fx", True, compute_content_fingerprint({"fx": "2026-11-24:25668"}), "2026-11-24"
        ),
        "domestic_fuel": lambda: SourceCheckResult(
            "domestic_fuel",
            True,
            compute_content_fingerprint({"fuel": "2026-11-19:19840"}),
            "2026-11-19",
        ),
    }


def test_identical_source_content_does_not_trigger(tmp_path: Path):
    state_file = _seed_state(tmp_path)
    report = detect_source_changes(checkers=_unchanged_checkers(), state_path=state_file)
    assert report.should_trigger is False
    assert report.changed_sources == ()
    assert set(report.unchanged_sources) == {"brent", "fx", "domestic_fuel"}
    assert report.failed_sources == {}


@pytest.mark.parametrize(
    ("changed_key", "new_payload", "new_date"),
    [
        ("brent", {"brent": "2026-11-18:93.10"}, "2026-11-18"),
        ("fx", {"fx": "2026-11-25:25679"}, "2026-11-25"),
        ("domestic_fuel", {"fuel": "2026-11-26:20100"}, "2026-11-26"),
    ],
)
def test_single_source_change_triggers_detection(
    tmp_path: Path, changed_key: str, new_payload: dict, new_date: str
):
    state_file = _seed_state(tmp_path)
    checkers = _unchanged_checkers()
    checkers[changed_key] = lambda: SourceCheckResult(
        changed_key, True, compute_content_fingerprint(new_payload), new_date
    )

    report = detect_source_changes(checkers=checkers, state_path=state_file)
    assert report.should_trigger is True
    assert report.changed_sources == (changed_key,)
    assert report.change_bundle_id is not None


def test_multiple_changed_sources_produce_one_logical_trigger(tmp_path: Path):
    state_file = _seed_state(tmp_path)
    checkers = {
        "brent": lambda: SourceCheckResult(
            "brent", True, compute_content_fingerprint({"brent": "new"}), "2026-11-25"
        ),
        "fx": lambda: SourceCheckResult(
            "fx", True, compute_content_fingerprint({"fx": "new"}), "2026-11-25"
        ),
        "domestic_fuel": lambda: SourceCheckResult(
            "domestic_fuel", True, compute_content_fingerprint({"fuel": "new"}), "2026-11-26"
        ),
    }

    report = detect_source_changes(checkers=checkers, state_path=state_file)
    assert report.should_trigger is True
    assert report.changed_sources == ("brent", "domestic_fuel", "fx")
    assert report.change_bundle_id is not None


def test_unavailable_sources_do_not_produce_false_positives(tmp_path: Path):
    state_file = _seed_state(tmp_path)
    before = load_fingerprint_state(state_file)
    checkers = _unchanged_checkers()
    checkers["brent"] = lambda: SourceCheckResult("brent", False, error="HTTP 503")
    checkers["fx"] = lambda: SourceCheckResult("fx", False, error="timeout")

    report = detect_source_changes(checkers=checkers, state_path=state_file)
    assert report.should_trigger is False
    assert set(report.failed_sources.keys()) == {"brent", "fx"}
    # Failed checks must not overwrite stored fingerprints
    assert load_fingerprint_state(state_file) == before


def test_failed_publication_does_not_advance_fingerprint_and_duplicate_is_suppressed(tmp_path: Path):
    state_file = _seed_state(tmp_path)
    checkers = _unchanged_checkers()
    checkers["fx"] = lambda: SourceCheckResult(
        "fx", True, compute_content_fingerprint({"fx": "2026-11-25:25679"}), "2026-11-25"
    )

    # First detection: triggers, but state is NOT advanced until publication succeeds
    report1 = detect_source_changes(checkers=checkers, state_path=state_file)
    assert report1.should_trigger is True

    # Simulate failed publication (record_published_fingerprints NOT called) -> next check retries
    report_retry = detect_source_changes(checkers=checkers, state_path=state_file)
    assert report_retry.should_trigger is True
    assert report_retry.change_bundle_id == report1.change_bundle_id

    # Now publication succeeds -> advance fingerprints
    record_published_fingerprints(
        report1.current_fingerprints,
        change_bundle_id=report1.change_bundle_id,
        state_path=state_file,
    )
    report_after = detect_source_changes(checkers=checkers, state_path=state_file)
    assert report_after.should_trigger is False


def test_repository_dispatch_sends_event_without_exposing_token(tmp_path: Path, monkeypatch):
    state_file = _seed_state(tmp_path)
    checkers = _unchanged_checkers()
    checkers["fx"] = lambda: SourceCheckResult(
        "fx", True, compute_content_fingerprint({"fx": "2026-11-25:25679"}), "2026-11-25"
    )
    report = detect_source_changes(checkers=checkers, state_path=state_file)

    captured: dict = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 204
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    code = trigger_repository_dispatch("dmins06/PetroTwin-", "secret-token-xyz", report)

    assert code == 204
    assert captured["url"] == "https://api.github.com/repos/dmins06/PetroTwin-/dispatches"
    assert captured["json"]["event_type"] == EVENT_TYPE_NEW_DATA
    assert "secret-token-xyz" not in str(captured["json"])


def test_petrotwin_update_workflow_supports_event_driven_and_manual_triggers():
    wf_path = Path(".github/workflows/petrotwin-update.yml")
    wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    # PyYAML parses bare `on:` key as boolean True in YAML 1.1
    triggers = wf.get("on") or wf.get(True)

    assert "workflow_dispatch" in triggers
    assert "repository_dispatch" in triggers
    assert EVENT_TYPE_NEW_DATA in triggers["repository_dispatch"]["types"]
    assert "schedule" not in triggers

    assert wf["concurrency"]["group"] == "petrotwin-data-update"
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf["permissions"] == {"contents": "write"}
    assert "dispatch_id" in wf.get("run-name", "")
    assert "push" not in triggers
    raw_text = wf_path.read_text(encoding="utf-8")
    assert "--confirm-dispatch-id" in raw_text
    assert "--state-branch state/detector" in raw_text


def test_petrotwin_detect_workflow_is_free_lightweight_and_loop_safe():
    wf_path = Path(".github/workflows/petrotwin-detect.yml")
    raw_text = wf_path.read_text(encoding="utf-8")
    wf = yaml.safe_load(raw_text)
    triggers = wf.get("on") or wf.get(True)

    assert "workflow_dispatch" in triggers
    assert "schedule" in triggers
    assert any(item.get("cron") == "*/30 * * * *" for item in triggers["schedule"])
    assert "push" not in triggers
    assert "repository_dispatch" not in triggers

    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["group"] == "petrotwin-data-detect"
    assert wf["concurrency"]["cancel-in-progress"] is False
    # Zero TiDB credentials in lightweight detector workflow
    assert "PETROTWIN_DB_" not in raw_text
    assert "secrets.PETROTWIN_DISPATCH_TOKEN" in raw_text
    assert "GITHUB_DISPATCH_TOKEN" not in raw_text
    assert "--state-backend github" in raw_text
    assert "--state-branch state/detector" in raw_text
    assert "--dispatch" in raw_text

    checkout_step = next(
        s for s in wf["jobs"]["detect-changes"]["steps"] if s["name"] == "Checkout repository"
    )
    sparse = str(checkout_step.get("with", {}).get("sparse-checkout", ""))
    assert "src/petrotwin" in sparse
    assert "configs/collectors.yaml" in sparse



def test_brent_historical_correction_changes_fingerprint(monkeypatch):
    from petrotwin.collectors.change_detector import check_brent_source

    csv_v1 = "DATE,DCOILBRENTEU\n2018-08-22,72.96\n2020-05-01,25.00\n2026-11-17,92.94\n"
    csv_v2 = "DATE,DCOILBRENTEU\n2018-08-22,72.96\n2020-05-01,25.50\n2026-11-17,92.94\n"

    def fake_get_factory(body: str):
        def _get(url, timeout):
            resp = MagicMock()
            resp.text = body
            resp.raise_for_status = MagicMock()
            return resp

        return _get

    monkeypatch.setattr(requests, "get", fake_get_factory(csv_v1))
    r1 = check_brent_source()
    monkeypatch.setattr(requests, "get", fake_get_factory(csv_v2))
    r2 = check_brent_source()

    assert r1.ok and r2.ok
    assert r1.fingerprint != r2.fingerprint


def test_stale_fx_and_comeco_are_rejected(monkeypatch):
    from datetime import date

    from petrotwin.collectors import comeco as comeco_collector
    from petrotwin.collectors import fx as fx_collector
    from petrotwin.collectors.change_detector import check_domestic_fuel_source, check_fx_source

    monkeypatch.setattr(fx_collector, "_fetch_rate", lambda: (25600, date(2026, 11, 1)))
    fx_res = check_fx_source(current_date=date(2026, 11, 25), max_age_days=7)
    assert fx_res.ok is False
    assert "stale_data" in (fx_res.error or "")

    monkeypatch.setattr(comeco_collector, "_fetch_html", lambda: "<html></html>")
    monkeypatch.setattr(
        comeco_collector,
        "_parse_comeco_html",
        lambda html, current_date=None: (date(2026, 11, 1), "15:00", [{"product_name": "X", "price": 20000}]),
    )
    fuel_res = check_domestic_fuel_source(current_date=date(2026, 11, 25), max_age_days=7)
    assert fuel_res.ok is False
    assert "stale_data" in (fuel_res.error or "")


def test_dispatch_failure_and_workflow_failure_do_not_advance_state(tmp_path: Path, monkeypatch):
    from petrotwin.collectors.change_detector import (
        reconcile_pending_dispatch,
        run_detector_cycle,
    )

    state_file = _seed_state(tmp_path)
    before = load_fingerprint_state(state_file)
    checkers = _unchanged_checkers()
    checkers["fx"] = lambda: SourceCheckResult(
        "fx", True, compute_content_fingerprint({"fx": "2026-11-25:25679"}), "2026-11-25"
    )

    # 1. Dispatch HTTP failure -> state unchanged (neither pending nor published advanced)
    def fail_post(url, headers, json, timeout):
        raise requests.HTTPError("502 Bad Gateway")

    monkeypatch.setattr(requests, "post", fail_post)
    with pytest.raises(requests.HTTPError):
        run_detector_cycle(
            repo="dmins06/PetroTwin-",
            token="tok",
            dispatch=True,
            state_path=state_file,
            checkers=checkers,
            lock_name=f"test_det_{tmp_path.name}",
        )
    assert load_fingerprint_state(state_file) == before

    # 2. Dispatch succeeds -> records pending_dispatch ONLY (last_published unchanged)
    def ok_post(url, headers, json, timeout):
        resp = MagicMock()
        resp.status_code = 204
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "post", ok_post)
    out = run_detector_cycle(
        repo="dmins06/PetroTwin-",
        token="tok",
        dispatch=True,
        state_path=state_file,
        checkers=checkers,
        lock_name=f"test_det_{tmp_path.name}",
    )
    assert out["dispatched"] is True
    mid_state = load_fingerprint_state(state_file)
    assert mid_state["last_published_fingerprints"] == before["last_published_fingerprints"]
    assert mid_state["pending_dispatch"] is not None
    dispatch_id_1 = mid_state["pending_dispatch"]["dispatch_id"]
    assert dispatch_id_1

    # 2b. Unrelated succeeded run with a DIFFERENT dispatch_id MUST NOT promote our pending state
    def unrelated_success_get(url, headers, timeout):
        resp = MagicMock()
        resp.json.return_value = {
            "workflow_runs": [
                {
                    "display_title": "PetroTwin Update [disp-other-unrelated]",
                    "created_at": "2099-01-01T00:00:00Z",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "get", unrelated_success_get)
    assert reconcile_pending_dispatch("dmins06/PetroTwin-", "tok", state_path=state_file) == "in_progress"
    assert load_fingerprint_state(state_file)["last_published_fingerprints"] == before["last_published_fingerprints"]

    # 3. Workflow matching our exact dispatch_id fails -> pending cleared, last_published NOT advanced
    def failed_runs_get(url, headers, timeout):
        resp = MagicMock()
        resp.json.return_value = {
            "workflow_runs": [
                {
                    "display_title": f"PetroTwin Update [{dispatch_id_1}]",
                    "created_at": "2099-01-01T00:00:00Z",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ]
        }
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "get", failed_runs_get)
    assert reconcile_pending_dispatch("dmins06/PetroTwin-", "tok", state_path=state_file) == "failed_cleared"
    after_fail = load_fingerprint_state(state_file)
    assert after_fail["pending_dispatch"] is None
    assert after_fail["last_published_fingerprints"] == before["last_published_fingerprints"]

    # 4. Retry dispatch -> workflow matching exact dispatch_id succeeds -> state promoted!
    run_detector_cycle(
        repo="dmins06/PetroTwin-",
        token="tok",
        dispatch=True,
        state_path=state_file,
        checkers=checkers,
        lock_name=f"test_det_{tmp_path.name}",
    )
    dispatch_id_2 = load_fingerprint_state(state_file)["pending_dispatch"]["dispatch_id"]

    def ok_runs_get(url, headers, timeout):
        resp = MagicMock()
        resp.json.return_value = {
            "workflow_runs": [
                {
                    "display_title": f"PetroTwin Update [{dispatch_id_2}]",
                    "created_at": "2099-01-01T00:00:00Z",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "get", ok_runs_get)
    assert reconcile_pending_dispatch("dmins06/PetroTwin-", "tok", state_path=state_file) == "promoted"
    promoted = load_fingerprint_state(state_file)
    assert promoted["pending_dispatch"] is None
    assert promoted["last_published_fingerprints"]["fx"] == compute_content_fingerprint({"fx": "2026-11-25:25679"})


def test_concurrent_detector_lock_prevents_overlap(tmp_path: Path, monkeypatch):
    from contextlib import contextmanager

    from petrotwin.collectors import change_detector

    @contextmanager
    def busy_lock(_name):
        yield False

    monkeypatch.setattr(change_detector, "file_lock", busy_lock)
    res = change_detector.run_detector_cycle(
        repo="dmins06/PetroTwin-",
        dispatch=False,
        state_path=tmp_path / "state.json",
        checkers=_unchanged_checkers(),
    )
    assert res["status"] == "lock_busy"
    assert res["should_trigger"] is False


class _InMemoryGitHubStateStore:
    """In-memory fake matching GitHubBranchStateStore interface for unit tests."""

    def __init__(self, initial_state: dict | None = None, conflict_on_save: bool = False) -> None:
        self.file_path = "source_fingerprints.json"
        self.branch = "state/detector"
        self.state = initial_state
        self.sha = "sha-0" if initial_state is not None else None
        self.conflict_on_save = conflict_on_save
        self.saves = 0

    def load(self):
        from petrotwin.collectors.change_detector import _default_state

        if self.state is None:
            return _default_state(), None
        import copy

        return copy.deepcopy(self.state), self.sha

    def save(self, state: dict, expected_sha: str | None, message: str) -> str:
        from petrotwin.collectors.change_detector import StateConflictError

        if self.conflict_on_save or expected_sha != self.sha:
            raise StateConflictError("HTTP 409 conflict")
        import copy

        self.state = copy.deepcopy(state)
        self.saves += 1
        self.sha = f"sha-{self.saves}"
        return self.sha


def test_github_state_store_roundtrip_and_409_conflict(monkeypatch):
    import base64
    import json

    from petrotwin.collectors.change_detector import GitHubBranchStateStore, StateConflictError

    store = GitHubBranchStateStore("dmins06/PetroTwin-", "tok", branch="state/detector")

    # 1. Load returns default state on 404
    def fake_get_404(url, headers, params=None, timeout=15):
        resp = MagicMock()
        resp.status_code = 404
        return resp

    monkeypatch.setattr(requests, "get", fake_get_404)
    state, sha = store.load()
    assert state["last_published_fingerprints"] == {}
    assert sha is None

    # 2. Load decodes base64 JSON content and returns blob sha
    encoded = base64.b64encode(
        json.dumps({"last_published_fingerprints": {"fx": "abc"}}).encode("utf-8")
    ).decode("ascii")

    def fake_get_200(url, headers, params=None, timeout=15):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"content": encoded, "sha": "blob-123"}
        return resp

    monkeypatch.setattr(requests, "get", fake_get_200)
    state2, sha2 = store.load()
    assert state2["last_published_fingerprints"] == {"fx": "abc"}
    assert sha2 == "blob-123"

    # 3. Save raises StateConflictError on HTTP 409
    def fake_put_409(url, headers, json, timeout=15):
        resp = MagicMock()
        resp.status_code = 409
        return resp

    monkeypatch.setattr(requests, "put", fake_put_409)
    with pytest.raises(StateConflictError):
        store.save(state2, "stale-sha", "update state")


def test_confirm_dispatch_id_exact_match_and_runner_restart_durability(monkeypatch):
    from petrotwin.collectors.change_detector import (
        confirm_dispatch_id,
        run_detector_cycle,
    )

    store = _InMemoryGitHubStateStore(
        initial_state={
            "schema_version": "petrotwin-detector-state-v1",
            "last_published_fingerprints": {
                "brent": compute_content_fingerprint({"brent": "2026-11-17:92.94"}),
                "fx": compute_content_fingerprint({"fx": "2026-11-24:25668"}),
                "domestic_fuel": compute_content_fingerprint({"fuel": "2026-11-19:19840"}),
            },
            "last_published_bundle_id": None,
            "last_published_at_utc": None,
            "pending_dispatch": None,
        }
    )
    checkers = _unchanged_checkers()
    checkers["fx"] = lambda: SourceCheckResult(
        "fx", True, compute_content_fingerprint({"fx": "2026-11-25:25679"}), "2026-11-25"
    )

    dispatch_calls = 0

    def ok_post(url, headers, json, timeout):
        nonlocal dispatch_calls
        dispatch_calls += 1
        resp = MagicMock()
        resp.status_code = 204
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "post", ok_post)

    # Runner 1 detects change and dispatches once; state persisted on GitHub store
    res1 = run_detector_cycle(
        repo="dmins06/PetroTwin-",
        token="tok",
        dispatch=True,
        checkers=checkers,
        lock_name="test_gh_runner_1",
        github_store=store,
    )
    assert res1["dispatched"] is True
    assert dispatch_calls == 1
    dispatch_id = store.state["pending_dispatch"]["dispatch_id"]
    assert dispatch_id

    # Runner 2 (fresh ephemeral runner) sees pending_dispatch in GitHub store -> no duplicate dispatch
    def empty_runs_get(url, headers, timeout):
        resp = MagicMock()
        resp.json.return_value = {"workflow_runs": []}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "get", empty_runs_get)
    res2 = run_detector_cycle(
        repo="dmins06/PetroTwin-",
        token="tok",
        dispatch=True,
        checkers=checkers,
        lock_name="test_gh_runner_2",
        github_store=store,
    )
    assert res2["status"] == "workflow_in_progress"
    assert dispatch_calls == 1

    # Mismatched dispatch_id must NOT promote state
    assert confirm_dispatch_id("disp-wrong-id", github_store=store) is False
    assert store.state["pending_dispatch"]["dispatch_id"] == dispatch_id

    # Exact dispatch_id promotes state cleanly
    assert confirm_dispatch_id(dispatch_id, github_store=store) is True
    assert store.state["pending_dispatch"] is None
    assert store.state["last_published_fingerprints"]["fx"] == compute_content_fingerprint(
        {"fx": "2026-11-25:25679"}
    )


def test_run_detector_cycle_handles_github_state_conflict_safely(monkeypatch):
    from petrotwin.collectors.change_detector import run_detector_cycle

    store = _InMemoryGitHubStateStore(conflict_on_save=True)
    checkers = _unchanged_checkers()

    def ok_post(url, headers, json, timeout):
        resp = MagicMock()
        resp.status_code = 204
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(requests, "post", ok_post)
    res = run_detector_cycle(
        repo="dmins06/PetroTwin-",
        token="tok",
        dispatch=True,
        checkers=checkers,
        lock_name="test_gh_conflict",
        github_store=store,
    )
    assert res["status"] == "state_conflict"
    assert res["should_trigger"] is False
    assert res["dispatched"] is False


def test_petrotwin_update_workflow_hardening_target_branch_and_step_ordering():
    wf_path = Path(".github/workflows/petrotwin-update.yml")
    raw_text = wf_path.read_text(encoding="utf-8")
    wf = yaml.safe_load(raw_text)

    job = wf["jobs"]["update-data"]
    assert job["env"]["TARGET_BRANCH"] == "main"

    steps = job["steps"]
    step_by_name = {s["name"]: s for s in steps}
    step_names = [s["name"] for s in steps]

    # 1 & 2. Both event types checkout and push strictly to TARGET_BRANCH (main), never GITHUB_REF_NAME
    checkout = step_by_name["Checkout repository"]
    assert checkout["with"]["ref"] == "${{ env.TARGET_BRANCH }}"
    assert "GITHUB_REF_NAME" not in raw_text
    assert "github.ref_name" not in raw_text

    # 3 & 8. Explicit CSV pathspec, empty-commit guard, and no force-push
    commit_step = step_by_name["Commit updated tracked raw data"]
    commit_run = commit_step["run"]
    assert "set -euo pipefail" in commit_run
    assert "git add -u -- 'data/raw/*.csv'" in commit_run
    assert "git diff --cached --quiet" in commit_run
    assert 'git push origin "HEAD:${TARGET_BRANCH}"' in commit_run
    assert "--force" not in commit_run
    assert "--force-with-lease" not in commit_run
    assert " -f" not in commit_run

    # 7. Push failure cannot execute --confirm-dispatch-id (ordered after commit, no always()/failure())
    confirm_step = step_by_name["Confirm published dispatch state"]
    assert step_names.index("Commit updated tracked raw data") < step_names.index(
        "Confirm published dispatch state"
    )
    confirm_if = str(confirm_step.get("if", ""))
    assert "always(" not in confirm_if
    assert "failure(" not in confirm_if
    assert "--confirm-dispatch-id" in confirm_step["run"]
    assert "secrets.PETROTWIN_DISPATCH_TOKEN" in raw_text
    assert "GITHUB_DISPATCH_TOKEN" not in raw_text


def test_missing_dispatch_token_raises_clear_error(tmp_path: Path):
    state_file = _seed_state(tmp_path)
    checkers = _unchanged_checkers()
    checkers["fx"] = lambda: SourceCheckResult(
        "fx", True, compute_content_fingerprint({"fx": "2026-11-25:25679"}), "2026-11-25"
    )
    report = detect_source_changes(checkers=checkers, state_path=state_file)
    with pytest.raises(ValueError, match="PETROTWIN_DISPATCH_TOKEN"):
        trigger_repository_dispatch("dmins06/PetroTwin-", "", report)


def test_fx_config_loads_independently_of_working_directory(tmp_path: Path, monkeypatch):
    from petrotwin.collectors import fx as fx_collector

    monkeypatch.chdir(tmp_path)
    settings = fx_collector._fx_config()
    assert isinstance(settings, dict)
    assert settings["source_id"] == "algolab_sbv_central_rate"
    assert settings["source_status"] == "enabled"


def test_fx_config_missing_file_raises_clear_error(tmp_path: Path, monkeypatch):
    from petrotwin.collectors import fx as fx_collector

    monkeypatch.setattr(fx_collector, "_repo_root", lambda: tmp_path)
    with pytest.raises(
        fx_collector.FXSourceUnavailableError,
        match="Could not load FX source configuration",
    ):
        fx_collector._fx_config()


def test_git_pathspec_stages_only_tracked_csv_files_under_data_raw(tmp_path: Path):
    import subprocess

    repo = tmp_path / "repo"
    (repo / "data" / "raw").mkdir(parents=True)
    (repo / "other").mkdir(parents=True)

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    _git("init")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")

    # Seed tracked files: 1 CSV under data/raw, 3 non-CSV under data/raw, 1 CSV outside data/raw
    tracked_csv = repo / "data" / "raw" / "brent_crude_prices.csv"
    tracked_json = repo / "data" / "raw" / "meta.json"
    tracked_yaml = repo / "data" / "raw" / "config.yaml"
    tracked_log = repo / "data" / "raw" / "run.log"
    outside_csv = repo / "other" / "outside.csv"

    tracked_csv.write_text("date,price\n2026-11-24,90.0\n", encoding="utf-8")
    tracked_json.write_text('{"v": 1}\n', encoding="utf-8")
    tracked_yaml.write_text("v: 1\n", encoding="utf-8")
    tracked_log.write_text("ok\n", encoding="utf-8")
    outside_csv.write_text("a,b\n1,2\n", encoding="utf-8")
    _git("add", ".")
    _git("commit", "-m", "initial")

    # Empty-commit guard when no tracked CSV under data/raw changed
    tracked_json.write_text('{"v": 2}\n', encoding="utf-8")
    tracked_yaml.write_text("v: 2\n", encoding="utf-8")
    tracked_log.write_text("ok2\n", encoding="utf-8")
    outside_csv.write_text("a,b\n1,3\n", encoding="utf-8")
    (repo / "data" / "raw" / "untracked.csv").write_text("x,y\n1,2\n", encoding="utf-8")

    _git("add", "-u", "--", "data/raw/*.csv")
    cached_empty = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=repo, check=False
    )
    assert cached_empty.returncode == 0

    # Now modify tracked CSV under data/raw -> ONLY this file is staged
    tracked_csv.write_text("date,price\n2026-11-24,90.0\n2026-11-25,91.0\n", encoding="utf-8")
    _git("add", "-u", "--", "data/raw/*.csv")
    staged = _git("diff", "--cached", "--name-only").stdout.strip().splitlines()
    assert staged == ["data/raw/brent_crude_prices.csv"]
