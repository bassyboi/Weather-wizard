import numpy as np

from ordcast.analog import AnalogLibrary
from ordcast.evaluation import HindcastCase, walk_forward_hindcast
from ordcast.types import SourceForecast


def test_analog_library_never_selects_current_or_future_cases():
    library = AnalogLibrary(
        issue_times=["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        features=[[0.0], [1.0], [2.0], [3.0]],
        outcomes_mm=[[0.0], [5.0], [20.0], [100.0]],
        feature_names=["moisture_state"],
        horizons_hours=[24],
    )
    source, selection = library.forecast(
        {"moisture_state": 3.0}, "2024-01-04", k=4, embargo_hours=0
    )
    assert np.all(selection.issue_times < np.datetime64("2024-01-04"))
    assert np.all(selection.outcome_available_at <= np.datetime64("2024-01-04"))
    assert 3 not in selection.indices
    assert source.members_mm.max() == 20.0


def test_analog_library_excludes_targets_not_yet_observed():
    library = AnalogLibrary(
        issue_times=["2024-01-01T00:00", "2024-01-04T00:00"],
        features=[[0.0], [9.0]],
        outcomes_mm=[[1.0], [99.0]],
        feature_names=["state"],
        horizons_hours=[24],
    )
    source, selection = library.forecast({"state": 9.0}, "2024-01-04T12:00", k=2)
    assert selection.indices.tolist() == [0]
    assert source.members_mm[:, 0].tolist() == [1.0]


def test_walk_forward_weights_change_only_after_outcome_matures():
    horizons = (24,)

    def case(observed):
        return HindcastCase(
            issue_time="2024-01-01",
            sources=[
                SourceForecast("accurate", horizons, [[9], [10], [11]]),
                SourceForecast("poor", horizons, [[40], [50], [60]]),
            ],
            observations_mm=[observed],
        )

    cases = [case(10), case(10), case(10)]
    cases[1] = HindcastCase("2024-01-02T01:00", cases[1].sources, [10])
    cases[2] = HindcastCase("2024-01-03T02:00", cases[2].sources, [10])
    result = walk_forward_hindcast(cases, learning_rate=1.0)
    assert result.issue_times.astype("datetime64[h]").tolist() == [
        np.datetime64("2024-01-01T00", "h"),
        np.datetime64("2024-01-02T01", "h"),
        np.datetime64("2024-01-03T02", "h"),
    ]
    assert result.source_weights["accurate"][0, 0] == 0.5
    assert result.source_weights["poor"][0, 0] == 0.5
    assert 0.5 < result.source_weights["accurate"][1, 0] < 0.8
    assert result.ledger_snapshot["accurate"]["observations"] == [2]


def test_walk_forward_does_not_reveal_unmatured_long_lead_targets():
    horizons = (24,)
    sources = [
        SourceForecast("accurate", horizons, [[10]]),
        SourceForecast("poor", horizons, [[90]]),
    ]
    result = walk_forward_hindcast(
        [
            HindcastCase("2024-01-01T00:00", sources, [10]),
            HindcastCase("2024-01-01T06:00", sources, [10]),
        ],
        learning_rate=1.0,
    )
    assert result.source_weights["accurate"][:, 0].tolist() == [0.5, 0.5]
    assert result.ledger_snapshot == {}


def test_walk_forward_learns_each_horizon_when_it_matures():
    horizons = (1, 24)
    sources = [
        SourceForecast("accurate", horizons, [[10, 10]]),
        SourceForecast("poor", horizons, [[90, 90]]),
    ]
    result = walk_forward_hindcast(
        [
            HindcastCase("2024-01-01T00:00", sources, [10, 10]),
            HindcastCase("2024-01-01T02:00", sources, [10, 10]),
        ],
        learning_rate=1.0,
    )
    assert 0.5 < result.source_weights["accurate"][1, 0] < 0.8
    assert result.source_weights["accurate"][1, 1] == 0.5
    assert result.ledger_snapshot["accurate"]["observations"] == [1, 0]


def test_walk_forward_rejects_invalid_truth_and_injected_skill():
    horizons = (1,)
    clean = SourceForecast("clean", horizons, [[0]])
    bad_truth = HindcastCase("2024-01-01", [clean], [-1])
    try:
        walk_forward_hindcast([bad_truth])
        raise AssertionError("negative truth should be rejected")
    except ValueError as error:
        assert "non-negative" in str(error)

    injected = SourceForecast("injected", horizons, [[0]], recent_crps=[0])
    try:
        walk_forward_hindcast([HindcastCase("2024-01-01", [injected], [0])])
        raise AssertionError("caller-provided skill should be rejected")
    except ValueError as error:
        assert "cannot supply recent_crps" in str(error)


def test_walk_forward_requires_aligned_source_sets():
    a = SourceForecast("a", (1,), [[0]])
    b = SourceForecast("b", (1,), [[0]])
    cases = [
        HindcastCase("2024-01-01", [a, b], [0]),
        HindcastCase("2024-01-02", [a], [0]),
    ]
    try:
        walk_forward_hindcast(cases)
        raise AssertionError("unaligned source sets should be rejected")
    except ValueError as error:
        assert "same sources" in str(error)


def test_walk_forward_rejects_decreasing_cumulative_truth():
    source = SourceForecast("source", (1, 6), [[0, 10]])
    case = HindcastCase("2024-01-01", [source], [10, 0])
    try:
        walk_forward_hindcast([case])
        raise AssertionError("decreasing cumulative truth should be rejected")
    except ValueError as error:
        assert "cannot decrease" in str(error)
