# PetroTwin — T25 review against T20/T21 and GitHub readiness

**Status:** Review of available T20 excerpt, T21 configuration excerpts, and user-provided benchmark output. **Not** a complete repository audit or an official backtest sign-off.

## 1. Findings

| Requirement | Evidence currently available | Assessment / next step |
|---|---|---|
| T21 expanding folds | Four fold counts match: train 632/737/855/985; validation 105/118/130/163 | PASS for counts only |
| T21 rolling folds | Four fold-start counts match: train 284/321/367/429; validation 105/118/130/163 | PASS for counts only; clarify moving vs fixed intra-fold training window |
| Baseline comparison | Naive and Seasonal Naive scored on 513 common target rows, 3 cold starts | PASS for simulation; origin protocol not official |
| Fold-level / pooled / product metrics | MAE, RMSE, MAPE and product CSV produced | PASS for simulation |
| Fold mean/std | Separate metrics_mean_std.csv, sample std ddof=1 | PASS; integrate into pipeline for reproducibility |
| T20 continuous origins | T20: new valid Brent and/or USD/VND triggers new forecast; simultaneous updates share snapshot | NOT IMPLEMENTED in simulated T25: uses target effective timestamp minus 1 second |
| T20 origin/target mapping | Multiple origins per open cycle, variable horizon, tentative next target until official announcement | PENDING; do not derive origin from future target |
| T20/T21 point-in-time guard | feature_available_at <= forecast_origin | NOT VERIFIED: historical published_at/available_at missing for Brent/FX |
| T21 consistent comparison | Same folds, origins, horizons, eligible target-origin rows for all models | PENDING M3/M4 shared manifest |
| T21 locked final test | 2026 reserved for final evaluation | DO NOT use for tuning or selection; processed dataset previously had only through 2026-10-22 |
| GitHub readiness | Local test outputs previously passed; current working tree unknown | Run local status/tests/diff before committing; no push performed here |

## 2. Decisions needed from M3/M4 before official T25

1. **Origin generation**: store UTC inference timestamp for each valid Brent/FX update, deduplicate simultaneous updates by feature snapshot and model version. For retrospective data, distinguish an auditable origin from a scenario/synthetic origin.
2. **Availability**: define provenance of `published_at`, `retrieved_at_utc`, and `available_at`; do not use later backfill collection timestamps as proof of historical availability. Define a documented fallback evaluation protocol if historical availability cannot be established; label it approximate rather than official point-in-time.
3. **Target matching**: map each origin to its next realized official adjustment event after the fact, while leaving the future event tentative when forecasting. Retain multiple origins per event and variable horizon.
4. **Window semantics**: approve rolling 730-day window anchored at each origin versus fixed fold-start cutoff; retain expanding comparison.
5. **Common evaluation manifest**: use immutable rows with `forecast_origin_utc`, `product_id`, `target_event_id`, `target_effective_at_local`, `horizon`, `fold_id`, `window_type`, `eligibility_status`, `feature_snapshot_id` and data-availability provenance. Use identical rows for Naive, Seasonal, SARIMAX and XGBoost.
6. **Cold-start policy**: report prediction coverage and evaluate on a common eligible subset; do not silently assign zero errors to missing predictions.

## 3. Proposed acceptance tests

- An origin cannot access a domestic price not yet effective or not yet known.
- Brent/FX rows with `available_at > forecast_origin` are excluded even if observation_date is earlier.
- A late-arriving revision never overwrites a previously emitted historical forecast.
- Multiple origins may point to one target event; a target event is not known to the model at inference time.
- All models use identical origin-target rows and split boundaries; preprocessing is fit only on training data.
- No final-test 2026 observations enter model selection.
- Metrics, cold-start counts, and fold totals reproduce from saved prediction files.

## 4. Safe GitHub workflow (run locally in VS Code PowerShell)

```powershell
git status --short
git branch --show-current
git diff --check
git diff --stat
python -m pytest tests/unit/test_baselines.py -v
```

Review untracked/generated files before staging. If reports are intended for version control, avoid large/raw datasets and secrets. Stage **explicitly**:

```powershell
git add src/petrotwin/evaluation/baseline_benchmark.py
git add reports/baseline_simulated/T25_Baseline_Benchmark_Report.md
# Add small, approved CSV summaries individually if the team wants them versioned.
git diff --cached --check
git diff --cached --stat
git commit -m "Add simulated T25 baseline evaluation and review"
git push -u origin feature/m4-t25-benchmark
```

Do **not** execute the `git add` line for a report until that file is present at the stated path. Do **not** commit if tests fail, or push if the branch name differs, there are unreviewed secrets, or the team prefers a different branch/PR workflow.

## 5. Next deliverable

After M3/M4 agree on origin and availability rules: implement a shared forecast-origin manifest, add temporal-leakage unit tests, rerun both baselines, regenerate all metrics, and explicitly separate simulated versus official outputs. Only then request official T25 sign-off.
