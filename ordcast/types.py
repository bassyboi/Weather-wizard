"""Validated forecast data structures used by OrdCast."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from numbers import Real
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import numpy as np


def _strict_numeric_array(values: Any, name: str) -> np.ndarray:
    """Copy real numeric input without accepting booleans or numeric strings."""
    try:
        raw = np.asarray(values)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a regular numeric array") from error
    if raw.dtype.kind not in "iuf":
        raise ValueError(f"{name} must contain real numbers, not booleans or strings")
    return np.array(raw, dtype=np.float64, copy=True)


def _immutable_array(values: Any, dtype: Any | None = None) -> np.ndarray:
    """Return an array backed by immutable bytes, not merely a write-disabled owner."""
    array = np.ascontiguousarray(values, dtype=dtype)
    return np.frombuffer(array.tobytes(order="C"), dtype=array.dtype).reshape(array.shape)


def _normalize_nonnegative_weights(
    values: Any, name: str, *, axis: int | None = None
) -> np.ndarray:
    """Normalize finite weights without overflow for very large finite values."""
    weights = _strict_numeric_array(values, name)
    if not np.all(np.isfinite(weights)) or np.any(weights < 0):
        raise ValueError(f"{name} must be finite and non-negative")
    maximum = np.max(weights, axis=axis, keepdims=True)
    if np.any(maximum <= 0):
        raise ValueError(f"{name} must have positive mass")
    scaled = weights / maximum
    total = np.sum(scaled, axis=axis, keepdims=True)
    if not np.all(np.isfinite(total)) or np.any(total <= 0):
        raise ValueError(f"{name} must have positive finite mass")
    return scaled / total


def _deep_freeze(value: Any) -> Any:
    """Defensively copy and recursively freeze metadata containers."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    if isinstance(value, np.ndarray):
        return _immutable_array(np.array(value, copy=True))
    return deepcopy(value)


def validate_horizons(values: Sequence[int]) -> tuple[int, ...]:
    """Validate positive integral hours without silently truncating inputs."""
    checked: list[int] = []
    for value in values:
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
            raise ValueError("forecast horizons must be integral numeric hours")
        numeric = float(value)
        if not np.isfinite(numeric) or not numeric.is_integer():
            raise ValueError("forecast horizons must be integral numeric hours")
        checked.append(int(numeric))
    horizons = tuple(checked)
    if not horizons:
        raise ValueError("at least one forecast horizon is required")
    if any(value <= 0 for value in horizons):
        raise ValueError("forecast horizons must be positive hours")
    if any(right <= left for left, right in zip(horizons, horizons[1:])):
        raise ValueError("forecast horizons must be strictly increasing")
    return horizons


def _as_2d(values: Any, name: str) -> np.ndarray:
    array = _strict_numeric_array(values, name)
    if array.ndim == 1:
        array = array[:, None]
    if array.ndim != 2 or array.shape[0] == 0 or array.shape[1] == 0:
        raise ValueError(f"{name} must have shape (members, horizons)")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must contain only finite values")
    return array


def weighted_quantile(
    values: np.ndarray, weights: np.ndarray, quantiles: Sequence[float]
) -> np.ndarray:
    """Return the left-continuous inverse CDF of a weighted empirical sample."""
    sample = _strict_numeric_array(values, "values")
    sample_weights = _strict_numeric_array(weights, "weights")
    requested = _strict_numeric_array(quantiles, "quantiles")

    if sample.ndim != 1 or sample_weights.shape != sample.shape:
        raise ValueError("values and weights must be one-dimensional and equal length")
    if not np.all(np.isfinite(sample)) or not np.all(np.isfinite(sample_weights)):
        raise ValueError("values and weights must be finite")
    if np.any(sample_weights < 0) or np.max(sample_weights) <= 0:
        raise ValueError("weights must be non-negative with a positive sum")
    if not np.all(np.isfinite(requested)) or np.any((requested < 0) | (requested > 1)):
        raise ValueError("quantiles must be finite and between zero and one")

    order = np.argsort(sample, kind="mergesort")
    sorted_values = sample[order]
    sorted_weights = sample_weights[order]
    positive = sorted_weights > 0
    sorted_values = sorted_values[positive]
    sorted_weights = sorted_weights[positive]
    sorted_weights = _normalize_nonnegative_weights(sorted_weights, "weights")

    # A forecast ensemble is a discrete empirical distribution. Merge tied support
    # points before inverting its CDF so results cannot depend on source/member order.
    unique_values, inverse = np.unique(sorted_values, return_inverse=True)
    unique_weights = np.asarray(
        [
            math.fsum(
                sorted(float(weight) for weight in sorted_weights[inverse == index])
            )
            for index in range(unique_values.size)
        ],
        dtype=np.float64,
    )
    unique_weights = _normalize_nonnegative_weights(unique_weights, "weights")
    cumulative = np.cumsum(unique_weights)
    indices = np.searchsorted(cumulative, requested, side="left")
    return unique_values[np.clip(indices, 0, unique_values.size - 1)]


