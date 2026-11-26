"""Lightweight event-driven data-change detector and GitHub repository_dispatch trigger.

Monitors each PetroTwin upstream source independently using deterministic SHA-256
content fingerprints (never timestamps alone), produces at most one logical trigger
when genuinely new data appears, protects state with file_lock, and only advances
last-successful fingerprints after GitHub Actions workflow completion is confirmed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import requests

from petrotwin.collectors import brent as brent_collector
from petrotwin.collectors import comeco as comeco_collector
from petrotwin.collectors import fx as fx_collector
from petrotwin.collectors.locks import file_lock

logger = logging.getLogger(__name__)

EVENT_TYPE_NEW_DATA = "petrotwin_new_data"
DEFAULT_STATE_PATH = Path("data/interim/source_fingerprints.json")
DEFAULT_STATE_BRANCH = "state/detector"
DEFAULT_STATE_REPO_FILE = "source_fingerprints.json"
BRENT_START_DATE = "2018-08-22"
DEFAULT_MAX_AGE_DAYS = 7
VALID_SOURCE_IDS = frozenset({"brent", "fx", "domestic_fuel"})


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def compute_content_fingerprint(payload: Any) -> str:
    """Compute a deterministic SHA-256 content fingerprint for canonical source data."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceCheckResult:
    """Result of probing one upstream data source."""

    source_id: str
    ok: bool
    fingerprint: str | None = None
    latest_date: str | None = None
    summary: str = ""
    error: str | None = None


@dataclass(frozen=True)
class DetectionReport:
    """Aggregated change-detection outcome across all monitored sources."""

    detected_at_utc: str
    changed_sources: tuple[str, ...]
    unchanged_sources: tuple[str, ...]
    failed_sources: dict[str, str]
    current_fingerprints: dict[str, str]
    latest_dates: dict[str, str]
    change_bundle_id: str | None
    should_trigger: bool
    is_duplicate_bundle: bool
    dispatch_id: str | None = None

    def to_dispatch_payload(self) -> dict[str, Any]:
        """Build a secret-free client_payload for GitHub repository_dispatch."""
        return {
            "dispatch_id": self.dispatch_id,
            "change_bundle_id": self.change_bundle_id,
            "detected_at_utc": self.detected_at_utc,
            "changed_sources": list(self.changed_sources),
            "unchanged_sources": list(self.unchanged_sources),
            "failed_sources": dict(self.failed_sources),
            "latest_dates": dict(self.latest_dates),
            "fingerprints": dict(self.current_fingerprints),
        }


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": "petrotwin-detector-state-v1",
        "last_published_fingerprints": {},
        "last_published_bundle_id": None,
        "last_published_at_utc": None,
        "pending_dispatch": None,
    }


