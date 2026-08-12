# OrdCast live recorder

This recorder creates the issued-data history that OrdCast needs before it can learn
what works around Kununurra. It does **not** make a forecast yet. Each run freezes an
explicit collection plan, validates exact upstream bytes, and stores them with issue,
valid, and receipt times in a content-addressed, append-only archive.

## Starter profile

Every six hours the default profile records:

- the official Kununurra Airport (`002056`, WMO `94216`) 72-hour observation JSON;
- the current complete GEFS cycle: control plus 30 perturbed members, every six-hour
  lead from 0 to 240 hours, and a small 125–132°E / 12–20°S Ord subset; and
- the preceding seven hours of Himawari-9 Band 13 at ten-minute cadence, downloading
  only native full-disk segment 7, which covers the Ord district.

The GEFS fields include precipitation, CAPE/CIN, helicity, precipitable water,
near-surface temperature/dew point/humidity/wind/gust, and mean-sea-level pressure.
GEFS precipitation fields have mixed accumulation windows; downstream parsing must
read GRIB `startStep` and `endStep` rather than assuming every value is a cumulative
or fixed-window total. Bureau `rain_trace` is a string holding rainfall since 09:00
WST—not an interval total—and may contain `"-"` for missing data.

ECMWF IFS is deliberately deferred from the automatic starter profile. Its open
client does not crop geographically before download, so even a rainfall-only ensemble
would add roughly 9–10 GB per day. It should be enabled as a separately budgeted
phase after the smaller archive is proven reliable.

## Run locally

For the supported automatic Windows deployment, use
[the local PC guide](LOCAL_PC.md). It stores every cycle in one persistent archive
root, prevents overlapping runs, wakes the PC on schedule, and provides a health
command.

The low-level two-command interface remains useful for development:

The two commands intentionally separate planning from network activity:

```bash
archive_root=data/live-archive/$(date -u +%Y%m%dT%H%M%SZ)
python -m pipelines.ingest.live_archive plan --output "$archive_root"
python -m pipelines.ingest.live_archive record --output "$archive_root"
```

For a small connectivity check:

```bash
archive_root=$(mktemp -d)
python -m pipelines.ingest.live_archive plan \
  --output "$archive_root" \
  --sources bom
python -m pipelines.ingest.live_archive record \
  --output "$archive_root" \
  --workers 1
```

Never commit `data/live-archive` to Git. In particular, the Bureau public-feed terms
allow this internal prototype use but do not permit a public/commercial product or
raw-data redistribution. Obtain a suitable Bureau data-service licence before such a
use. NOAA GEFS is U.S. Government open data; Himawari data should attribute JMA and
NOAA without implying endorsement.

## Archive layout and provenance

```text
archive-root/
├── blobs/sha256/ab/<full sha256>                 exact response bytes
├── plans/live-<as-of>-<plan hash>.json           frozen source requests
├── records/<source>/<product>/<record-id>.json   immutable provenance
└── runs/live-<as-of>-<report hash>.json           success/failure report
```

Raw payloads are deduplicated by SHA-256. Record and plan files are canonical JSON and
create-only. A record keeps `available_at`, `retrieved_at`, source issue time, valid
time, URL, media type, byte count, checksum, bounded response headers, source key,
licence notice, and source-specific metadata. A conflicting reuse of a record ID
fails closed.

`available_at` is deliberately the first time this recorder successfully obtained
and validated the bytes. That is conservative for hindcasts. Where an upstream
`Last-Modified` value exists it is parsed separately as
`upstream_last_modified_at`; it never replaces the observed receipt time.

The CLI downloads up to four objects concurrently, interleaves sources so one outage
cannot starve another, caps each response, validates the source signature/schema, and
stops launching work after a 45-minute cycle deadline. It continues independent
sources after one failure and exits nonzero if fewer than 90% of a source's planned
objects succeed. GEFS completeness is also checked independently at every forecast
lead so concentrated member gaps cannot hide inside an overall ratio. Deadline-expired
objects and every other failure remain in the run report. The scheduled workflow
requires 100% completeness, uploads all successful partial data and that report, and
only then marks an incomplete run failed.

The seven-hour satellite window deliberately overlaps a six-hour local schedule so
ordinary scheduler drift does not create gaps. Content-addressed blobs deduplicate the
overlap. A PC that remains off longer still needs an explicit backfill/gap workflow;
an individually complete cycle does not prove the whole archive is continuous.

## Optional GitHub/AWS publisher

The workflow `.github/workflows/archive-live-data.yml` is now manual-only because the
chosen starter deployment is the local Windows PC. It remains an optional AWS fallback
and refuses to run until these repository secrets exist:

- `AWS_OIDC_ROLE_ARN`: an AWS role trusted only for this repository's default-branch
  GitHub OIDC subject;
- `ORDCAST_ARCHIVE_S3_URI`: an explicit destination such as
  `s3://ordcast-prod-data/ordcast/live`.

The role should have only `s3:GetObject` and `s3:PutObject` for that exact prefix, plus
the bucket-owner check needed by AWS. Do not grant delete permission. The workflow
downloads and validates all upstream data before requesting AWS credentials, then
uploads each file with `If-None-Match: *`; it will reuse an identical object but never
overwrite a different one. With the default plan this is roughly 2,600 immutable
files per cycle; publication runs eight create-only uploads in parallel, with blobs
completed before their manifests.

No AWS configuration is needed for local operation. If the manual cloud publisher is
ever enabled, its hosted runner still is not a guaranteed meteorological service.

## Operational acceptance checks

Before using this history for training:

1. Run it manually and inspect one manifest from each source.
2. Confirm the S3 bucket has versioning, encryption, retention, and lifecycle rules.
3. Add alerts for a failed workflow and for gaps in expected source/valid times.
4. Collect through at least one complete wet season.
5. Parse source-specific time semantics and never use `retrieved_at`-future data in a
   historical forecast.
6. Compare the raw ensemble with climatology and the SILO analog before training a
   more complex model.
