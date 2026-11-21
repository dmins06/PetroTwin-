"""Baseline forecasting models."""

from petrotwin.models.baselines.naive import NaivePersistenceBaseline
from petrotwin.models.baselines.seasonal_naive import SeasonalNaiveBaseline

__all__ = [
    "NaivePersistenceBaseline",
    "SeasonalNaiveBaseline",
]
