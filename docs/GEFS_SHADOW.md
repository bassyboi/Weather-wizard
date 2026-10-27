# Raw GEFS rainfall shadow baseline

This is an offline, experimental, **unvalidated** baseline, not a trained OrdCast
model or an operational forecast. It does not change the API, local recorder,
Windows scheduler, analog selection or fusion weights. It uses CPU ecCodes only;
no claim is made about the owner's physical GPU or Windows execution.

## Reproduce from an existing local archive

Install the optional decoder in the same Python environment as OrdCast:

```powershell
python -m pip install -r requirements-gefs.txt
python -m pipelines.baseline.gefs_shadow --archive C:/OrdCast/data/live-archive --cycle 2026-10-27T00:00:00+00:00 --as-of 2026-10-27T08:00:00+00:00 --horizons 6 12 24 > gefs-shadow.json
```

The timestamps above are an example, not a claim that these inputs exist. Choose
a cycle actually collected by your recorder. The command needs the existing
`records/noaa-gefs-0p25/ord-regional-subset` manifests and referenced
`blobs/sha256` bytes. Raw archive files are never modified. JSON output is
repeatable for identical inputs, cutoff, options and decoder version. Preserve
that environment (including ecCodes library version) with any scientific result.

The output retains all 31 equally weighted control/perturbed trajectories,
empirical quantiles, rainfall exceedance fractions, source checksums and receipt
times. These fractions are uncalibrated ensemble frequencies. The point defaults
to Kununurra Aero (-15.7781, 128.7075); the function accepts other coordinates.
The chosen native grid point is reported. A grid-box forecast is not a point
rain-gauge measurement, especially during local convective storms.

## Time and accumulation contract

`SourceForecast` cumulative totals start at the **model cycle**, with horizon
end = cycle + lead. The JSON target's forecast issue is this model reference time,
not the later collection time. `as_of` is a separate availability cutoff. At an
08 UTC receipt of a 00 UTC cycle, the six-hour target has already ended: it is a
hindcast diagnostic, not a six-hour-ahead prediction. Consumers must use the
reported cycle and valid windows; this offline command does not rebase the model.

The decoder selects surface total precipitation (GRIB2 0/1/8) and reads each
message's startStep/endStep, cycle, member and valid time. Supported amounts are
kg m^-2 (= mm liquid water) or mm, in hourly step units. Totals are constructed
as total[end] = total[start] + amount, accepting cumulative and chained interval
windows without double counting. Unsupported units, missing data, gaps,
inconsistent totals, changed grid points, duplicates and incomplete member sets
fail closed. No missing member is imputed or silently renormalized. Lead zero is
ignored. Scope is deliberately rainfall only, six-hour horizons through day ten;
no bias correction, downscaling, storm location or spray advice is generated.

## Evidence and remaining real inputs

No real GEFS archive, corresponding gauge history, or SILO snapshot was present
in the repository checkout inspected at commit e0ef4c6. The repository's archive
is intentionally excluded from Git. The optional AWS archive destination is
user-configured; no accessible remote archive location was supplied. Real
forecast skill has therefore **not** been measured.

Tests generate representative binary GRIB2 messages using ecCodes and pass them
through the archive, decoder and CLI. Additional fixtures check mixed windows,
missing members, future receipts and invalid precipitation. A toy observation
is scored with the existing `ensemble_crps` against a zero-rain benchmark solely
to exercise the scoring path. Toy scores are not validation results.

For real verification, supply:

1. At least one complete recorded cycle's manifests and blobs for all 31 members
   with windows covering the requested leads; preserve original receipt times.
2. Quality-controlled observed rainfall totals for the *exact same UTC windows*,
   with station, coordinates, units, missing-value flags and availability times.
   Bureau rain_trace is since 09:00 AWST, not an interval amount; do not sum it.
3. For comparison with the existing SILO analog, an archived station-2056 SILO
   snapshot vintage available at each historical issue and targets aligned with
   its 09:00-to-09:00 AWST convention. Current six-hour GEFS endpoints do not land
   on 01 UTC (09 AWST), so those daily windows cannot be equated or prorated.
   Obtain matching subdaily gauge totals and a matching-window climatology, or
   collect appropriate finer-time model inputs before comparing with SILO.

Then score paired cases by lead with the existing CRPS (and Brier scores at fixed
thresholds), against a past-only seasonal climatology on identical windows.
Report sample count, missing-case policy, gauge/grid mismatch, wet/dry-season
coverage and uncertainty; fit any correction on earlier cases and evaluate on
held-out later cycles. A single cycle can prove decoding, not forecast skill.
Use official forecasts and warnings for safety-critical decisions.
