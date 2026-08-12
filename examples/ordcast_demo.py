"""Small deterministic demonstration of the OrdCast marginal-fusion contract."""

from __future__ import annotations

import json

from ordcast import EvidenceWeightedScenarioEngine, SourceForecast


def main() -> None:
    horizons = (1, 3, 6, 24)
    sources = [
        SourceForecast(
            name="satellite_radar",
            horizons_hours=horizons,
            members_mm=[
                [0.0, 2.0, 8.0, 18.0],
                [0.5, 4.0, 12.0, 30.0],
                [1.0, 8.0, 20.0, 45.0],
            ],
            recent_crps=[0.8, 1.8, 5.0, 12.0],
            age_hours=0.2,
            half_life_hours=1.5,
        ),
        SourceForecast(
            name="global_ensemble",
            horizons_hours=horizons,
            members_mm=[
                [0.0, 1.0, 5.0, 12.0],
                [0.0, 3.0, 10.0, 25.0],
                [0.5, 6.0, 18.0, 55.0],
                [2.0, 12.0, 28.0, 80.0],
            ],
            recent_crps=[2.2, 2.5, 3.8, 8.0],
            age_hours=2.0,
            half_life_hours=12.0,
        ),
        SourceForecast(
            name="local_analogs",
            horizons_hours=horizons,
            members_mm=[
                [0.0, 0.0, 4.0, 20.0],
                [0.0, 5.0, 15.0, 38.0],
                [2.0, 10.0, 24.0, 70.0],
            ],
            recent_crps=[2.8, 3.0, 4.0, 9.0],
            half_life_hours=1e12,
        ),
    ]
    forecast = EvidenceWeightedScenarioEngine().fuse(sources)
    print(json.dumps(forecast.to_dict(), indent=2))


if __name__ == "__main__":
    main()
