"""Synthetic rainfall fixtures test mechanics, not weather prediction skill."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

import numpy as np
import pytest

from ordcast.archive import ArchiveStore
from ordcast.gefs_baseline import raw_gefs_forecast
from ordcast.live_sources import GEFS_DEFAULT_MEMBERS
from ordcast.metrics import ensemble_crps

CYCLE = datetime(2026, 8, 13, tzinfo=timezone.utc)
AS_OF = CYCLE + timedelta(hours=8)


def populate(tmp_path, *, missing=None, future=None, windows=((0, 6, 2.), (0, 12, 5.), (12, 18, 1.), (18, 24, 4.)), grib=False):
    store = ArchiveStore(tmp_path)
    for member in GEFS_DEFAULT_MEMBERS:
        for start, end, rain in windows:
            if (member, end) == missing:
                continue
            payload = json.dumps([start, end, rain, -15.75, 128.75]).encode()
            if grib:
                ec = pytest.importorskip('eccodes')
                h = ec.codes_grib_new_from_samples('regular_ll_sfc_grib2')
                try:
                    for key, value in {'centre': 'kwbc', 'tablesVersion': 2, 'discipline': 0, 'Ni': 2, 'Nj': 2, 'latitudeOfFirstGridPointInDegrees': -15.75,
                                       'latitudeOfLastGridPointInDegrees': -16.0,
                                       'longitudeOfFirstGridPointInDegrees': 128.5,
                                       'longitudeOfLastGridPointInDegrees': 128.75,
                                       'iDirectionIncrementInDegrees': .25,
                                       'jDirectionIncrementInDegrees': .25,
                                       'productDefinitionTemplateNumber': 11,
                                       'dataDate': 20260813, 'dataTime': 0,
                                       'parameterCategory': 1, 'parameterNumber': 8,
                                       'typeOfFirstFixedSurface': 1,
                                       'perturbationNumber': int(member[1:]),
                                       'stepType': 'accum', 'startStep': start, 'endStep': end}.items():
                        ec.codes_set(h, key, value)
                    ec.codes_set_values(h, [rain] * 4)
                    payload = ec.codes_get_message(h)
                finally:
                    ec.codes_release(h)
            receipt = AS_OF + timedelta(hours=1) if (member, end) == future else AS_OF
            store.record(source='noaa-gefs-0p25', product='ord-regional-subset',
                         record_id=f'{member}-{end}', payload=payload,
                         source_issued_at=CYCLE, valid_at=CYCLE+timedelta(hours=end),
                         available_at=receipt, retrieved_at=receipt,
                         source_url='https://example.org/fixture', content_type='application/x-grib2',
                         metadata={'member': member, 'forecast_hour': end})
    return store


def decoded(payload, *args):
    return json.loads(payload)


def forecast(store, **kwargs):
    return raw_gefs_forecast(store, cycle=CYCLE, as_of=AS_OF, decoder=decoded, **kwargs)


def test_mixed_windows_and_existing_crps(tmp_path):
    source = forecast(populate(tmp_path))
    np.testing.assert_equal(source.members_mm, np.tile([2., 5., 10.], (31, 1)))
    assert len(source.metadata['inputs']) == 124
    # Toy zero-rain benchmark demonstrates metrics only; this is not validation.
    assert ensemble_crps(source.members_mm[:, 2], 9.) == pytest.approx(1.)
    assert ensemble_crps([0.], 9.) == 9.
    assert source.metadata['status'] == 'experimental_unvalidated'


@pytest.mark.parametrize('option', ['missing', 'future'])
def test_incomplete_or_unavailable_member_fails(tmp_path, option):
    with pytest.raises(ValueError, match='missing required horizon'):
        forecast(populate(tmp_path, **{option: ('p30', 24)}))


@pytest.mark.parametrize('windows,match', [
    (((6, 12, 2.),), 'gap'),
    (((0, 6, 5.), (0, 12, 2.)), 'inconsistent'),
    (((0, 6, -1.),), 'invalid accumulation'),
])
def test_invalid_windows(tmp_path, windows, match):
    with pytest.raises(ValueError, match=match):
        forecast(populate(tmp_path, windows=windows))


def test_real_grib_decoder_and_cli(tmp_path):
    store = populate(tmp_path, grib=True)
    source = raw_gefs_forecast(store, cycle=CYCLE, as_of=AS_OF)
    np.testing.assert_allclose(source.members_mm[0], [2, 5, 10])
    command = [sys.executable, '-m', 'pipelines.baseline.gefs_shadow', '--archive', str(tmp_path),
               '--cycle', CYCLE.isoformat(), '--as-of', AS_OF.isoformat()]
    first = subprocess.check_output(command)
    assert first == subprocess.check_output(command)
    result = json.loads(first)
    assert result['method'] == 'raw_gefs_shadow_v1'
    assert len(result['members_mm']) == 31
    assert result['warnings']