def _normalize_state(data: Any, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TypeError(f"Invalid fingerprint state file: {label}")
    merged = _default_state()
    merged.update(data)
    return merged


def load_fingerprint_state(state_path: str | Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Load the last-successfully-published source fingerprints and pending dispatch."""
    path = Path(state_path)
    if not path.is_file():
        return _default_state()
    return _normalize_state(json.loads(path.read_text(encoding="utf-8")), str(path))


class StateConflictError(RuntimeError):
    """Raised when concurrent state writers collide on optimistic SHA check (HTTP 409)."""


class GitHubBranchStateStore:
    """Durable, zero-cost state store backed by a dedicated GitHub branch (Contents API).

    Uses optimistic concurrency (blob SHA) so concurrent writes never silently overwrite
    each other, and never commits to `main` (preventing infinite workflow loops).
    """

    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = DEFAULT_STATE_BRANCH,
        file_path: str = DEFAULT_STATE_REPO_FILE,
        timeout: int = 15,
    ) -> None:
        if not repo or "/" not in repo:
            raise ValueError("repo must be in 'owner/name' format.")
        if not token:
            raise ValueError("GitHub token is required for GitHubBranchStateStore.")
        self.repo = repo
        self.token = token
        self.branch = branch
        self.file_path = file_path
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _ensure_branch_exists(self) -> None:
        ref_url = f"https://api.github.com/repos/{self.repo}/git/ref/heads/{self.branch}"
        resp = requests.get(ref_url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code == 200:
            return
        if resp.status_code != 404:
            resp.raise_for_status()
        # Create branch from default branch HEAD (without modifying default branch)
        repo_resp = requests.get(
            f"https://api.github.com/repos/{self.repo}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        repo_resp.raise_for_status()
        default_branch = repo_resp.json()["default_branch"]
        base_ref = requests.get(
            f"https://api.github.com/repos/{self.repo}/git/ref/heads/{default_branch}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        base_ref.raise_for_status()
        base_sha = base_ref.json()["object"]["sha"]
        create_resp = requests.post(
            f"https://api.github.com/repos/{self.repo}/git/refs",
            headers=self._headers(),
            json={"ref": f"refs/heads/{self.branch}", "sha": base_sha},
            timeout=self.timeout,
        )
        if create_resp.status_code not in (200, 201, 422):
            create_resp.raise_for_status()

    def load(self) -> tuple[dict[str, Any], str | None]:
        """Return (state_dict, blob_sha). Returns default state and None if file does not exist."""
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.file_path}"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={"ref": self.branch},
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return _default_state(), None
        resp.raise_for_status()
        body = resp.json()
        raw = base64.b64decode(body["content"]).decode("utf-8")
        return _normalize_state(json.loads(raw), f"{self.branch}:{self.file_path}"), str(body["sha"])

    def save(self, state: dict[str, Any], expected_sha: str | None, message: str) -> str:
        """Atomically save state using expected blob SHA; raises StateConflictError on HTTP 409."""
        self._ensure_branch_exists()
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.file_path}"
        encoded = base64.b64encode(
            json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        payload: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": self.branch,
        }
        if expected_sha is not None:
            payload["sha"] = expected_sha
        resp = requests.put(url, headers=self._headers(), json=payload, timeout=self.timeout)
        if resp.status_code == 409:
            raise StateConflictError("Concurrent update detected on GitHub state branch (HTTP 409).")
        resp.raise_for_status()
        return str(resp.json()["content"]["sha"])


def confirm_dispatch_id(
    dispatch_id: str,
    *,
    state_path: str | Path | None = DEFAULT_STATE_PATH,
    github_store: GitHubBranchStateStore | None = None,
) -> bool:
    """Promote pending fingerprints ONLY if pending_dispatch matches exact dispatch_id."""
    if not dispatch_id:
        return False

    if github_store is not None:
        state, blob_sha = github_store.load()
    else:
        assert state_path is not None
        state, blob_sha = load_fingerprint_state(state_path), None

    pending = state.get("pending_dispatch")
    if not isinstance(pending, dict) or pending.get("dispatch_id") != dispatch_id:
        return False

    merged = dict(state.get("last_published_fingerprints") or {})
    merged.update(dict(pending.get("fingerprints") or {}))
    new_state = {
        "schema_version": "petrotwin-detector-state-v1",
        "last_published_fingerprints": merged,
        "last_published_bundle_id": pending.get("change_bundle_id") or compute_content_fingerprint(merged),
        "last_published_at_utc": _utcnow_iso(),
        "pending_dispatch": None,
    }
    if github_store is not None:
        github_store.save(new_state, blob_sha, f"state: confirm published {dispatch_id}")
    else:
        assert state_path is not None
        _atomic_save_state(new_state, state_path)
    return True


def _atomic_save_state(state: dict[str, Any], state_path: str | Path) -> Path:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def record_pending_dispatch(
    report: DetectionReport,
    state_path: str | Path = DEFAULT_STATE_PATH,
    *,
    github_store: GitHubBranchStateStore | None = None,
) -> Path:
    """Record an in-flight dispatch WITHOUT advancing last_published_fingerprints."""
    if github_store is not None:
        state, blob_sha = github_store.load()
    else:
        state, blob_sha = load_fingerprint_state(state_path), None

    state["pending_dispatch"] = {
        "dispatch_id": report.dispatch_id,
        "change_bundle_id": report.change_bundle_id,
        "dispatched_at_utc": _utcnow_iso(),
        "fingerprints": dict(report.current_fingerprints),
        "changed_sources": list(report.changed_sources),
    }
    if github_store is not None:
        github_store.save(state, blob_sha, f"state: record pending {report.dispatch_id}")
        return Path(github_store.file_path)
    return _atomic_save_state(state, state_path)


def record_published_fingerprints(
    fingerprints: dict[str, str],
    change_bundle_id: str | None = None,
    state_path: str | Path = DEFAULT_STATE_PATH,
    *,
    github_store: GitHubBranchStateStore | None = None,
) -> Path:
    """Persist fingerprints ONLY after downstream pipeline + TiDB publication succeeds."""
    if github_store is not None:
        existing, blob_sha = github_store.load()
    else:
        existing, blob_sha = load_fingerprint_state(state_path), None

    merged = dict(existing.get("last_published_fingerprints") or {})
    merged.update(fingerprints)
    bundle_id = change_bundle_id or compute_content_fingerprint(merged)

    state = {
        "schema_version": "petrotwin-detector-state-v1",
        "last_published_fingerprints": merged,
        "last_published_bundle_id": bundle_id,
        "last_published_at_utc": _utcnow_iso(),
        "pending_dispatch": None,
    }
    if github_store is not None:
        github_store.save(state, blob_sha, f"state: promote published {bundle_id[:12]}")
        return Path(github_store.file_path)
    return _atomic_save_state(state, state_path)


def reconcile_pending_dispatch(
    repo: str,
    token: str,
    state_path: str | Path = DEFAULT_STATE_PATH,
    workflow_file: str = "petrotwin-update.yml",
    timeout: int = 15,
    *,
    github_store: GitHubBranchStateStore | None = None,
) -> str:
    """Check GitHub Actions status for a pending dispatch and promote state only on success.

    Returns one of: 'none', 'in_progress', 'promoted', 'failed_cleared'.
    """
    if github_store is not None:
        state, blob_sha = github_store.load()
    else:
        state, blob_sha = load_fingerprint_state(state_path), None

    pending = state.get("pending_dispatch")
    if not pending:
        return "none"

    dispatch_id = str(pending.get("dispatch_id") or "")
    dispatched_at = str(pending.get("dispatched_at_utc") or "")
    if not dispatch_id:
        return "in_progress"
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
        "?event=repository_dispatch&per_page=20"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    runs = resp.json().get("workflow_runs") or []

    # Strictly match the exact workflow run associated with this dispatch_id via display_title
    matching = [
        r
        for r in runs
        if dispatch_id in str(r.get("display_title") or "")
        and (not dispatched_at or str(r.get("created_at") or "") >= dispatched_at)
    ]
    if not matching:
        return "in_progress"

    matched_run = matching[0]
    status = str(matched_run.get("status") or "")
    conclusion = str(matched_run.get("conclusion") or "")

    if status != "completed":
        return "in_progress"

    if conclusion == "success":
        record_published_fingerprints(
            dict(pending.get("fingerprints") or {}),
            change_bundle_id=pending.get("change_bundle_id"),
            state_path=state_path,
            github_store=github_store,
        )
        return "promoted"

    # Workflow failed/cancelled/timed_out -> clear pending without advancing published state
    state["pending_dispatch"] = None
    if github_store is not None:
        github_store.save(state, blob_sha, f"state: clear failed {dispatch_id}")
    else:
        _atomic_save_state(state, state_path)
    return "failed_cleared"


def check_brent_source(timeout: int = 20) -> SourceCheckResult:
    """Probe FRED/EIA DCOILBRENTEU CSV and hash full canonical history since 2018-08-22."""
    try:
        resp = requests.get(brent_collector.FRED_CSV_URL, timeout=timeout)
        resp.raise_for_status()
        lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return SourceCheckResult("brent", ok=False, error="empty_fred_response")
        records: list[list[str]] = []
        latest_date: str | None = None
        for ln in lines[1:]:
            parts = [p.strip() for p in ln.split(",", 1)]
            if len(parts) == 2 and parts[0] >= BRENT_START_DATE:
                records.append(parts)
                latest_date = parts[0]
        if not records or latest_date is None:
            return SourceCheckResult("brent", ok=False, error="malformed_fred_csv")
        fp = compute_content_fingerprint({"series": "DCOILBRENTEU", "rows": records})
        return SourceCheckResult(
            source_id="brent",
            ok=True,
            fingerprint=fp,
            latest_date=latest_date,
            summary=f"DCOILBRENTEU rows={len(records)} latest={latest_date}",
        )
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        return SourceCheckResult("brent", ok=False, error=str(exc))


def check_fx_source(
    *,
    current_date: date | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> SourceCheckResult:
    """Probe Algolab / SBV central USD/VND reference rate and reject stale observations."""
    today = current_date or datetime.now(UTC).date()
    try:
        rate, obs_date = fx_collector._fetch_rate()
        if obs_date is None or rate is None:
            return SourceCheckResult("fx", ok=False, error="missing_fx_observation")
        obs_date_obj = date.fromisoformat(obs_date) if isinstance(obs_date, str) else obs_date
        obs_date_str = obs_date_obj.isoformat()
        age_days = (today - obs_date_obj).days
        if age_days < 0:
            return SourceCheckResult("fx", ok=False, error=f"future_date:{obs_date_str}")
        if age_days > max_age_days:
            return SourceCheckResult("fx", ok=False, error=f"stale_data:age_days={age_days}")
        fp = compute_content_fingerprint({"series": "usd_vnd_sbv", "date": obs_date_str, "rate": str(rate)})
        return SourceCheckResult(
            source_id="fx",
            ok=True,
            fingerprint=fp,
            latest_date=obs_date_str,
            summary=f"SBV USD/VND date={obs_date_str} rate={rate}",
        )
    except (requests.RequestException, ValueError, KeyError, RuntimeError, OSError) as exc:
        return SourceCheckResult("fx", ok=False, error=str(exc))


def check_domestic_fuel_source(
    *,
    current_date: date | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> SourceCheckResult:
    """Probe active domestic fuel price source (COMECO) and reject stale announcements."""
    today = current_date or datetime.now(UTC).date()
    try:
        html = comeco_collector._fetch_html()
        eff_date, eff_time, records = comeco_collector._parse_comeco_html(html, current_date=today)
        if not eff_date or not records:
            return SourceCheckResult("domestic_fuel", ok=False, error="empty_comeco_parse")
        eff_date_obj = date.fromisoformat(eff_date) if isinstance(eff_date, str) else eff_date
        eff_date_str = eff_date_obj.isoformat()
        age_days = (today - eff_date_obj).days
        if age_days > max_age_days:
            return SourceCheckResult("domestic_fuel", ok=False, error=f"stale_data:age_days={age_days}")
        canonical_rows = sorted(
            [
                {
                    "effective_date": eff_date_str,
                    "effective_time": eff_time,
                    "product_name": str(r["product_name"]),
                    "price_vnd_per_liter": int(r["price"]),
                }
                for r in records
            ],
            key=lambda x: x["product_name"],
        )
        fp = compute_content_fingerprint({"source": "comeco", "rows": canonical_rows})
        return SourceCheckResult(
            source_id="domestic_fuel",
            ok=True,
            fingerprint=fp,
            latest_date=eff_date_str,
            summary=f"COMECO effective={eff_date_str} {eff_time} ({len(canonical_rows)} products)",
        )
    except (requests.RequestException, ValueError, KeyError, RuntimeError, OSError) as exc:
        return SourceCheckResult("domestic_fuel", ok=False, error=str(exc))


DEFAULT_SOURCE_CHECKERS: dict[str, Callable[[], SourceCheckResult]] = {
    "brent": check_brent_source,
    "fx": check_fx_source,
    "domestic_fuel": check_domestic_fuel_source,
}


def detect_source_changes(
    checkers: dict[str, Callable[[], SourceCheckResult]] | None = None,
    state_path: str | Path = DEFAULT_STATE_PATH,
    *,
    github_store: GitHubBranchStateStore | None = None,
) -> DetectionReport:
    """Check all configured sources independently and decide whether to trigger one run.

    Failed/stale/unavailable sources are logged and never cause false-positive triggers
    or overwrite previously recorded fingerprints.
    """
    active_checkers = checkers if checkers is not None else DEFAULT_SOURCE_CHECKERS
    if github_store is not None:
        state, _ = github_store.load()
    else:
        state = load_fingerprint_state(state_path)
    last_published: dict[str, str] = dict(state.get("last_published_fingerprints") or {})
    last_bundle_id: str | None = state.get("last_published_bundle_id")
    pending = state.get("pending_dispatch")
    pending_bundle_id: str | None = pending.get("change_bundle_id") if isinstance(pending, dict) else None

    changed: list[str] = []
    unchanged: list[str] = []
    failed: dict[str, str] = {}
    current_fps: dict[str, str] = {}
    latest_dates: dict[str, str] = {}

    for source_id, check_fn in active_checkers.items():
        res = check_fn()
        if not res.ok or not res.fingerprint:
            failed[source_id] = res.error or "source_unavailable"
            logger.warning("source=%s status=failed error=%s", source_id, failed[source_id])
            continue

        current_fps[source_id] = res.fingerprint
        if res.latest_date:
            latest_dates[source_id] = res.latest_date

        if last_published.get(source_id) != res.fingerprint:
            changed.append(source_id)
            logger.info("source=%s status=changed latest=%s", source_id, res.latest_date)
        else:
            unchanged.append(source_id)

    if not changed:
        return DetectionReport(
            detected_at_utc=_utcnow_iso(),
            changed_sources=(),
            unchanged_sources=tuple(sorted(unchanged)),
            failed_sources=failed,
            current_fingerprints=current_fps,
            latest_dates=latest_dates,
            change_bundle_id=None,
            should_trigger=False,
            is_duplicate_bundle=False,
        )

    detected_at = _utcnow_iso()
    merged_candidate = dict(last_published)
    merged_candidate.update(current_fps)
    bundle_id = compute_content_fingerprint(merged_candidate)
    is_dup = (
        (last_bundle_id is not None and bundle_id == last_bundle_id)
        or (pending_bundle_id is not None and bundle_id == pending_bundle_id)
    )
    compact_ts = detected_at.replace("-", "").replace(":", "").split(".")[0]
    dispatch_id = f"disp-{bundle_id[:12]}-{compact_ts}"

    return DetectionReport(
        detected_at_utc=detected_at,
        changed_sources=tuple(sorted(changed)),
        unchanged_sources=tuple(sorted(unchanged)),
        failed_sources=failed,
        current_fingerprints=current_fps,
        latest_dates=latest_dates,
        change_bundle_id=bundle_id,
        should_trigger=not is_dup,
        is_duplicate_bundle=is_dup,
        dispatch_id=dispatch_id,
    )


def trigger_repository_dispatch(
    repo: str,
    token: str,
    report: DetectionReport,
    event_type: str = EVENT_TYPE_NEW_DATA,
    timeout: int = 15,
) -> int:
    """Trigger GitHub Actions via repository_dispatch without logging credentials."""
    if not repo or "/" not in repo:
        raise ValueError("repo must be in 'owner/name' format.")
    if not token:
        raise ValueError("GITHUB_DISPATCH_TOKEN must be set before dispatching.")
    if not report.should_trigger:
        return 0

    url = f"https://api.github.com/repos/{repo}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {
        "event_type": event_type,
        "client_payload": report.to_dispatch_payload(),
    }
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.status_code


def run_detector_cycle(
    *,
    repo: str,
    token: str | None = None,
    dispatch: bool = False,
    state_path: str | Path = DEFAULT_STATE_PATH,
    checkers: dict[str, Callable[[], SourceCheckResult]] | None = None,
    lock_name: str = "change_detector",
    github_store: GitHubBranchStateStore | None = None,
) -> dict[str, Any]:
    """Execute one locked detector cycle: reconcile pending -> detect -> optionally dispatch."""
    with file_lock(lock_name) as acquired:
        if not acquired:
            return {"status": "lock_busy", "should_trigger": False}

        try:
            reconcile_status = "none"
            if dispatch and token:
                reconcile_status = reconcile_pending_dispatch(
                    repo=repo,
                    token=token,
                    state_path=state_path,
                    github_store=github_store,
                )
                if reconcile_status == "in_progress":
                    return {"status": "workflow_in_progress", "should_trigger": False}

            report = detect_source_changes(
                checkers=checkers,
                state_path=state_path,
                github_store=github_store,
            )
            dispatched = False
            if dispatch and report.should_trigger:
                trigger_repository_dispatch(repo=repo, token=token or "", report=report)
                record_pending_dispatch(report, state_path=state_path, github_store=github_store)
                dispatched = True
        except StateConflictError:
            logger.warning("State conflict (HTTP 409) detected; skipping cycle safely.")
            return {"status": "state_conflict", "should_trigger": False, "dispatched": False}

        return {
            "status": "dispatched" if dispatched else "checked",
            "reconcile_status": reconcile_status,
            "should_trigger": report.should_trigger,
            "is_duplicate_bundle": report.is_duplicate_bundle,
            "dispatched": dispatched,
            **report.to_dispatch_payload(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="PetroTwin event-driven data-change detector.")
    parser.add_argument("--state-backend", choices=("file", "github"), default="file")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--state-branch", default=DEFAULT_STATE_BRANCH)
    parser.add_argument("--state-repo-file", default=DEFAULT_STATE_REPO_FILE)
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "dmins06/PetroTwin-"))
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Reconcile pending runs and send GitHub repository_dispatch when new data is found.",
    )
    parser.add_argument(
        "--confirm-dispatch-id",
        default="",
        help="Promote pending_dispatch fingerprints ONLY if matching this exact dispatch_id.",
    )
    args = parser.parse_args()

    token = os.getenv("GITHUB_DISPATCH_TOKEN", "")
    github_store = (
        GitHubBranchStateStore(
            repo=args.repo,
            token=token,
            branch=args.state_branch,
            file_path=args.state_repo_file,
        )
        if args.state_backend == "github"
        else None
    )

    if args.confirm_dispatch_id:
        try:
            confirmed = confirm_dispatch_id(
                args.confirm_dispatch_id,
                state_path=args.state_file,
                github_store=github_store,
            )
            status = "confirmed" if confirmed else "no_match"
        except StateConflictError:
            confirmed = False
            status = "state_conflict"
        print(
            json.dumps(
                {"status": status, "dispatch_id": args.confirm_dispatch_id, "confirmed": confirmed},
                ensure_ascii=False,
            )
        )
        return

    result = run_detector_cycle(
        repo=args.repo,
        token=token,
        dispatch=args.dispatch,
        state_path=args.state_file,
        github_store=github_store,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
