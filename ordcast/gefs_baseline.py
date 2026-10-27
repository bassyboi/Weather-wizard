"""Offline, uncalibrated GEFS rainfall shadow baseline; never used by the API."""
from __future__ import annotations

from datetime import datetime, timedelta
import tempfile

import numpy as np

from ordcast.archive import ArchiveStore, _utc_datetime
from ordcast.live_sources import GEFS_DEFAULT_MEMBERS
from ordcast.types import SourceForecast, validate_horizons


def decode_precipitation(payload: bytes, record, latitude: float, longitude: float):
    """Decode surface accumulated precipitation at the nearest native grid point.

    ecCodes is an optional CPU dependency. Do not infer rainfall windows from file
    lead names: each GRIB message supplies its own startStep and endStep.
    """
    try:
        import eccodes as ec
    except ImportError as error:
        raise RuntimeError('Install requirements-gefs.txt to decode archived GRIB2') from error
    result = []
    with tempfile.TemporaryFile() as stream:
        stream.write(payload)
        stream.seek(0)
        while (handle := ec.codes_grib_new_from_file(stream)) is not None:
            try:
                get = lambda key: ec.codes_get(handle, key)
                if (get('discipline'), get('parameterCategory'), get('parameterNumber')) != (0, 1, 8):
                    continue
                if get('typeOfLevel') != 'surface' or get('stepType') != 'accum':
                    raise ValueError('precipitation must be a surface accumulation')
                if get('stepUnits') != 1 or get('units') not in ('kg m**-2', 'mm'):
                    raise ValueError('unsupported precipitation time or amount units')
                cycle = record.source_issued_at
                if int(get('dataDate')) != int(cycle.strftime('%Y%m%d')) or int(get('dataTime')) != int(cycle.strftime('%H%M')):
                    raise ValueError('GRIB cycle differs from archive manifest')
                member_number = int(record.metadata['member'][1:])
                if int(get('perturbationNumber')) != member_number:
                    raise ValueError('GRIB member differs from archive manifest')
                point = ec.codes_grib_find_nearest(handle, latitude, longitude)[0]
                if point['distance'] > 30 or int(get('numberOfMissing')):
                    raise ValueError('missing precipitation or requested point outside subset')
                start, end = int(get('startStep')), int(get('endStep'))
                if end != record.metadata['forecast_hour'] or record.valid_at != cycle + timedelta(hours=end):
                    raise ValueError('GRIB valid time differs from archive manifest')
                result.append((start, end, float(point['value']), float(point['lat']), float(point['lon'])))
            finally:
                ec.codes_release(handle)
    if len(result) != 1:
        raise ValueError('expected exactly one precipitation message per archived artifact')
    return result[0]


def raw_gefs_forecast(store: ArchiveStore, *, cycle: datetime, as_of: datetime,
                      horizons_hours=(6, 12, 24), latitude=-15.7781,
                      longitude=128.7075, decoder=decode_precipitation) -> SourceForecast:
    """Require all 31 members and exact, gap-free cumulative rainfall coverage.

    Targets start at model cycle, not receipt/issuance time. as_of only controls
    input availability, preventing use of future archive receipts in hindcasts.
    """
    cycle = _utc_datetime(cycle, 'cycle')
    as_of = _utc_datetime(as_of, 'as_of')
    horizons = validate_horizons(horizons_hours)
    if cycle > as_of or cycle.minute or cycle.second or cycle.microsecond or cycle.hour % 6:
        raise ValueError('cycle must be a 00/06/12/18 UTC cycle no later than as_of')
    if max(horizons) > 240 or any(h % 6 for h in horizons):
        raise ValueError('horizons must be six-hour multiples through 240')
    if not np.isfinite([latitude, longitude]).all() or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError('invalid latitude/longitude')
    rows = {m: {} for m in GEFS_DEFAULT_MEMBERS}
    provenance = []
    grid = None
    for record in store.iter_records(source='noaa-gefs-0p25', product='ord-regional-subset'):
        if record.source_issued_at != cycle or record.available_at > as_of:
            continue
        lead = record.metadata.get('forecast_hour')
        if not isinstance(lead, int) or isinstance(lead, bool) or lead < 0 or lead % 6:
            raise ValueError('invalid archive forecast_hour')
        if lead == 0 or lead > max(horizons):
            continue
        member = record.metadata.get('member')
        if member not in rows or lead in rows[member]:
            raise ValueError('unexpected member or duplicate member/lead record')
        start, end, rain, lat, lon = decoder(store.read_blob(record), record, latitude, longitude)
        if end != lead or not 0 <= start < end or not np.isfinite(rain) or rain < 0:
            raise ValueError('invalid accumulation window or precipitation')
        if grid is None:
            grid = (lat, lon)
        if grid != (lat, lon):
            raise ValueError('native grid point changed between inputs')
        rows[member][end] = (start, rain)
        provenance.append({'record_id': record.record_id, 'sha256': record.sha256,
                           'available_at': record.available_at.isoformat()})
    members = []
    for member, windows in rows.items():
        totals = {0: 0.0}
        for end, (start, rain) in sorted(windows.items()):
            if start not in totals:
                raise ValueError(f'{member}: gap before accumulation {start}-{end}')
            totals[end] = totals[start] + rain
            if totals[end] < totals[max(t for t in totals if t < end)]:
                raise ValueError('inconsistent overlapping accumulations')
        if any(h not in totals for h in horizons):
            raise ValueError(f'{member}: missing required horizon')
        members.append([totals[h] for h in horizons])
    return SourceForecast(name='raw_gefs_shadow', horizons_hours=horizons, members_mm=members,
                          age_hours=(as_of-cycle).total_seconds()/3600,
                          metadata={'status': 'experimental_unvalidated',
                                    'cycle_utc': cycle.isoformat(), 'as_of_utc': as_of.isoformat(),
                                    'target_start_utc': cycle.isoformat(),
                                    'requested_lat_lon': [latitude, longitude], 'native_lat_lon': grid,
                                    'spatial_method': 'nearest_native_grid_point',
                                    'inputs': provenance, 'calibration': 'none'})