@dataclass(frozen=True)
class SourceForecast:
    """One source's rainfall members for identical cumulative valid horizons.

    ``members_mm[:, j]`` is accumulated rain from forecast issue through
    ``horizons_hours[j]``. Observations used for CRPS must match that exact window.
    """

    name: str
    horizons_hours: Sequence[int]
    members_mm: Any
    age_hours: float = 0.0
    half_life_hours: float = 24.0
    member_weights: Any | None = None
    recent_crps: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("source name cannot be empty")
        horizons = validate_horizons(self.horizons_hours)
        members = _as_2d(self.members_mm, "members_mm")
        if members.shape[1] != len(horizons):
            raise ValueError("members_mm horizon dimension does not match horizons_hours")
        if np.any(members < 0):
            raise ValueError("rainfall scenario members cannot be negative")
        if members.shape[1] > 1 and np.any(np.diff(members, axis=1) < 0):
            raise ValueError("cumulative rainfall cannot decrease with forecast horizon")

        if isinstance(self.age_hours, (bool, np.bool_)) or not isinstance(
            self.age_hours, Real
        ):
            raise ValueError("age_hours must be a real number")
        if isinstance(self.half_life_hours, (bool, np.bool_)) or not isinstance(
            self.half_life_hours, Real
        ):
            raise ValueError("half_life_hours must be a real number")
        age = float(self.age_hours)
        half_life = float(self.half_life_hours)
        if not np.isfinite(age) or age < 0:
            raise ValueError("age_hours must be finite and non-negative")
        if not np.isfinite(half_life) or half_life <= 0:
            raise ValueError("half_life_hours must be finite and positive")

        weights = self.member_weights
        if weights is None:
            normalized_weights = np.full(members.shape, 1.0 / members.shape[0])
        else:
            normalized_weights = _strict_numeric_array(weights, "member_weights")
            if normalized_weights.ndim == 1:
                if normalized_weights.shape[0] != members.shape[0]:
                    raise ValueError("member_weights length must match number of members")
                normalized_weights = np.repeat(
                    normalized_weights[:, None], members.shape[1], axis=1
                )
            if normalized_weights.shape != members.shape:
                raise ValueError("member_weights must have shape (members, horizons)")
            normalized_weights = _normalize_nonnegative_weights(
                normalized_weights, "member_weights", axis=0
            )

        crps = self.recent_crps
        if crps is not None:
            crps = _strict_numeric_array(crps, "recent_crps")
            if crps.ndim == 0:
                crps = np.repeat(crps, len(horizons))
            if crps.shape != (len(horizons),):
                raise ValueError("recent_crps must have one value per horizon")
            if not np.all(np.isfinite(crps)) or np.any(crps < 0):
                raise ValueError("recent_crps must be finite and non-negative")

        members = _immutable_array(members)
        normalized_weights = _immutable_array(normalized_weights)
        if crps is not None:
            crps = _immutable_array(crps)

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "horizons_hours", horizons)
        object.__setattr__(self, "members_mm", members)
        object.__setattr__(self, "age_hours", age)
        object.__setattr__(self, "half_life_hours", half_life)
        object.__setattr__(self, "member_weights", normalized_weights)
        object.__setattr__(self, "recent_crps", crps)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))

    @property
    def freshness(self) -> float:
        """Exponential freshness multiplier with a source-specific half-life."""
        return float(0.5 ** (self.age_hours / self.half_life_hours))


