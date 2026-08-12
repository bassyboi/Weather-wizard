import numpy as np
import pytest

from ordcast.fusion import EvidenceWeightedScenarioEngine
from ordcast.metrics import brier_score, ensemble_crps
from ordcast.types import SourceForecast, weighted_quantile
from ordcast.types import ScenarioForecast


def test_weighted_quantile_inverts_the_empirical_cdf():
    result = weighted_quantile(
        np.array([0.0, 10.0]), np.array([0.5, 0.5]), [0.1, 0.5, 0.9]
    )
    assert result.tolist() == pytest.approx([0.0, 0.0, 10.0])


def test_weighted_quantile_ignores_zero_mass_particles():
    result = weighted_quantile(
        np.array([0.0, 10.0, 20.0]), np.array([0.0, 1.0, 0.0]), [0.1, 0.5, 0.9]
    )
    assert result.tolist() == pytest.approx([10.0, 10.0, 10.0])


def test_weighted_quantile_rejects_nonfinite_probability():
    with pytest.raises(ValueError, match="finite"):
        weighted_quantile(np.array([0.0]), np.array([1.0]), [np.nan])


def test_lower_crps_source_earns_more_weight():
    horizons = (1, 6)
    strong = SourceForecast(
        "strong", horizons, [[5, 10], [7, 14]], recent_crps=[1, 2]
    )
    weak = SourceForecast(
        "weak", horizons, [[0, 1], [30, 50]], recent_crps=[8, 12]
    )
    forecast = EvidenceWeightedScenarioEngine().fuse([strong, weak])
    assert np.all(forecast.source_weights["strong"] > forecast.source_weights["weak"])
    assert np.allclose(
        forecast.source_weights["strong"] + forecast.source_weights["weak"], 1.0
    )


def test_stale_source_loses_weight_even_with_equal_skill():
    horizons = (1,)
    fresh = SourceForecast(
        "fresh", horizons, [[5], [10]], recent_crps=[2], half_life_hours=1
    )
    stale = SourceForecast(
        "stale",
        horizons,
        [[5], [10]],
        recent_crps=[2],
        age_hours=4,
        half_life_hours=1,
    )
    forecast = EvidenceWeightedScenarioEngine().fuse([fresh, stale])
    assert forecast.source_weights["fresh"][0] > 0.8


def test_distribution_outputs_are_probabilistic_and_bounded():
    source = SourceForecast("only", (1, 3), [[0, 2], [10, 20], [30, 50]])
    forecast = EvidenceWeightedScenarioEngine().fuse([source])
    probabilities = forecast.probability_exceedance(10)
    quantiles = forecast.quantiles([0.1, 0.5, 0.9])
    assert np.all((probabilities >= 0) & (probabilities <= 1))
    assert np.all(quantiles[0] <= quantiles[1])
    assert np.all(quantiles[1] <= quantiles[2])
    assert "single-source" in forecast.warnings[0]


def test_probabilistic_metrics_known_cases():
    assert ensemble_crps([5.0], 5.0) == pytest.approx(0.0)
    assert ensemble_crps([0.0, 10.0], 5.0) == pytest.approx(2.5)
    assert brier_score(0.8, True) == pytest.approx(0.04)


def test_negative_rainfall_is_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        SourceForecast("bad", (1,), [[-1]])


def test_cumulative_rainfall_cannot_decrease():
    with pytest.raises(ValueError, match="cannot decrease"):
        SourceForecast("bad", (1, 6, 24), [[20, 0, 5]])


def test_fusion_projects_cumulative_marginals_to_physical_order():
    horizons = (1, 6)
    wet_early = SourceForecast(
        "wet_early", horizons, [[10, 10]], recent_crps=[0, 100]
    )
    dry_late = SourceForecast(
        "dry_late", horizons, [[0, 0]], recent_crps=[100, 0]
    )
    forecast = EvidenceWeightedScenarioEngine().fuse([wet_early, dry_late])
    assert np.all(np.diff(forecast.values_mm, axis=1) >= 0)
    assert np.all(np.diff(forecast.quantiles([0.1, 0.5, 0.9]), axis=1) >= 0)
    assert np.all(np.diff(forecast.probability_exceedance(5)) >= 0)


def test_horizons_reject_fractional_and_boolean_values():
    with pytest.raises(ValueError, match="integral"):
        SourceForecast("bad", (1.9,), [[0]])
    with pytest.raises(ValueError, match="integral"):
        SourceForecast("bad", (True,), [[0]])


def test_validated_forecast_arrays_are_immutable_snapshots():
    raw = np.array([[1.0, 2.0]])
    source = SourceForecast("safe", (1, 2), raw)
    raw[0, 1] = -99
    assert source.members_mm[0, 1] == 2.0
    with pytest.raises(ValueError):
        source.members_mm[0, 0] = 0.0
    with pytest.raises(ValueError):
        source.member_weights[0, 0] = 0.0
    with pytest.raises(ValueError):
        source.members_mm.setflags(write=True)


def test_scenario_rejects_invalid_source_weights():
    with pytest.raises(ValueError, match="finite and non-negative"):
        ScenarioForecast(
            (1,), [[1.0]], [[1.0]], {"bad": np.array([np.nan])}
        )
    with pytest.raises(ValueError, match="sum to one"):
        ScenarioForecast(
            (1,), [[1.0]], [[1.0]], {"a": np.array([0.4]), "b": np.array([0.4])}
        )


def test_serialization_preserves_close_quantiles_and_thresholds():
    forecast = EvidenceWeightedScenarioEngine().fuse(
        [SourceForecast("only", (1,), [[0.0], [10.0]])]
    )
    payload = forecast.to_dict(
        quantiles=[0.101, 0.104], thresholds_mm=[1.0000001, 1.0000002]
    )
    assert [row["probability"] for row in payload["quantiles_mm"]] == [0.101, 0.104]
    assert [row["threshold_mm"] for row in payload["probability_at_least"]] == [
        1.0000001,
        1.0000002,
    ]


def test_huge_finite_weights_normalize_without_overflow():
    source = SourceForecast(
        "large", (1,), [[0.0], [10.0]], member_weights=[1e308, 1e308]
    )
    assert source.member_weights[:, 0].tolist() == pytest.approx([0.5, 0.5])
    assert weighted_quantile([0.0, 10.0], [1e308, 1e308], [0.5])[0] == 0.0
    assert ensemble_crps([0.0, 10.0], 5.0, [1e308, 1e308]) == pytest.approx(2.5)


def test_core_rejects_coerced_numeric_members_and_tiny_decreases():
    with pytest.raises(ValueError, match="booleans or strings"):
        SourceForecast("bad", (1,), [["1"]])
    with pytest.raises(ValueError, match="booleans or strings"):
        SourceForecast("bad", (1,), [[True]])
    with pytest.raises(ValueError, match="cannot decrease"):
        SourceForecast("bad", (1, 2), [[1.0, 1.0 - 5e-10]])
