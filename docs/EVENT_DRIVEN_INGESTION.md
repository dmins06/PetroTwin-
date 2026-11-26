# PetroTwin Event-Driven Data Ingestion (100% Free GitHub Actions Architecture)

## Overview

PetroTwin uses a **two-workflow, zero-cost (0 VND/month) GitHub Actions architecture** so the heavy T14 data collection + TiDB publication pipeline runs **only when genuinely new or revised source data is detected**, without requiring any external VPS, paid Render service, or third-party scheduler:

1. **Workflow 1 — Lightweight Change Detector** (`.github/workflows/petrotwin-detect.yml`):
   - Runs on `schedule` every 30 minutes (`*/30 * * * *`) and supports manual `workflow_dispatch`.
   - Installs only lightweight Python HTTP/HTML libraries (`requests`, `beautifulsoup4`, `PyYAML`) and holds **zero TiDB credentials**.
   - Independently probes all three active upstream sources via `python -m petrotwin.collectors.change_detector`:
     - **Brent crude (`brent`)**: FRED/EIA `DCOILBRENTEU` full canonical history since `2018-08-22` (detects both new daily prices and historical EIA revisions).
     - **USD/VND FX (`fx`)**: Algolab / SBV central reference rate (with 7-day freshness guard).
     - **Domestic fuel (`domestic_fuel`)**: Active COMECO retail fuel announcement (`comeco.py`, 4 tracked products, with 7-day freshness guard). PVOIL remains historical in `data/raw/data_price_petro.csv`.
   - Computes deterministic SHA-256 content fingerprints (never timestamps alone).
   - Emits at most **one** `repository_dispatch` (`event_type: petrotwin_new_data`) with a unique `dispatch_id` when any source fingerprint differs from `last_published_fingerprints`.
   - **Never** advances `last_published_fingerprints` upon detection or dispatch.

2. **Workflow 2 — Event-Driven T14 Pipeline** (`.github/workflows/petrotwin-update.yml`):
   - Triggered by `repository_dispatch` (`types: [petrotwin_new_data]`) or manual `workflow_dispatch`.
   - Embeds `dispatch_id` in `run-name` (`PetroTwin Update [<dispatch_id>]`) for exact run correlation.
   - Runs `python -m petrotwin.collectors.runner` (T14 collection, validation, snapshot/manifest generation, and idempotent TiDB publication) and commits updated `data/raw/*.csv` to `main`.
   - Only after all publication steps succeed, confirms **its own exact `dispatch_id`** via `--confirm-dispatch-id` on the `state/detector` branch.

---

## Durable State Persistence (`state/detector` Branch)

GitHub Actions runners are ephemeral, so detector state cannot live only on the local runner filesystem. PetroTwin persists `source_fingerprints.json` on a dedicated orphan/state branch **`state/detector`** via the GitHub Contents API (`GitHubBranchStateStore`):

- **Zero external infrastructure**: No database, Redis, S3, or paid host required.
- **No infinite workflow loops**: State updates are committed only to `state/detector`, never to `main`, and neither workflow triggers on `push`.
- **Optimistic concurrency (CAS)**: Every write passes the previously read blob `sha`; concurrent writers receive HTTP `409` (`StateConflictError`) and exit safely without overwriting state.
- **Crash & failure recovery (defense in depth)**:
  1. Primary confirmation: `petrotwin-update.yml` calls `--confirm-dispatch-id <dispatch_id>` after T14 publication succeeds.
  2. Fallback reconciliation: Before dispatching, `petrotwin-detect.yml` calls `reconcile_pending_dispatch()`, which inspects the exact GitHub Actions run whose `display_title` contains `dispatch_id`:
     - `status != "completed"` → returns `workflow_in_progress` (suppresses duplicate dispatches).
     - `conclusion == "success"` → promotes `pending_dispatch` to `last_published_fingerprints`.
     - `conclusion in {"failure", "cancelled", "timed_out"}` → clears `pending_dispatch` **without** advancing `last_published_fingerprints`, so the next cycle automatically retries.

---

## Same-Day Idempotency (T14 TiDB Loader)

`src/petrotwin/data/mysql_loader.py` reconciles same-day reruns safely via `_immutable_manifest_identity()` and `_immutable_source_identity()`:

- Volatile retrieval timestamps (`retrieved_at_utc`) are excluded from manifest equality comparisons.
- All content-bearing fields (`source_id`, `source_name`, `sha256`, `row_count`, `size_bytes`, `schema_version`, `file_path`, and every output artifact hash/row count) remain strictly verified.
- Identical same-day reruns reconcile as `already_published`; any genuine content or hash mismatch still fails closed.

---

## Required GitHub Secrets & Variables

### Repository Secret for Event-Driven Detection & Dispatch

Create a **Fine-Grained Personal Access Token (PAT)** scoped **only** to `dmins06/PetroTwin-`:

| Repository Permission | Access Level | Reason |
| --- | --- | --- |
| **Contents** | Read and write | Send `repository_dispatch` (`POST /dispatches`) and read/write `source_fingerprints.json` on branch `state/detector` |
| **Actions** | Read-only | Query exact workflow run status in `reconcile_pending_dispatch()` |

Store this PAT as repository secret **`PETROTWIN_DISPATCH_TOKEN`** (GitHub reserves the `GITHUB_` prefix for built-in secrets).

- `.github/workflows/petrotwin-detect.yml` uses **only** `PETROTWIN_DISPATCH_TOKEN` (no TiDB secrets; `GITHUB_TOKEN` stays `contents: read`).
- `.github/workflows/petrotwin-update.yml` keeps existing TiDB variables/secrets (`PETROTWIN_DB_HOST`, `PETROTWIN_DB_PORT`, `PETROTWIN_DB_NAME`, `PETROTWIN_DB_USER`, `PETROTWIN_DB_PASSWORD`) plus optional `PETROTWIN_DISPATCH_TOKEN` for immediate post-success state confirmation.

---

## Monthly GitHub Actions Cost & Quota Estimate

- **Public repository**: **0 VND/month** (unlimited standard GitHub-hosted runner minutes).
- **Private repository** (GitHub Free tier: **2,000 Linux minutes/month**):
  - `petrotwin-detect.yml` (`*/30 * * * *`): 48 runs/day × 30 days = **1,440 runs/month**. Each lightweight run takes ~20–30s (billed at the 1-minute minimum per run on private repos) = **1,440 minutes/month**.
  - `petrotwin-update.yml` (event-driven only when Brent/FX/COMECO changes): ~20–30 runs/month × ~2 minutes = **40–60 minutes/month**.
  - **Total estimated usage**: **~1,480–1,500 minutes/month** — comfortably within the 2,000-minute/month free tier (**0 VND/month**).

---

## Local Testing Commands

```bash
# Dry-run detector locally using local file backend (no dispatch)
PYTHONPATH=src python3 -m petrotwin.collectors.change_detector

# Run unit tests for T14 idempotency, detector state, and workflow configs
PYTHONPATH=src python3 -m pytest -q tests/unit/test_idempotency_t14.py tests/unit/test_change_detector.py
```
