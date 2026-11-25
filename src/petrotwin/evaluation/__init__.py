"""PetroTwin chronological validation splits, evaluation metrics, and T25 benchmark."""

from petrotwin.evaluation.benchmark import BaselineBenchmarkResult, run_baseline_benchmark
from petrotwin.evaluation.metrics import (
    BOG_CLASS_NAMES,
    BOG_CLASSES,
    PROB_CLIP_EPS,
    aggregate_regression_metrics,
    compute_bog_metrics,
    compute_regression_metrics,
    evaluate_baselines_on_t21,
)
from petrotwin.evaluation.splits import (
    ValidationSplit,
    get_final_test_split,
    iter_validation_splits,
)

__all__ = [
    "BOG_CLASSES",
    "BOG_CLASS_NAMES",
    "PROB_CLIP_EPS",
    "BaselineBenchmarkResult",
    "ValidationSplit",
    "aggregate_regression_metrics",
    "compute_bog_metrics",
    "compute_regression_metrics",
    "evaluate_baselines_on_t21",
    "get_final_test_split",
    "iter_validation_splits",
    "run_baseline_benchmark",
]