@dataclass(frozen=True)
class ScenarioForecast:
    """A fused marginal distribution at every forecast horizon.

    Particle weights may differ by lead, so rows are not yet joint trajectories.
    """

    horizons_hours: Sequence[int]
    values_mm: Any
    particle_weights: Any
    source_weights: Mapping[str, Any]
    mixture_weight_ess: Any | None = None
    method: str = "evidence_weighted_marginals_v1"
    physical_projection: str = "none"
    warnings: Sequence[str] = ()

    def __post_init__(self) -> None:
        horizons = validate_horizons(self.horizons_hours)
        values = _as_2d(self.values_mm, "values_mm")
        weights = _as_2d(self.particle_weights, "particle_weights")
        if values.shape != weights.shape:
            raise ValueError("values_mm and particle_weights must have equal shape")
        if values.shape[1] != len(horizons):
            raise ValueError("particle horizon dimension does not match horizons_hours")
        if np.any(values < 0) or np.any(weights < 0):
            raise ValueError("forecast values and weights cannot be negative")
        if values.shape[1] > 1 and np.any(np.diff(values, axis=1) < 0):
            raise ValueError("fused cumulative rainfall cannot decrease with horizon")
        weights = _normalize_nonnegative_weights(weights, "particle_weights", axis=0)

        checked_sources: dict[str, np.ndarray] = {}
        for name, raw in self.source_weights.items():
            source_weight = _strict_numeric_array(raw, "source weights")
            if source_weight.shape != (len(horizons),):
                raise ValueError("each source weight needs one value per horizon")
            if not np.all(np.isfinite(source_weight)) or np.any(source_weight < 0):
                raise ValueError("source weights must be finite and non-negative")
            checked_sources[str(name)] = _immutable_array(source_weight)
        if not checked_sources:
            raise ValueError("at least one source weight is required")
        source_totals = np.sum(np.vstack(list(checked_sources.values())), axis=0)
        if not np.allclose(source_totals, 1.0, rtol=1e-9, atol=1e-12):
            raise ValueError("source weights must sum to one at every horizon")

        if self.mixture_weight_ess is None:
            mixture_ess = 1.0 / np.sum(np.square(weights), axis=0)
        else:
            mixture_ess = _strict_numeric_array(
                self.mixture_weight_ess, "mixture_weight_ess"
            )
            if mixture_ess.shape != (len(horizons),):
                raise ValueError("mixture_weight_ess must have one value per horizon")
            if not np.all(np.isfinite(mixture_ess)) or np.any(mixture_ess <= 0):
                raise ValueError("mixture_weight_ess must be finite and positive")

        values = _immutable_array(values)
        weights = _immutable_array(weights)
        mixture_ess = _immutable_array(mixture_ess)
        object.__setattr__(self, "horizons_hours", horizons)
        object.__setattr__(self, "values_mm", values)
        object.__setattr__(self, "particle_weights", weights)
        object.__setattr__(self, "source_weights", MappingProxyType(checked_sources))
        object.__setattr__(self, "mixture_weight_ess", mixture_ess)
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def quantiles(self, probabilities: Sequence[float]) -> np.ndarray:
        """Return an array with shape (quantiles, horizons)."""
        return np.column_stack(
            [
                weighted_quantile(
                    self.values_mm[:, index],
                    self.particle_weights[:, index],
                    probabilities,
                )
                for index in range(len(self.horizons_hours))
            ]
        )

    def probability_exceedance(self, threshold_mm: float) -> np.ndarray:
        threshold = float(threshold_mm)
        if not np.isfinite(threshold) or threshold < 0:
            raise ValueError("threshold_mm must be finite and non-negative")
        probabilities = np.sum(
            self.particle_weights * (self.values_mm >= threshold), axis=0
        )
        return np.clip(probabilities, 0.0, 1.0)

    def to_dict(
        self,
        quantiles: Sequence[float] = (0.1, 0.5, 0.9),
        thresholds_mm: Sequence[float] = (1.0, 10.0, 25.0, 50.0),
    ) -> dict[str, Any]:
        quantile_values = self.quantiles(quantiles)
        if len(set(float(value) for value in quantiles)) != len(quantiles):
            raise ValueError("quantiles must be unique")
        if len(set(float(value) for value in thresholds_mm)) != len(thresholds_mm):
            raise ValueError("thresholds_mm must be unique")
        return {
            "method": self.method,
            "target": {
                "variable": "precipitation_accumulation",
                "window": "forecast_issue_to_horizon",
                "units": "mm",
            },
            "physical_projection": self.physical_projection,
            "horizons_hours": list(self.horizons_hours),
            "quantiles_mm": [
                {
                    "probability": float(probability),
                    "values_mm": quantile_values[index].tolist(),
                }
                for index, probability in enumerate(quantiles)
            ],
            "probability_at_least": [
                {
                    "threshold_mm": float(threshold),
                    "probabilities": self.probability_exceedance(threshold).tolist(),
                }
                for threshold in thresholds_mm
            ],
            "source_weights": {
                name: values.tolist() for name, values in self.source_weights.items()
            },
            "mixture_weight_ess": self.mixture_weight_ess.tolist(),
            "warnings": list(self.warnings),
        }
