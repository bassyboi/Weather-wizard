"""Maturity-aware walk-forward verification for the marginal-fusion engine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Sequence

import numpy as np

from ordcast.fusion import EvidenceWeightedScenarioEngine
from ordcast.metrics import ensemble_crps
from ordcast.skill import SkillLedger
from ordcast.types import (
    SourceForecast,
    _immutable_array,
    _strict_numeric_array,
)


@dataclass(frozen=True)
class HindcastCase:
    issue_time: Any
    sources: Sequence[SourceForecast]
    observations_mm: Any
    observations_available_at: Any | None = None


@dataclass(frozen=True)
class HindcastResult:
    fused_crps: np.ndarray
    source_weights: dict[str, np.ndarray]
    ledger_snapshot: dict[str, dict[str, list[float | None] | list[int]]]
    issue_times: np.ndarray | None = None

    def __post_init__(self) -> None:
        fused = _strict_numeric_array(self.fused_crps, "fused_crps")
        if fused.ndim != 2 or not np.all(np.isfinite(fused)) or np.any(fused < 0):
            raise ValueError("fused_crps must be a finite non-negative case-by-horizon array")
        checked_weights: dict[str, np.ndarray] = {}
        for name, values in self.source_weights.items():
            array = _strict_numeric_array(values, "source_weights")
            if array.shape != fused.shape or not np.all(np.isfinite(array)):
                raise ValueError("source_weights must align with fused_crps")
            checked_weights[str(name)] = _immutable_array(array)
        if self.issue_times is None:
            raise ValueError("issue_times must provide one valid time per case")
        issue_times = np.asarray(self.issue_times, dtype="datetime64[ns]")
        if issue_times.shape != (fused.shape[0],) or np.isnat(issue_times).any():
            raise ValueError("issue_times must provide one valid time per case")
        object.__setattr__(self, "fused_crps", _immutable_array(fused))
        object.__setattr__(
            self, "source_weights", MappingProxyType(checked_weights)
        )
        object.__setattr__(self, "ledger_snapshot", deepcopy(self.ledger_snapshot))
        object.__setattr__(self, "issue_times", _immutable_array(issue_times))

    @property
    def mean_crps_by_horizon(self) -> np.ndarray:
        return np.mean(self.fused_crps, axis=0)


def walk_forward_hindcast(
    cases: Sequence[HindcastCase],
    *,
    learning_rate: float = 0.2,
) -> HindcastResult:
    """Forecast each case first, then reveal it to the skill ledger."""
    if not cases or not cases[0].sources:
        raise ValueError("at least one non-empty hindcast case is required")
    horizons = tuple(cases[0].sources[0].horizons_hours)
    expected_sources = tuple(source.name for source in cases[0].sources)
    if len(expected_sources) != len(set(expected_sources)):
        raise ValueError("source names must be unique within every hindcast case")
    for case in cases:
        case_sources = tuple(source.name for source in case.sources)
        if case_sources != expected_sources:
            raise ValueError(
                "every hindcast case must contain the same sources in the same order"
            )
        if any(tuple(source.horizons_hours) != horizons for source in case.sources):
            raise ValueError("every hindcast source must use identical horizons")
    if any(
        source.recent_crps is not None
        for case in cases
        for source in case.sources
    ):
        raise ValueError(
            "walk-forward sources cannot supply recent_crps; the evaluator owns skill history"
        )
    ledger = SkillLedger(horizons, learning_rate=learning_rate)
    engine = EvidenceWeightedScenarioEngine(ledger)
    fused_rows: list[np.ndarray] = []
    recorded_weights: dict[str, list[np.ndarray]] = {}

    ordered_cases = sorted(cases, key=lambda case: np.datetime64(case.issue_time, "ns"))
    pending: list[
        tuple[np.datetime64, Sequence[SourceForecast], np.ndarray, int]
    ] = []
    previous_issue: np.datetime64 | None = None

    for case in ordered_cases:
        issue_time = np.datetime64(case.issue_time, "ns")
        if np.isnat(issue_time):
            raise ValueError("every case needs a valid issue_time")
        if previous_issue is not None and issue_time <= previous_issue:
            raise ValueError("hindcast issue times must be unique")
        previous_issue = issue_time

        matured = [entry for entry in pending if entry[0] <= issue_time]
        pending = [entry for entry in pending if entry[0] > issue_time]
        for _, past_sources, past_observations, horizon_index in sorted(
            matured, key=lambda item: item[0]
        ):
            mask = np.zeros(len(horizons), dtype=bool)
            mask[horizon_index] = True
            for past_source in past_sources:
                ledger.record(past_source, past_observations, available_mask=mask)

        observations = _strict_numeric_array(case.observations_mm, "observations_mm")
        if observations.shape != (len(horizons),):
            raise ValueError("each case needs one observation per horizon")
        if not np.all(np.isfinite(observations)) or np.any(observations < 0):
            raise ValueError("case observations must be finite and non-negative")
        if observations.size > 1 and np.any(np.diff(observations) < 0):
            raise ValueError("cumulative case observations cannot decrease")

        forecast = engine.fuse(case.sources)
        fused_rows.append(
            np.asarray(
                [
                    ensemble_crps(
                        forecast.values_mm[:, index],
                        observations[index],
                        forecast.particle_weights[:, index],
                    )
                    for index in range(len(horizons))
                ]
            )
        )
        for name, weights in forecast.source_weights.items():
            recorded_weights.setdefault(name, []).append(weights.copy())

        if case.observations_available_at is None:
            availability = np.asarray(
                [issue_time + np.timedelta64(horizon, "h") for horizon in horizons],
                dtype="datetime64[ns]",
            )
        else:
            raw_availability = np.asarray(case.observations_available_at)
            if raw_availability.ndim == 0:
                availability = np.repeat(
                    np.datetime64(case.observations_available_at, "ns"), len(horizons)
                )
            else:
                availability = np.asarray(
                    case.observations_available_at, dtype="datetime64[ns]"
                )
            if availability.shape != (len(horizons),) or np.isnat(availability).any():
                raise ValueError(
                    "observations_available_at must be one valid time or one per horizon"
                )
        minimum_availability = np.asarray(
            [issue_time + np.timedelta64(horizon, "h") for horizon in horizons],
            dtype="datetime64[ns]",
        )
        if np.any(availability < minimum_availability):
            raise ValueError(
                "an observation cannot be available before its horizon verifies"
            )
        for horizon_index, available_at in enumerate(availability):
            pending.append(
                (available_at, case.sources, observations.copy(), horizon_index)
            )

    return HindcastResult(
        fused_crps=np.vstack(fused_rows),
        source_weights={name: np.vstack(rows) for name, rows in recorded_weights.items()},
        ledger_snapshot=ledger.snapshot(),
        issue_times=np.asarray(
            [np.datetime64(case.issue_time, "ns") for case in ordered_cases]
        ),
    )
