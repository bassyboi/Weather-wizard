"""Probabilistic forecast metrics used for training and honest hindcasts."""

from __future__ import annotations

from typing import Any

import numpy as np

from ordcast.types import _normalize_nonnegative_weights, _strict_numeric_array


def ensemble_crps(members: Any, observation: float, weights: Any | None = None) -> float:
    """Continuous ranked probability score for a weighted ensemble."""
    values = _strict_numeric_array(members, "members")
    if values.ndim != 1 or values.size == 0 or not np.all(np.isfinite(values)):
        raise ValueError("members must be a non-empty, finite one-dimensional array")
    observed = float(observation)
    if not np.isfinite(observed):
        raise ValueError("observation must be finite")

    if weights is None:
        probabilities = np.full(values.size, 1.0 / values.size)
    else:
        probabilities = _strict_numeric_array(weights, "weights")
        if probabilities.shape != values.shape:
            raise ValueError("weights must match members")
        if not np.all(np.isfinite(probabilities)) or np.any(probabilities < 0):
            raise ValueError("weights must be finite and non-negative")
        probabilities = _normalize_nonnegative_weights(probabilities, "weights")

    accuracy = np.sum(probabilities * np.abs(values - observed))
    pairwise = np.abs(values[:, None] - values[None, :])
    spread = 0.5 * np.sum(probabilities[:, None] * probabilities[None, :] * pairwise)
    return float(accuracy - spread)


def brier_score(probability: float, occurred: bool) -> float:
    forecast = float(probability)
    if not np.isfinite(forecast) or not 0 <= forecast <= 1:
        raise ValueError("probability must be between zero and one")
    return float((forecast - float(bool(occurred))) ** 2)
