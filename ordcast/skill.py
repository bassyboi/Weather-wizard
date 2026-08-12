"""A horizon-specific skill ledger that only learns after observations arrive."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from ordcast.metrics import ensemble_crps
from ordcast.types import SourceForecast, _strict_numeric_array, validate_horizons


@dataclass
class _SkillState:
    ewma_crps: np.ndarray
    observations: np.ndarray


class SkillLedger:
    """Track recent source skill separately at each horizon.

    Forecasts must be issued before :meth:`record` is called. That ordering makes
    this suitable for prequential (walk-forward) evaluation without future data
    leaking into a past forecast.
    """

    def __init__(
        self,
        horizons_hours: Sequence[int],
        learning_rate: float = 0.2,
        prior_observations: float = 5.0,
    ):
        self.horizons_hours = validate_horizons(horizons_hours)
        rate = float(learning_rate)
        if not np.isfinite(rate) or not 0 < rate <= 1:
            raise ValueError("learning_rate must be in (0, 1]")
        self.learning_rate = rate
        prior = float(prior_observations)
        if not np.isfinite(prior) or prior < 0:
            raise ValueError("prior_observations must be finite and non-negative")
        self.prior_observations = prior
        self._states: dict[str, _SkillState] = {}

    def record(
        self,
        source: SourceForecast,
        observations_mm: Any,
        available_mask: Any | None = None,
    ) -> np.ndarray:
        """Score only newly available horizons and update their EWMAs."""
        if tuple(source.horizons_hours) != self.horizons_hours:
            raise ValueError("source horizons do not match this ledger")
        observations = _strict_numeric_array(observations_mm, "observations_mm")
        if observations.shape != (len(self.horizons_hours),):
            raise ValueError("observations_mm must have one value per horizon")
        if not np.all(np.isfinite(observations)) or np.any(observations < 0):
            raise ValueError("observations_mm must be finite and non-negative")
        if observations.size > 1 and np.any(np.diff(observations) < 0):
            raise ValueError("cumulative observations_mm cannot decrease")

        if available_mask is None:
            mask = np.ones(len(self.horizons_hours), dtype=bool)
        else:
            mask = np.asarray(available_mask, dtype=bool)
            if mask.shape != (len(self.horizons_hours),):
                raise ValueError("available_mask must have one value per horizon")
        if not np.any(mask):
            return np.full(len(self.horizons_hours), np.nan, dtype=np.float64)

        scores = np.full(len(self.horizons_hours), np.nan, dtype=np.float64)
        for index in np.flatnonzero(mask):
            scores[index] = ensemble_crps(
                source.members_mm[:, index],
                observations[index],
                source.member_weights[:, index],
            )
        state = self._states.get(source.name)
        if state is None:
            state = _SkillState(
                ewma_crps=np.full(scores.shape, np.nan, dtype=np.float64),
                observations=np.zeros(scores.shape, dtype=np.int64),
            )
            self._states[source.name] = state

        first = mask & (state.observations == 0)
        update = mask & (state.observations > 0)
        state.ewma_crps[first] = scores[first]
        state.ewma_crps[update] = (
            (1.0 - self.learning_rate) * state.ewma_crps[update]
            + self.learning_rate * scores[update]
        )
        state.observations[mask] += 1
        return scores

    def losses_for(self, sources: Sequence[SourceForecast]) -> np.ndarray:
        """Return source-by-horizon CRPS, using neutral priors when unscored."""
        if not sources:
            raise ValueError("at least one source is required")
        n_horizons = len(self.horizons_hours)
        losses = np.full((len(sources), n_horizons), np.nan, dtype=np.float64)

        for row, source in enumerate(sources):
            if tuple(source.horizons_hours) != self.horizons_hours:
                raise ValueError("source horizons do not match this ledger")
            if source.recent_crps is not None:
                losses[row] = source.recent_crps
                continue
            state = self._states.get(source.name)
            if state is not None:
                losses[row] = state.ewma_crps

        for column in range(n_horizons):
            known = losses[:, column][np.isfinite(losses[:, column])]
            neutral = float(np.median(known)) if known.size else 1.0
            for row, source in enumerate(sources):
                state = self._states.get(source.name)
                if (
                    source.recent_crps is None
                    and state is not None
                    and state.observations[column] > 0
                    and self.prior_observations > 0
                ):
                    count = float(state.observations[column])
                    losses[row, column] = (
                        count * losses[row, column]
                        + self.prior_observations * neutral
                    ) / (count + self.prior_observations)
            losses[~np.isfinite(losses[:, column]), column] = neutral
        return losses

    def snapshot(self) -> dict[str, dict[str, list[float | None] | list[int]]]:
        return {
            name: {
                "ewma_crps": [
                    float(value) if np.isfinite(value) else None
                    for value in state.ewma_crps
                ],
                "observations": state.observations.tolist(),
            }
            for name, state in sorted(self._states.items())
        }


def source_weights_from_losses(
    losses: Any,
    freshness: Any,
    temperature: float = 0.75,
    minimum_weight: float = 0.02,
) -> np.ndarray:
    """Turn comparable source losses into stable horizon-specific weights."""
    loss_array = _strict_numeric_array(losses, "losses")
    fresh_array = _strict_numeric_array(freshness, "freshness")
    if loss_array.ndim != 2:
        raise ValueError("losses must have shape (sources, horizons)")
    if fresh_array.shape != (loss_array.shape[0],):
        raise ValueError("freshness must have one value per source")
    if not np.all(np.isfinite(loss_array)) or np.any(loss_array < 0):
        raise ValueError("losses must be finite and non-negative")
    if not np.all(np.isfinite(fresh_array)) or np.any(fresh_array < 0):
        raise ValueError("freshness must be finite and non-negative")

    temp = float(temperature)
    floor = float(minimum_weight)
    if not np.isfinite(temp) or temp <= 0:
        raise ValueError("temperature must be positive")
    if not np.isfinite(floor) or floor < 0 or floor * loss_array.shape[0] >= 1:
        raise ValueError("minimum_weight is too large for the number of sources")

    scales = np.median(loss_array, axis=0)
    scales = np.maximum(scales, 1e-6)
    logits = -(loss_array / scales[None, :]) / temp
    logits -= np.max(logits, axis=0, keepdims=True)
    raw = np.exp(logits) * fresh_array[:, None]
    empty = raw.sum(axis=0) <= 0
    raw[:, empty] = 1.0
    normalized = raw / raw.sum(axis=0, keepdims=True)
    return floor + (1.0 - floor * loss_array.shape[0]) * normalized
