"""Target-maturity-safe daily Ord rainfall analog baseline.

SILO labels daily rain by the local date on which its 24-hour observation ends at
09:00. For a conservative present-day query, an ``issue_date`` uses only rows labelled
through the previous date. The first target is the total labelled on the following
date; the issue-date row is intentionally excluded from both features and targets.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from ordcast.analog import AnalogLibrary
from ordcast.types import validate_horizons
from pipelines.ingest.silo_daily import DailyRainSeries, KUNUNURRA_AERO_STATION

DAILY_FEATURE_NAMES = (
    "day_of_year_sin",
    "day_of_year_cos",
    "antecedent_rain_7d",
    "antecedent_rain_30d",
    "wet_days_14d",
    "max_rain_14d",
)
SILO_CUTOFF_UTC_HOUR = 1  # 09:00 Australia/Perth (UTC+08:00; no DST).


def _issue_index(series: DailyRainSeries, issue_date: object) -> int:
    issue = np.datetime64(issue_date, "D")
    if np.isnat(issue):
        raise ValueError("issue_date must be valid")
    return int(np.searchsorted(series.dates, issue, side="left"))


def daily_feature_vector(
    series: DailyRainSeries, issue_date: object
) -> dict[str, float]:
    """Build conservative features using rows through ``issue_date - 1``."""
    if series.station != KUNUNURRA_AERO_STATION:
        raise ValueError("SILO daily baseline supports Kununurra Aero station 2056 only")
    issue = np.datetime64(issue_date, "D")
    index = _issue_index(series, issue)
    if index < 30:
        raise ValueError("at least 30 antecedent days are required")
    if index == 0 or series.dates[index - 1] != issue - np.timedelta64(1, "D"):
        raise ValueError("SILO snapshot must extend continuously through issue_date - 1")

    year_start = issue.astype("datetime64[Y]").astype("datetime64[D]")
    day_of_year = int((issue - year_start).astype(int)) + 1
    angle = 2.0 * np.pi * (day_of_year - 1) / 365.2425
    previous_7 = series.rain_mm[index - 7 : index]
    previous_14 = series.rain_mm[index - 14 : index]
    previous_30 = series.rain_mm[index - 30 : index]
    return {
        "day_of_year_sin": float(np.sin(angle)),
        "day_of_year_cos": float(np.cos(angle)),
        "antecedent_rain_7d": float(previous_7.sum()),
        "antecedent_rain_30d": float(previous_30.sum()),
        "wet_days_14d": float(np.count_nonzero(previous_14 >= 0.2)),
        "max_rain_14d": float(previous_14.max()),
    }


def build_silo_daily_analog_library(
    series: DailyRainSeries,
    horizon_days: Sequence[int] = (1, 3, 7),
    *,
    publication_lag_days: int = 1,
) -> AnalogLibrary:
    """Create issue-time features and cumulative-rain targets from a SILO snapshot."""
    if series.station != KUNUNURRA_AERO_STATION:
        raise ValueError("SILO daily baseline supports Kununurra Aero station 2056 only")
    days = tuple(horizon_days)
    horizons = validate_horizons([day * 24 for day in days])
    if any(not isinstance(day, (int, np.integer)) or isinstance(day, (bool, np.bool_)) for day in days):
        raise ValueError("horizon_days must contain integers")
    if any(day <= 0 for day in days):
        raise ValueError("horizon_days must be positive")
    if not isinstance(publication_lag_days, (int, np.integer)) or isinstance(
        publication_lag_days, (bool, np.bool_)
    ):
        raise ValueError("publication_lag_days must be a non-negative integer")
    lag = int(publication_lag_days)
    if lag < 0:
        raise ValueError("publication_lag_days must be a non-negative integer")

    longest = max(days)
    # At the conservative 09:00 issue cutoff, features end at ``index - 1``.
    # The issue-date row is skipped and targets span ``index + 1`` through
    # ``index + longest``.
    case_indices = range(30, len(series.dates) - longest)
    features: list[list[float]] = []
    outcomes: list[list[float]] = []
    issue_times: list[np.datetime64] = []
    availability: list[np.datetime64] = []

    for index in case_indices:
        issue = series.dates[index]
        feature = daily_feature_vector(series, issue)
        cumulative = np.cumsum(series.rain_mm[index + 1 : index + longest + 1])
        features.append([feature[name] for name in DAILY_FEATURE_NAMES])
        outcomes.append([float(cumulative[day - 1]) for day in days])
        issue_at = (
            issue.astype("datetime64[ns]")
            + np.timedelta64(SILO_CUTOFF_UTC_HOUR, "h")
        )
        issue_times.append(issue_at)
        availability.append(
            issue_at + np.timedelta64(longest + lag, "D")
        )

    if not features:
        raise ValueError("SILO series is too short for the requested daily horizons")
    return AnalogLibrary(
        issue_times=issue_times,
        features=features,
        outcomes_mm=outcomes,
        feature_names=DAILY_FEATURE_NAMES,
        horizons_hours=horizons,
        outcome_available_at=availability,
    )
