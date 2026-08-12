"""Target-maturity-safe local analogs for the Ord River district."""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral
from typing import Any, Mapping, Sequence

import numpy as np

from ordcast.types import (
    SourceForecast,
    _immutable_array,
    _strict_numeric_array,
    validate_horizons,
)


@dataclass(frozen=True)
class AnalogSelection:
    indices: np.ndarray
    issue_times: np.ndarray
    outcome_available_at: np.ndarray
    distances: np.ndarray

    def __post_init__(self) -> None:
        object.__setattr__(self, "indices", _immutable_array(self.indices, np.int64))
        object.__setattr__(
            self,
            "issue_times",
            _immutable_array(self.issue_times, "datetime64[ns]"),
        )
        object.__setattr__(
            self,
            "outcome_available_at",
            _immutable_array(self.outcome_available_at, "datetime64[ns]"),
        )
        object.__setattr__(
            self, "distances", _immutable_array(self.distances, np.float64)
        )


class AnalogLibrary:
    """Retrieve past local states with target-maturity protection.

    Production features still need an external ``available_at``-aware feature view;
    this class cannot prove when caller-provided historical features arrived.
    """

    def __init__(
        self,
        issue_times: Any,
        features: Any,
        outcomes_mm: Any,
        feature_names: Sequence[str],
        horizons_hours: Sequence[int],
        outcome_available_at: Any | None = None,
    ) -> None:
        times = np.asarray(issue_times, dtype="datetime64[ns]")
        feature_array = _strict_numeric_array(features, "features")
        outcomes = _strict_numeric_array(outcomes_mm, "outcomes_mm")
        names = tuple(str(name) for name in feature_names)
        horizons = validate_horizons(horizons_hours)

        if times.ndim != 1 or times.size == 0 or np.isnat(times).any():
            raise ValueError("issue_times must be a non-empty one-dimensional datetime array")
        if feature_array.shape != (times.size, len(names)) or not names:
            raise ValueError("features must have shape (cases, feature_names)")
        if outcomes.shape != (times.size, len(horizons)) or not horizons:
            raise ValueError("outcomes_mm must have shape (cases, horizons)")
        if not np.all(np.isfinite(feature_array)):
            raise ValueError("features must be finite")
        if not np.all(np.isfinite(outcomes)) or np.any(outcomes < 0):
            raise ValueError("outcomes_mm must be finite and non-negative")
        if outcomes.shape[1] > 1 and np.any(np.diff(outcomes, axis=1) < 0):
            raise ValueError("cumulative analog outcomes cannot decrease")
        if len(names) != len(set(names)):
            raise ValueError("feature_names must be unique")
        if np.unique(times).size != times.size:
            raise ValueError("analog issue_times must be unique")

        if outcome_available_at is None:
            availability = times + np.timedelta64(max(horizons), "h")
        else:
            availability = np.asarray(outcome_available_at, dtype="datetime64[ns]")
            if availability.shape != times.shape or np.isnat(availability).any():
                raise ValueError("outcome_available_at must provide one valid time per case")
        minimum_availability = times + np.timedelta64(max(horizons), "h")
        if np.any(availability < minimum_availability):
            raise ValueError(
                "outcome_available_at cannot precede issue time plus the longest horizon"
            )

        order = np.argsort(times, kind="mergesort")
        self.issue_times = _immutable_array(times[order], "datetime64[ns]")
        self.outcome_available_at = _immutable_array(
            availability[order], "datetime64[ns]"
        )
        self.features = _immutable_array(feature_array[order], np.float64)
        self.outcomes_mm = _immutable_array(outcomes[order], np.float64)
        self.feature_names = names
        self.horizons_hours = horizons

    def _query_array(self, query: Any) -> np.ndarray:
        if isinstance(query, Mapping):
            missing = [name for name in self.feature_names if name not in query]
            if missing:
                raise ValueError(f"missing analog features: {', '.join(missing)}")
            values = np.asarray([query[name] for name in self.feature_names], dtype=np.float64)
        else:
            values = np.asarray(query, dtype=np.float64)
        if values.shape != (len(self.feature_names),) or not np.all(np.isfinite(values)):
            raise ValueError("query must provide one finite value per feature")
        return values

    def forecast(
        self,
        query: Any,
        issue_time: Any,
        *,
        k: int = 30,
        embargo_hours: int = 0,
        source_name: str = "local_analogs",
    ) -> tuple[SourceForecast, AnalogSelection]:
        """Return weighted past outcomes from data available before issue_time."""
        current_time = np.datetime64(issue_time, "ns")
        if np.isnat(current_time):
            raise ValueError("issue_time must be a valid datetime")
        if isinstance(embargo_hours, (bool, np.bool_)) or not isinstance(
            embargo_hours, Integral
        ):
            raise ValueError("embargo_hours must be a non-negative integer")
        embargo = int(embargo_hours)
        if embargo < 0:
            raise ValueError("embargo_hours cannot be negative")
        cutoff = current_time - np.timedelta64(embargo, "h")
        candidate_indices = np.flatnonzero(self.outcome_available_at <= cutoff)
        if candidate_indices.size == 0:
            raise ValueError("no historical analogs exist before the leakage cutoff")

        query_values = self._query_array(query)
        candidates = self.features[candidate_indices]
        centre = np.median(candidates, axis=0)
        mad = np.median(np.abs(candidates - centre), axis=0) * 1.4826
        standard_deviation = np.std(candidates, axis=0)
        scale = np.where(mad > 1e-9, mad, np.where(standard_deviation > 1e-9, standard_deviation, 1.0))
        distances = np.sqrt(np.mean(np.square((candidates - query_values) / scale), axis=1))

        if isinstance(k, (bool, np.bool_)) or not isinstance(k, Integral) or int(k) <= 0:
            raise ValueError("k must be a positive integer")
        neighbour_count = min(int(k), candidate_indices.size)
        local_order = np.argsort(distances, kind="mergesort")[:neighbour_count]
        selected_indices = candidate_indices[local_order]
        selected_distances = distances[local_order]
        positive = selected_distances[selected_distances > 1e-12]
        bandwidth = float(np.median(positive)) if positive.size else 1.0
        bandwidth = max(bandwidth, 1e-6)
        weights = np.exp(-0.5 * np.square(selected_distances / bandwidth))
        if weights.sum() <= 0:
            weights = np.ones_like(weights)

        source = SourceForecast(
            name=source_name,
            horizons_hours=self.horizons_hours,
            members_mm=self.outcomes_mm[selected_indices],
            member_weights=weights,
            half_life_hours=1e12,
            metadata={
                "kind": "local_state_analogs",
                "feature_names": list(self.feature_names),
                "availability_cutoff": str(cutoff),
                "k": neighbour_count,
            },
        )
        selection = AnalogSelection(
            indices=selected_indices,
            issue_times=self.issue_times[selected_indices],
            outcome_available_at=self.outcome_available_at[selected_indices],
            distances=selected_distances,
        )
        return source, selection
