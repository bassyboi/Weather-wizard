"""Evidence-weighted fusion of forecast marginals at each lead time."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from ordcast.skill import SkillLedger, source_weights_from_losses
from ordcast.types import ScenarioForecast, SourceForecast


def _project_cumulative_marginals(
    values: np.ndarray,
    weights: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Create stochastically ordered empirical cumulative-rain marginals.

    The union of every marginal CDF's mass breakpoints partitions ``(0, 1]`` into
    intervals on which every empirical inverse CDF is constant. Representing those
    intervals directly retains every input atom and its exact probability mass; it
    does not interpolate or resample quantiles. A cumulative maximum across leads is
    then the one-sided physical correction. Rows are an artificial rank coupling,
    not event trajectories.
    """
    if values.shape != weights.shape or values.ndim != 2:
        raise ValueError("values and weights must be equal two-dimensional arrays")

    sorted_marginals: list[tuple[np.ndarray, np.ndarray]] = []
    breakpoints: list[np.ndarray] = [np.array([0.0, 1.0])]
    for horizon_index in range(values.shape[1]):
        horizon_values = values[:, horizon_index]
        horizon_weights = weights[:, horizon_index]
        positive = horizon_weights > 0
        horizon_values = horizon_values[positive]
        horizon_weights = horizon_weights[positive]

        # A secondary weight key makes equal-valued atoms canonical under member and
        # source permutations. Keeping (rather than aggregating) their boundaries
        # also preserves the empirical mixture's particle-weight information.
        order = np.lexsort((horizon_weights, horizon_values))
        sorted_values = horizon_values[order]
        sorted_weights = horizon_weights[order]
        total = math.fsum(float(weight) for weight in sorted_weights)
        normalized_weights = sorted_weights / total
        cumulative_weights = np.cumsum(normalized_weights, dtype=np.float64)
        cumulative_weights[-1] = 1.0
        sorted_marginals.append((sorted_values, cumulative_weights))
        breakpoints.append(cumulative_weights)

    common_breakpoints = np.unique(np.concatenate(breakpoints))
    interval_weights = np.diff(common_breakpoints)
    positive_intervals = interval_weights > 0
    interval_weights = interval_weights[positive_intervals]
    interval_ends = common_breakpoints[1:][positive_intervals]

    # For Q(p) = inf{x: F(x) >= p}, the right endpoint represents the interval
    # (left, right] exactly, including intervals only one floating-point ULP wide.
    projected = np.column_stack(
        [
            sorted_values[
                np.searchsorted(cumulative_weights, interval_ends, side="left")
            ]
            for sorted_values, cumulative_weights in sorted_marginals
        ]
    )
    projected = np.maximum.accumulate(projected, axis=1)

    # Collapse adjacent intervals only when their complete coupled output is equal.
    # This is lossless and avoids inflating the reported output ESS with duplicate
    # representations of the same atom.
    starts = np.ones(projected.shape[0], dtype=bool)
    starts[1:] = np.any(projected[1:] != projected[:-1], axis=1)
    start_indices = np.flatnonzero(starts)
    projected = projected[start_indices]
    interval_weights = np.add.reduceat(interval_weights, start_indices)
    interval_weights /= math.fsum(float(weight) for weight in interval_weights)
    projected_weights = np.repeat(
        interval_weights[:, None], projected.shape[1], axis=1
    )
    return projected, projected_weights


class EvidenceWeightedScenarioEngine:
    """Blend lead-time marginals by earned local skill and freshness."""

    def __init__(
        self,
        ledger: SkillLedger | None = None,
        *,
        temperature: float = 0.75,
        minimum_source_weight: float = 0.02,
    ) -> None:
        self.ledger = ledger
        self.temperature = float(temperature)
        self.minimum_source_weight = float(minimum_source_weight)

    def fuse(self, sources: Sequence[SourceForecast]) -> ScenarioForecast:
        if not sources:
            raise ValueError("at least one source forecast is required")
        names = [source.name for source in sources]
        if len(names) != len(set(names)):
            raise ValueError("source names must be unique")
        # Canonical source ordering makes the complete fused representation stable,
        # not merely distributionally equivalent, when callers reorder sources.
        sources = tuple(sorted(sources, key=lambda source: source.name))

        horizons = tuple(sources[0].horizons_hours)
        if any(tuple(source.horizons_hours) != horizons for source in sources[1:]):
            raise ValueError("all source forecasts must use identical horizons")

        if self.ledger is None:
            ledger = SkillLedger(horizons)
        else:
            ledger = self.ledger
            if ledger.horizons_hours != horizons:
                raise ValueError("skill ledger and source horizons do not match")

        losses = ledger.losses_for(sources)
        freshness = np.asarray([source.freshness for source in sources])
        source_weight_matrix = source_weights_from_losses(
            losses,
            freshness,
            temperature=self.temperature,
            minimum_weight=self.minimum_source_weight,
        )

        values = np.concatenate([source.members_mm for source in sources], axis=0)
        particle_weights = np.concatenate(
            [
                source.member_weights * source_weight_matrix[index][None, :]
                for index, source in enumerate(sources)
            ],
            axis=0,
        )
        # Normalize with an order-independent accurate sum so permuting otherwise
        # identical input members cannot perturb downstream CDF breakpoints.
        for horizon_index in range(particle_weights.shape[1]):
            total = math.fsum(
                sorted(float(weight) for weight in particle_weights[:, horizon_index])
            )
            particle_weights[:, horizon_index] /= total
        values, particle_weights = _project_cumulative_marginals(
            values, particle_weights
        )

        warnings: list[str] = []
        if max(source.age_hours for source in sources) > 0 and np.max(freshness) < 0.25:
            warnings.append("all input sources are older than two of their configured half-lives")
        if len(sources) == 1:
            warnings.append("single-source forecast; disagreement uncertainty is unavailable")

        return ScenarioForecast(
            horizons_hours=horizons,
            values_mm=values,
            particle_weights=particle_weights,
            source_weights={
                source.name: source_weight_matrix[index]
                for index, source in enumerate(sources)
            },
            # Let ScenarioForecast calculate ESS from its normalized, post-projection
            # weights so the reported statistic always describes the returned rows.
            mixture_weight_ess=None,
            physical_projection="exact_empirical_cdf_cumulative_max_v1",
            warnings=warnings,
        )
