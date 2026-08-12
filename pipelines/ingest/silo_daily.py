"""Download and validate SILO patched-point daily rainfall CSV data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from numbers import Integral
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

SILO_ENDPOINT = (
    "https://www.longpaddock.qld.gov.au/cgi-bin/silo/PatchedPointDataset.php"
)
KUNUNURRA_AERO_STATION = 2056
MAX_DOWNLOAD_BYTES = 25_000_000
_INTEGER_TEXT = re.compile(r"[+-]?\d+")


@dataclass(frozen=True)
class DailyRainSeries:
    station: int
    dates: np.ndarray
    rain_mm: np.ndarray
    source_codes: np.ndarray

    def __post_init__(self) -> None:
        dates = np.array(self.dates, dtype="datetime64[D]", copy=True)
        rain = np.array(self.rain_mm, dtype=np.float64, copy=True)
        codes = np.array(self.source_codes, dtype=np.int64, copy=True)
        if dates.ndim != 1 or dates.size == 0:
            raise ValueError("SILO series must contain at least one date")
        if rain.shape != dates.shape or codes.shape != dates.shape:
            raise ValueError("SILO date, rainfall, and source-code arrays must align")
        if np.isnat(dates).any() or not np.all(np.isfinite(rain)) or np.any(rain < 0):
            raise ValueError("SILO dates and rainfall must be valid and non-negative")
        if np.any(np.diff(dates).astype("timedelta64[D]").astype(int) != 1):
            raise ValueError("SILO daily series must be sorted, unique, and continuous")
        if isinstance(self.station, (bool, np.bool_)) or not isinstance(
            self.station, Integral
        ):
            raise ValueError("SILO station must be an integer")
        if int(self.station) <= 0:
            raise ValueError("SILO station must be a positive integer")
        if np.any(codes < 0):
            raise ValueError("SILO source codes must be non-negative")
        dates.setflags(write=False)
        rain.setflags(write=False)
        codes.setflags(write=False)
        object.__setattr__(self, "station", int(self.station))
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "rain_mm", rain)
        object.__setattr__(self, "source_codes", codes)


def _silo_date(value: str | date) -> str:
    if isinstance(value, date):
        parsed = value
    else:
        parsed = date.fromisoformat(str(value))
    return parsed.strftime("%Y%m%d")


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def build_silo_station_url(
    station: int,
    start: str | date,
    finish: str | date,
    username: str,
) -> str:
    """Build the documented SILO CSV request for daily rain plus source codes."""
    if isinstance(station, (bool, np.bool_)) or not isinstance(station, Integral):
        raise ValueError("station must be an integer")
    station_number = int(station)
    if station_number <= 0:
        raise ValueError("station must be positive")
    email = username.strip()
    if not email or "@" not in email:
        raise ValueError("SILO_USERNAME must be a valid contact email")
    start_date = _silo_date(start)
    finish_date = _silo_date(finish)
    if start_date > finish_date:
        raise ValueError("start date cannot follow finish date")
    query = urlencode(
        {
            "station": station_number,
            "start": start_date,
            "finish": finish_date,
            "format": "csv",
            "comment": "R",
            "username": email,
        }
    )
    return f"{SILO_ENDPOINT}?{query}"


def parse_silo_rain_csv(text: str, expected_station: int | None = None) -> DailyRainSeries:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"station", "date", "daily_rain", "daily_rain_source"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise ValueError("response is not a SILO daily-rain CSV dataset")

    stations: list[int] = []
    dates: list[np.datetime64] = []
    rain: list[float] = []
    source_codes: list[int] = []
    for line_number, row in enumerate(reader, start=2):
        try:
            station_text = str(row["station"]).strip()
            source_text = str(row["daily_rain_source"]).strip()
            if not _INTEGER_TEXT.fullmatch(station_text) or not _INTEGER_TEXT.fullmatch(
                source_text
            ):
                raise ValueError
            stations.append(int(station_text))
            dates.append(np.datetime64(date.fromisoformat(row["date"]), "D"))
            rain.append(float(row["daily_rain"]))
            source_codes.append(int(source_text))
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid SILO CSV row {line_number}") from error

    if not stations or len(set(stations)) != 1:
        raise ValueError("SILO response must contain exactly one station")
    station = stations[0]
    if expected_station is not None and station != int(expected_station):
        raise ValueError(
            f"SILO response station {station} does not match requested {expected_station}"
        )
    return DailyRainSeries(station, dates, rain, source_codes)


def download_silo_station_daily_rain(
    station: int,
    start: str | date,
    finish: str | date,
    username: str,
    *,
    timeout_seconds: float = 60.0,
) -> bytes:
    """Download raw CSV bytes so callers can archive the exact source response."""
    url = build_silo_station_url(station, start, finish, username)
    request = Request(url, headers={"User-Agent": "OrdCast-RET/0.1"})
    with urlopen(request, timeout=float(timeout_seconds)) as response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValueError("SILO response exceeds the configured download limit")
    series = parse_silo_rain_csv(payload.decode("utf-8"), expected_station=station)
    start_date = _as_date(start)
    finish_date = _as_date(finish)
    expected_rows = (finish_date - start_date).days + 1
    if (
        series.dates[0] != np.datetime64(start_date, "D")
        or series.dates[-1] != np.datetime64(finish_date, "D")
        or len(series.dates) != expected_rows
    ):
        raise ValueError(
            "SILO response does not cover the requested inclusive date range"
        )
    return payload


def silo_snapshot_manifest_path(snapshot_path: Path) -> Path:
    """Return the sidecar path used for an immutable SILO snapshot manifest."""
    return snapshot_path.with_name(f"{snapshot_path.name}.manifest.json")


def write_silo_snapshot(
    payload: bytes,
    snapshot_path: Path,
    *,
    station: int,
    start: str | date,
    finish: str | date,
    retrieved_at: datetime | None = None,
) -> Path:
    """Exclusively create raw snapshot bytes and a provenance sidecar.

    Existing snapshots are never overwritten. The request manifest deliberately omits
    the SILO contact email while retaining the reproducible dataset parameters.
    """
    series = parse_silo_rain_csv(payload.decode("utf-8"), expected_station=station)
    start_date = _as_date(start)
    finish_date = _as_date(finish)
    expected_rows = (finish_date - start_date).days + 1
    if expected_rows <= 0 or (
        series.dates[0] != np.datetime64(start_date, "D")
        or series.dates[-1] != np.datetime64(finish_date, "D")
        or len(series.dates) != expected_rows
    ):
        raise ValueError(
            "SILO response does not cover the requested inclusive date range"
        )

    observed_at = retrieved_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("retrieved_at must include a timezone")
    observed_at_utc = observed_at.astimezone(timezone.utc)
    manifest = {
        "retrieved_at_utc": observed_at_utc.isoformat().replace("+00:00", "Z"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "request": {
            "endpoint": SILO_ENDPOINT,
            "station": int(station),
            "start": start_date.isoformat(),
            "finish": finish_date.isoformat(),
            "format": "csv",
            "comment": "R",
        },
        "response": {
            "station": series.station,
            "first_date": str(series.dates[0]),
            "last_date": str(series.dates[-1]),
            "row_count": len(series.dates),
        },
    }

    manifest_path = silo_snapshot_manifest_path(snapshot_path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists() or manifest_path.exists():
        raise FileExistsError("SILO snapshot or manifest already exists")
    # Exclusive mode closes the remaining race between the existence check and write.
    with snapshot_path.open("xb") as snapshot_file:
        snapshot_file.write(payload)
    try:
        with manifest_path.open("x", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2)
            manifest_file.write("\n")
    except Exception:
        # This invocation created the snapshot, so remove it if its provenance sidecar
        # cannot be created. Pre-existing files are rejected before either open.
        snapshot_path.unlink(missing_ok=True)
        raise
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download an immutable SILO daily-rain CSV snapshot."
    )
    parser.add_argument("--station", type=int, default=KUNUNURRA_AERO_STATION)
    parser.add_argument("--start", required=True, help="ISO date, for example 1971-01-01")
    parser.add_argument("--finish", required=True, help="ISO date")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    username = os.environ.get("SILO_USERNAME", "")
    payload = download_silo_station_daily_rain(
        args.station, args.start, args.finish, username
    )
    manifest_path = write_silo_snapshot(
        payload,
        args.out,
        station=args.station,
        start=args.start,
        finish=args.finish,
    )
    print(f"Wrote validated SILO snapshot to {args.out}")
    print(f"Wrote provenance manifest to {manifest_path}")


if __name__ == "__main__":
    main()
