import numpy as np
import pytest

from ordcast.fusion import EvidenceWeightedScenarioEngine
from ordcast.types import SourceForecast


def _mass_at_least(values, weights, threshold):
    values = np.asarray(values)
    weights = np.asarray(weights)
    return float(np.sum(weights[values >= threshold]) / np.sum(weights))


def test_single_horizon_keeps_every_empirical_tail_probability_exact():
    source = SourceForecast(
        "weighted",
        (6,),
        [[0.0], [3.0], [3.0], [17.0]],
        member_weights=[0.07, 0.11, 0.29, 0.53],
    )

    forecast = EvidenceWeightedScenarioEngine().fuse([source])

    for threshold in [0.0, 0.1, 3.0, 3.1, 17.0, 18.0]:
        expected = _mass_at_least(
            source.members_mm[:, 0], source.member_weights[:, 0], threshold
        )
        assert forecast.probability_exceedance(threshold)[0] == pytest.approx(
            expected, abs=1e-15
        )


def test_projection_preserves_marginals_that_are_already_stochastically_ordered():
    source = SourceForecast(
        "ordered",
        (1, 6, 24),
        [[0.0, 3.0, 6.0], [2.0, 5.0, 8.0], [9.0, 12.0, 20.0]],
        member_weights=[
            [0.10, 0.20, 0.25],
            [0.30, 0.20, 0.15],
            [0.60, 0.60, 0.60],
        ],
    )

    forecast = EvidenceWeightedScenarioEngine().fuse([source])

    for horizon_index in range(3):
        for threshold in [0.0, 0.5, 2.0, 4.0, 9.0, 12.0, 21.0]:
            expected = _mass_at_least(
                source.members_mm[:, horizon_index],
                source.member_weights[:, horizon_index],
                threshold,
            )
            assert forecast.probability_exceedance(threshold)[
                horizon_index
            ] == pytest.approx(expected, abs=1e-15)


def test_fusion_is_invariant_to_source_order_permutations():
    alpha = SourceForecast(
        "alpha",
        (1, 6),
        [[0.0, 4.0], [8.0, 12.0], [2.0, 9.0]],
        member_weights=[0.2, 0.5, 0.3],
        recent_crps=[1.0, 4.0],
    )
    beta = SourceForecast(
        "beta",
        (1, 6),
        [[1.0, 2.0], [7.0, 15.0]],
        member_weights=[0.65, 0.35],
        recent_crps=[3.0, 1.0],
    )
    first = EvidenceWeightedScenarioEngine().fuse([alpha, beta])
    second = EvidenceWeightedScenarioEngine().fuse([beta, alpha])

    np.testing.assert_array_equal(first.values_mm, second.values_mm)
    np.testing.assert_array_equal(first.particle_weights, second.particle_weights)
    assert tuple(first.source_weights) == tuple(second.source_weights)
    for name in first.source_weights:
        np.testing.assert_array_equal(first.source_weights[name], second.source_weights[name])


def test_reported_mixture_ess_describes_returned_projection_weights():
    early = SourceForecast(
        "early", (1, 6), [[0.0, 0.0], [10.0, 10.0]], recent_crps=[1.0, 9.0]
    )
    late = SourceForecast(
        "late", (1, 6), [[0.0, 0.0], [20.0, 20.0]], recent_crps=[9.0, 1.0]
    )

    forecast = EvidenceWeightedScenarioEngine().fuse([early, late])
    output_ess = 1.0 / np.sum(np.square(forecast.particle_weights), axis=0)

    np.testing.assert_allclose(forecast.mixture_weight_ess, output_ess, rtol=0, atol=0)
    assert forecast.physical_projection == "exact_empirical_cdf_cumulative_max_v1"
