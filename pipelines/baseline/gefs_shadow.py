"""Reproduce one experimental GEFS forecast from the existing immutable archive."""
import argparse
from datetime import datetime
import json

from ordcast.archive import ArchiveStore
from ordcast.gefs_baseline import raw_gefs_forecast
from ordcast.types import ScenarioForecast


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--cycle', required=True, help='timezone-aware ISO timestamp')
    parser.add_argument('--as-of', required=True, help='historical input availability cutoff')
    parser.add_argument('--horizons', nargs='+', type=int, default=[6, 12, 24])
    args = parser.parse_args()
    source = raw_gefs_forecast(ArchiveStore(args.archive),
                              cycle=datetime.fromisoformat(args.cycle),
                              as_of=datetime.fromisoformat(args.as_of),
                              horizons_hours=args.horizons)
    scenario = ScenarioForecast(source.horizons_hours, source.members_mm,
                                source.member_weights,
                                {source.name: [1.] * len(source.horizons_hours)},
                                method='raw_gefs_shadow_v1',
                                warnings=['Experimental, uncalibrated and unvalidated; not operational.'])
    output = scenario.to_dict()
    output['provenance'] = dict(source.metadata)
    output['provenance']['inputs'] = [dict(item) for item in source.metadata['inputs']]
    output['members_mm'] = source.members_mm.tolist()
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
