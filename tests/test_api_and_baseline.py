import numpy as np
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from pipelines.nowcast.persistence_nowcast import dbz_to_rainrate, make_demo_cube
from serve.api_fastapi import (
    ScenarioForecastRequest,
    SourceScenarioRequest,
    nowcast_meta,
    scenario_forecast,
)


def test_zero_dbz_demo_background_stays_dry():
    rain = dbz_to_rainrate(np.array([0.0, 5.0, 30.0]))
    assert rain[0] == 0.0
    assert rain[1] > 0.0
    assert rain[2] > rain[1]
    assert make_demo_cube().dims == ("time", "y", "x")


def test_nonfinite_reflectivity_stays_missing_not_dry():
    rain = dbz_to_rainrate(np.array([np.nan, -np.inf, np.inf, 30.0]))
    assert np.isnan(rain[:3]).all()
    assert rain[3] > 0


def test_scenario_api_contract():
    request = ScenarioForecastRequest(
        horizons_hours=[1, 6],
        sources=[
            SourceScenarioRequest(
                name="radar", members_mm=[[0, 3], [4, 12]], recent_crps=[1, 3]
            ),
            SourceScenarioRequest(
                name="nwp", members_mm=[[0, 1], [8, 20]], recent_crps=[4, 2]
            ),
        ],
        thresholds_mm=[1, 10],
    )
    response = scenario_forecast(request)
    assert response["method"] == "evidence_weighted_marginals_v1"
    assert response["target"]["window"] == "forecast_issue_to_horizon"
    assert response["horizons_hours"] == [1, 6]
    assert [row["probability"] for row in response["quantiles_mm"]] == [0.1, 0.5, 0.9]
    assert set(response["source_weights"]) == {"radar", "nwp"}
    assert [row["threshold_mm"] for row in response["probability_at_least"]] == [1, 10]


def test_api_schema_caps_work_and_rejects_nonfinite_values():
    with pytest.raises(ValidationError):
        ScenarioForecastRequest(
            horizons_hours=list(range(1, 66)),
            sources=[SourceScenarioRequest(name="x", members_mm=[[0]])],
        )
    with pytest.raises(ValidationError):
        ScenarioForecastRequest(
            horizons_hours=[1],
            sources=[SourceScenarioRequest(name="x", members_mm=[[np.nan]])],
        )


def test_nowcast_metadata_rejects_path_traversal():
    with pytest.raises(HTTPException) as error:
        nowcast_meta("../requirements.txt")
    assert error.value.status_code == 400
