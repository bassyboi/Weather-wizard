"""Run the first real-data OrdCast baseline from an archived SILO CSV."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from ordcast.daily_baseline import (
    SILO_CUTOFF_UTC_HOUR,
    build_silo_daily_analog_library,
    daily_feature_vector,
)
from ordcast.fusion import EvidenceWeightedScenarioEngine
from pipelines.ingest.silo_daily import parse_silo_rain_csv


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forecast 1/3/7-day cumulative rain from past SILO analogs."
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument(
        "--issue-date",
        default=None,
        help="09:00 AWST issue; only final snapshot date + 1 is supported",
    )
    parser.add_argument("--analogs", type=int, default=40)
    args = parser.parse_args()

    if args.analogs <= 0:
        parser.error("--analogs must be positive")

    series = parse_silo_rain_csv(
        args.csv.read_text(encoding="utf-8"), expected_station=2056
    )
    default_issue = series.dates[-1] + np.timedelta64(1, "D")
    issue_date = np.datetime64(args.issue_date, "D") if args.issue_date else default_issue
    if issue_date != default_issue:
        parser.error(
            "historical --issue-date requires an archived snapshot vintage; "
            "this command supports only final snapshot date + 1"
        )
    library = build_silo_daily_analog_library(series)
    query = daily_feature_vector(series, issue_date)
    source, selection = library.forecast(
        query,
        issue_date.astype("datetime64[ns]")
        + np.timedelta64(SILO_CUTOFF_UTC_HOUR, "h"),
        k=args.analogs,
        source_name="silo_daily_analogs",
    )
    result = EvidenceWeightedScenarioEngine().fuse([source]).to_dict()
    result["baseline"] = {
        "name": "SILO seasonal-and-antecedent analog",
        "station": series.station,
        "issue_cutoff": f"{issue_date}T09:00:00+08:00",
        "target_windows": "following 1/3/7 completed 09:00-to-09:00 AWST periods",
        "selected_analog_dates": [str(value.astype("datetime64[D]")) for value in selection.issue_times],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
