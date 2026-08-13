"""Deterministic plans and bounded downloads for OrdCast's first live sources.

The functions in this module deliberately select explicit issue/scan times.  A live
recorder must never archive an ambiguous ``latest`` URL and later pretend to know
which model cycle or satellite image it received.
"""

from __future__ import annotations

import bz2
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from numbers import Real
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen

UTC = timezone.utc

BOM_KUNUNURRA_URL = (
    "https://www.bom.gov.au/fwo/IDW60801/IDW60801.94216.json"
)
BOM_PRODUCT_ID = "IDW60801"
BOM_WMO_ID = 94216

GEFS_FILTER_URL = (
    "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p25s.pl"
)
# Six-hourly leads preserve a continuous 0–240 h precipitation series. NOAA's APCP
# messages at these leads cover the adjacent 0–6, 6–12, ... windows; a sparse lead
# selection would permanently discard the intervals between requested files.
GEFS_DEFAULT_HORIZONS = tuple(range(0, 241, 6))
GEFS_DEFAULT_MEMBERS = ("c00",) + tuple(f"p{number:02d}" for number in range(1, 31))
# A deliberately small training domain around the Ord.  Longitudes are east-positive.
GEFS_ORD_BOUNDS = {
    "leftlon": "125",
    "rightlon": "132",
    "toplat": "-12",
    "bottomlat": "-20",
}
GEFS_LEVELS = (
    "lev_2_m_above_ground",
    "lev_10_m_above_ground",
    "lev_3000-0_m_above_ground",
    "lev_entire_atmosphere_(considered_as_a_single_layer)",
    "lev_mean_sea_level",
    "lev_surface",
)
GEFS_VARIABLES = (
    "var_APCP",
    "var_CAPE",
    "var_CIN",
    "var_DPT",
    "var_GUST",
    "var_HLCY",
    "var_PRMSL",
    "var_PWAT",
    "var_RH",
    "var_TMP",
    "var_UGRD",
    "var_VGRD",
)

HIMAWARI_BUCKET_URL = "https://noaa-himawari9.s3.amazonaws.com"
HIMAWARI_BAND = 13
HIMAWARI_SEGMENT = 7
HIMAWARI_SEGMENT_COUNT = 10
HIMAWARI_MAX_DECOMPRESSED_BYTES = 50_000_000
HIMAWARI_MAX_BZIP2_STREAMS = 64

GEFS_REQUIRED_PARAMETERS = frozenset(
    {
        (0, 0),  # TMP
        (0, 6),  # DPT
        (1, 1),  # RH
        (1, 3),  # PWAT
        (1, 8),  # APCP (absent only at analysis hour 0)
        (2, 2),  # UGRD
        (2, 3),  # VGRD
        (2, 22),  # GUST
        (3, 1),  # PRMSL
        (7, 6),  # CAPE
        (7, 7),  # CIN
        (7, 8),  # HLCY
    }
)


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class ArtifactSpec:
    """One immutable, explicitly timed remote artifact to retrieve."""

    source: str
    source_key: str
    url: str
    event_time: datetime
    media_type: str
    license_notice: str
    max_bytes: int
    issue_time: datetime | None = None
    valid_time: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("source", self.source),
            ("source_key", self.source_key),
            ("url", self.url),
            ("media_type", self.media_type),
            ("license_notice", self.license_notice),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
        source = self.source.strip()
        source_key = self.source_key.strip()
        url = self.url.strip()
        media_type = self.media_type.strip()
        license_notice = self.license_notice.strip()
        if not source:
            raise ValueError("artifact source cannot be empty")
        if len(source) > 128 or any(ord(character) < 32 for character in source):
            raise ValueError("artifact source is too long or contains controls")
        if (
            not source_key
            or len(source_key) > 1024
            or source_key.startswith("/")
            or ".." in source_key.split("/")
            or "\\" in source_key
            or any(ord(character) < 32 for character in source_key)
        ):
            raise ValueError("source_key must be a safe relative object key")
        if len(url) > 4096 or not url.startswith("https://"):
            raise ValueError("live artifact URLs must use HTTPS")
        if any(ord(character) < 32 for character in url):
            raise ValueError("live artifact URL cannot contain controls")
        if not media_type or len(media_type) > 255:
            raise ValueError("media_type must be a short, non-empty value")
        if not license_notice or len(license_notice) > 2048:
            raise ValueError("license_notice must be a short, non-empty value")
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool):
            raise ValueError("max_bytes must be an integer")
        if self.max_bytes <= 0 or self.max_bytes > 100_000_000:
            raise ValueError("max_bytes must be between 1 and 100,000,000")
        event_time = _utc(self.event_time, field_name="event_time")
        issue_time = (
            None
            if self.issue_time is None
            else _utc(self.issue_time, field_name="issue_time")
        )
        valid_time = (
            None
            if self.valid_time is None
            else _utc(self.valid_time, field_name="valid_time")
        )
        if issue_time is not None and valid_time is not None and valid_time < issue_time:
            raise ValueError("valid_time cannot precede issue_time")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("artifact metadata must be an object")
        metadata = _thaw_json(self.metadata)
        if not _json_value(metadata):
            raise ValueError("artifact metadata must be JSON-compatible")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_key", source_key)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "license_notice", license_notice)
        object.__setattr__(self, "event_time", event_time)
        object.__setattr__(self, "issue_time", issue_time)
        object.__setattr__(self, "valid_time", valid_time)
        object.__setattr__(self, "metadata", _freeze_json(metadata))

    def to_dict(self) -> dict[str, Any]:
        def stamp(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat().replace("+00:00", "Z")

        return {
            "source": self.source,
            "source_key": self.source_key,
            "url": self.url,
            "event_time": stamp(self.event_time),
            "issue_time": stamp(self.issue_time),
            "valid_time": stamp(self.valid_time),
            "media_type": self.media_type,
            "license_notice": self.license_notice,
            "max_bytes": self.max_bytes,
            "metadata": _thaw_json(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactSpec":
        """Rebuild a spec from a recorder plan without permissive coercion."""
        expected = {
            "source",
            "source_key",
            "url",
            "event_time",
            "issue_time",
            "valid_time",
            "media_type",
            "license_notice",
            "max_bytes",
            "metadata",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("artifact plan has missing or unknown fields")

        def parse_stamp(name: str, *, optional: bool) -> datetime | None:
            raw = value[name]
            if raw is None and optional:
                return None
            if not isinstance(raw, str) or not raw.endswith("Z"):
                raise ValueError(f"artifact {name} must be a canonical UTC timestamp")
            try:
                parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
            except ValueError as error:
                raise ValueError(f"artifact {name} is not a valid timestamp") from error
            if parsed.isoformat().replace("+00:00", "Z") != raw:
                raise ValueError(f"artifact {name} must be a canonical UTC timestamp")
            return parsed

        if not isinstance(value["max_bytes"], int) or isinstance(value["max_bytes"], bool):
            raise ValueError("artifact max_bytes must be an integer")
        if not isinstance(value["metadata"], Mapping):
            raise ValueError("artifact metadata must be an object")
        for name in (
            "source",
            "source_key",
            "url",
            "media_type",
            "license_notice",
        ):
            if not isinstance(value[name], str):
                raise ValueError(f"artifact {name} must be a string")
        return cls(
            source=value["source"],
            source_key=value["source_key"],
            url=value["url"],
            event_time=parse_stamp("event_time", optional=False),
            issue_time=parse_stamp("issue_time", optional=True),
            valid_time=parse_stamp("valid_time", optional=True),
            media_type=value["media_type"],
            license_notice=value["license_notice"],
            max_bytes=value["max_bytes"],
            metadata=dict(value["metadata"]),
        )


@dataclass(frozen=True)
class FetchedArtifact:
    """Validated source bytes plus the receipt time that proves availability."""

    spec: ArtifactSpec
    payload: bytes
    retrieved_at: datetime
    sha256: str
    response_headers: Mapping[str, str] = field(default_factory=dict)
    upstream_last_modified_at: datetime | None = None

    def __post_init__(self) -> None:
        payload = bytes(self.payload)
        if not payload:
            raise ValueError("downloaded artifact cannot be empty")
        digest = hashlib.sha256(payload).hexdigest()
        if self.sha256 != digest:
            raise ValueError("artifact SHA-256 does not match its payload")
        object.__setattr__(
            self, "retrieved_at", _utc(self.retrieved_at, field_name="retrieved_at")
        )
        if self.upstream_last_modified_at is not None:
            published = _utc(
                self.upstream_last_modified_at,
                field_name="upstream_last_modified_at",
            )
            if published > self.retrieved_at:
                raise ValueError("upstream_last_modified_at cannot follow retrieved_at")
            object.__setattr__(self, "upstream_last_modified_at", published)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(
            self,
            "response_headers",
            MappingProxyType({str(k).lower(): str(v) for k, v in self.response_headers.items()}),
        )


def plan_bom_kununurra(as_of: datetime) -> ArtifactSpec:
    """Plan one retrieval of the official 72-hour Kununurra Airport JSON."""
    planned_at = _utc(as_of, field_name="as_of")
    request_stamp = planned_at.strftime("%Y%m%dT%H%M%SZ")
    return ArtifactSpec(
        source="bom-kununurra-observations",
        source_key=f"bom/kununurra-aero/requests/{request_stamp}.json",
        url=BOM_KUNUNURRA_URL,
        event_time=planned_at,
        media_type="application/json",
        license_notice=(
            "Bureau public data feed: internal/non-commercial use only; obtain an "
            "appropriate Bureau data-service licence before public or commercial use"
        ),
        max_bytes=2_000_000,
        metadata={
            "product_id": BOM_PRODUCT_ID,
            "wmo_id": BOM_WMO_ID,
            "station_number": "002056",
            "planned_event_time_note": (
                "request time; newest contained observation time is verified after download"
            ),
        },
    )


def latest_gefs_cycle(as_of: datetime, *, publication_lag_hours: int = 6) -> datetime:
    """Return the newest explicit six-hour GEFS cycle expected to be complete."""
    observed_at = _utc(as_of, field_name="as_of")
    if (
        not isinstance(publication_lag_hours, int)
        or isinstance(publication_lag_hours, bool)
        or publication_lag_hours < 0
    ):
        raise ValueError("publication_lag_hours must be a non-negative integer")
    candidate = observed_at - timedelta(hours=publication_lag_hours)
    return candidate.replace(
        hour=(candidate.hour // 6) * 6, minute=0, second=0, microsecond=0
    )


def _gefs_member(member: str) -> str:
    text = str(member).strip().lower()
    if text not in GEFS_DEFAULT_MEMBERS:
        raise ValueError("GEFS member must be c00 or p01 through p30")
    return text


def plan_gefs_ord(
    as_of: datetime,
    *,
    horizons_hours: tuple[int, ...] = GEFS_DEFAULT_HORIZONS,
    members: tuple[str, ...] = GEFS_DEFAULT_MEMBERS,
) -> list[ArtifactSpec]:
    """Plan regional 0.25-degree GEFS subsets for the 31-member ensemble."""
    cycle = latest_gefs_cycle(as_of)
    horizons: list[int] = []
    for horizon in horizons_hours:
        if (
            not isinstance(horizon, int)
            or isinstance(horizon, bool)
            or horizon < 0
            or horizon > 240
            or horizon % 6
        ):
            raise ValueError(
                "GEFS starter horizons must be 0..240 hours on the 6-hour grid"
            )
        horizons.append(horizon)
    if not horizons or len(set(horizons)) != len(horizons):
        raise ValueError("GEFS horizons must be non-empty and unique")
    selected_members = [_gefs_member(member) for member in members]
    if not selected_members or len(set(selected_members)) != len(selected_members):
        raise ValueError("GEFS members must be non-empty and unique")

    directory = f"/gefs.{cycle:%Y%m%d}/{cycle:%H}/atmos/pgrb2sp25"
    artifacts: list[ArtifactSpec] = []
    for member in selected_members:
        file_prefix = f"ge{member}"
        for horizon in sorted(horizons):
            filename = f"{file_prefix}.t{cycle:%H}z.pgrb2s.0p25.f{horizon:03d}"
            query: dict[str, str] = {
                "file": filename,
                **{level: "on" for level in GEFS_LEVELS},
                **{variable: "on" for variable in GEFS_VARIABLES},
                "subregion": "",
                **GEFS_ORD_BOUNDS,
                "dir": directory,
            }
            url = f"{GEFS_FILTER_URL}?{urlencode(query)}"
            valid_time = cycle + timedelta(hours=horizon)
            artifacts.append(
                ArtifactSpec(
                    source="noaa-gefs-0p25",
                    source_key=(
                        f"noaa-gefs/gefs.{cycle:%Y%m%d}/{cycle:%H}/"
                        f"{member}/f{horizon:03d}-ord.grib2"
                    ),
                    url=url,
                    event_time=valid_time,
                    issue_time=cycle,
                    valid_time=valid_time,
                    media_type="application/x-grib2",
                    license_notice="NOAA/NWS open U.S. Government data",
                    max_bytes=5_000_000,
                    metadata={
                        "model": "GEFS",
                        "grid_degrees": 0.25,
                        "member": member,
                        "forecast_hour": horizon,
                        "bounds": dict(GEFS_ORD_BOUNDS),
                        "variables": [name.removeprefix("var_") for name in GEFS_VARIABLES],
                        "levels": [name.removeprefix("lev_") for name in GEFS_LEVELS],
                    },
                )
            )
    return artifacts


def _floor_ten_minutes(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 10) * 10, second=0, microsecond=0)


def plan_himawari_ord(
    as_of: datetime,
    *,
    lookback_hours: int = 6,
    publication_lag_minutes: int = 30,
) -> list[ArtifactSpec]:
    """Plan native 10-minute Band-13 segments covering the Ord district."""
    observed_at = _utc(as_of, field_name="as_of")
    for name, value, minimum, maximum in (
        ("lookback_hours", lookback_hours, 1, 24),
        ("publication_lag_minutes", publication_lag_minutes, 0, 180),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
            or value > maximum
        ):
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    final_scan = _floor_ten_minutes(
        observed_at - timedelta(minutes=publication_lag_minutes)
    )
    count = lookback_hours * 6
    scans = [final_scan - timedelta(minutes=10 * offset) for offset in range(count)]
    scans.reverse()

    artifacts: list[ArtifactSpec] = []
    for scan in scans:
        filename = (
            f"HS_H09_{scan:%Y%m%d}_{scan:%H%M}_B{HIMAWARI_BAND:02d}_"
            f"FLDK_R20_S{HIMAWARI_SEGMENT:02d}{HIMAWARI_SEGMENT_COUNT:02d}.DAT.bz2"
        )
        raw_key = f"AHI-L1b-FLDK/{scan:%Y/%m/%d/%H%M}/{filename}"
        artifacts.append(
            ArtifactSpec(
                source="noaa-jma-himawari9",
                source_key=f"noaa-himawari9/{raw_key}",
                url=f"{HIMAWARI_BUCKET_URL}/{quote(raw_key, safe='/')}",
                event_time=scan,
                valid_time=scan,
                media_type="application/x-bzip2",
                license_notice=(
                    "JMA data distributed openly by NOAA; attribute JMA and NOAA and "
                    "do not imply endorsement"
                ),
                max_bytes=15_000_000,
                metadata={
                    "platform": "Himawari-9",
                    "instrument": "AHI",
                    "product": "AHI-L1b-FLDK",
                    "band": HIMAWARI_BAND,
                    "segment": HIMAWARI_SEGMENT,
                    "segment_count": HIMAWARI_SEGMENT_COUNT,
                    "nominal_resolution_km": 2.0,
                },
            )
        )
    return artifacts


def plan_live_artifacts(
    as_of: datetime,
    *,
    sources: tuple[str, ...] = ("bom", "gefs", "himawari"),
) -> list[ArtifactSpec]:
    """Build the deterministic starter-profile plan for selected sources."""
    selected = tuple(str(source).strip().lower() for source in sources)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("sources must be non-empty and unique")
    unknown = set(selected) - {"bom", "gefs", "himawari"}
    if unknown:
        raise ValueError(f"unknown live source(s): {', '.join(sorted(unknown))}")
    artifacts: list[ArtifactSpec] = []
    if "bom" in selected:
        artifacts.append(plan_bom_kununurra(as_of))
    if "gefs" in selected:
        artifacts.extend(plan_gefs_ord(as_of))
    if "himawari" in selected:
        # Seven hours gives scheduled local runs an hour of overlap. Content-addressed
        # storage deduplicates that overlap; it also avoids ten-minute holes from
        # normal Task Scheduler drift around a nominal six-hour cadence.
        artifacts.extend(plan_himawari_ord(as_of, lookback_hours=7))
    return artifacts


def _validate_bom(payload: bytes) -> datetime:
    try:
        document = json.loads(payload)
        observations = document["observations"]
        header = observations["header"][0]
        rows = observations["data"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Bureau response is not the expected observations JSON") from error
    if header.get("ID") != BOM_PRODUCT_ID or not rows:
        raise ValueError("Bureau response has the wrong product or no observations")
    if any(row.get("wmo") != BOM_WMO_ID for row in rows):
        raise ValueError("Bureau response contains an unexpected weather station")
    try:
        event_time = datetime.strptime(rows[0]["aifstime_utc"], "%Y%m%d%H%M%S").replace(
            tzinfo=UTC
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Bureau response lacks a valid newest observation time") from error
    return event_time


def _grib_sign_magnitude(raw: bytes) -> int:
    value = int.from_bytes(raw, "big")
    sign = 1 << (len(raw) * 8 - 1)
    return -(value & (sign - 1)) if value & sign else value


def _gefs_expected_ensemble(member: str) -> tuple[int, int, int]:
    if member == "c00":
        return (1, 0, 30)  # low-resolution control, member 0, 30 perturbations
    return (3, int(member[1:]), 30)


def _parse_grib_sections(message: bytes) -> tuple[int, dict[int, bytes]]:
    if len(message) < 20 or message[:4] != b"GRIB" or message[7] != 2:
        raise ValueError("GEFS response contains a non-GRIB2 message")
    if int.from_bytes(message[8:16], "big") != len(message) or message[-4:] != b"7777":
        raise ValueError("GEFS response contains an incomplete GRIB2 message")
    sections: dict[int, bytes] = {}
    position = 16
    expected_sequence = [1, 3, 4, 5, 6, 7]
    observed_sequence: list[int] = []
    while position < len(message) - 4:
        if position + 5 > len(message) - 4:
            raise ValueError("GEFS GRIB2 section header is truncated")
        length = int.from_bytes(message[position : position + 4], "big")
        number = message[position + 4]
        if length < 5 or position + length > len(message) - 4:
            raise ValueError("GEFS GRIB2 section length is invalid")
        if number == 2:
            position += length
            continue
        if number in sections:
            raise ValueError("GEFS GRIB2 message repeats a section")
        sections[number] = message[position : position + length]
        observed_sequence.append(number)
        position += length
    if position != len(message) - 4 or observed_sequence != expected_sequence:
        raise ValueError("GEFS GRIB2 message has an unexpected section sequence")
    return message[6], sections


def _validate_gefs(spec: ArtifactSpec, payload: bytes) -> dict[str, Any]:
    if spec.issue_time is None or spec.valid_time is None:
        raise ValueError("GEFS artifact requires issue and valid times")
    member = spec.metadata.get("member")
    horizon = spec.metadata.get("forecast_hour")
    if not isinstance(member, str) or member not in GEFS_DEFAULT_MEMBERS:
        raise ValueError("GEFS artifact metadata has an invalid member")
    if not isinstance(horizon, int) or isinstance(horizon, bool):
        raise ValueError("GEFS artifact metadata has an invalid forecast hour")
    expected_valid = spec.issue_time + timedelta(hours=horizon)
    if spec.valid_time != expected_valid:
        raise ValueError("GEFS valid time does not match its issue time and lead")

    position = 0
    parameters: set[tuple[int, int]] = set()
    message_count = 0
    expected_ensemble = _gefs_expected_ensemble(member)
    while position < len(payload):
        if position + 16 > len(payload) or payload[position : position + 4] != b"GRIB":
            raise ValueError("GEFS response has bytes outside complete GRIB2 messages")
        message_length = int.from_bytes(payload[position + 8 : position + 16], "big")
        if message_length < 20 or position + message_length > len(payload):
            raise ValueError("GEFS response contains a truncated GRIB2 message")
        message = payload[position : position + message_length]
        discipline, sections = _parse_grib_sections(message)

        section1 = sections[1]
        if len(section1) < 21:
            raise ValueError("GEFS identification section is too short")
        try:
            reference_time = datetime(
                int.from_bytes(section1[12:14], "big"),
                section1[14],
                section1[15],
                section1[16],
                section1[17],
                section1[18],
                tzinfo=UTC,
            )
        except ValueError as error:
            raise ValueError("GEFS GRIB2 reference time is invalid") from error
        if reference_time != spec.issue_time:
            raise ValueError("GEFS GRIB2 reference cycle does not match the plan")

        section3 = sections[3]
        if len(section3) != 72 or int.from_bytes(section3[12:14], "big") != 0:
            raise ValueError("GEFS GRIB2 grid is not the expected regular lat/lon grid")
        grid = {
            "points": int.from_bytes(section3[6:10], "big"),
            "nx": int.from_bytes(section3[30:34], "big"),
            "ny": int.from_bytes(section3[34:38], "big"),
            "lat_first": _grib_sign_magnitude(section3[46:50]),
            "lon_first": _grib_sign_magnitude(section3[50:54]),
            "lat_last": _grib_sign_magnitude(section3[55:59]),
            "lon_last": _grib_sign_magnitude(section3[59:63]),
            "dx": int.from_bytes(section3[63:67], "big"),
            "dy": int.from_bytes(section3[67:71], "big"),
        }
        if grid != {
            "points": 957,
            "nx": 29,
            "ny": 33,
            "lat_first": -20_000_000,
            "lon_first": 125_000_000,
            "lat_last": -12_000_000,
            "lon_last": 132_000_000,
            "dx": 250_000,
            "dy": 250_000,
        }:
            raise ValueError("GEFS GRIB2 grid does not match the planned Ord domain")

        section4 = sections[4]
        if len(section4) < 37:
            raise ValueError("GEFS product definition section is too short")
        template = int.from_bytes(section4[7:9], "big")
        if discipline != 0 or template not in {1, 11}:
            raise ValueError("GEFS response contains an unexpected product template")
        parameter = (section4[9], section4[10])
        if parameter in parameters:
            raise ValueError("GEFS response contains a duplicate weather parameter")
        parameters.add(parameter)
        if tuple(section4[34:37]) != expected_ensemble:
            raise ValueError("GEFS GRIB2 ensemble member does not match the plan")
        if section4[17] != 1:
            raise ValueError("GEFS forecast time is not expressed in hours")
        forecast_time = int.from_bytes(section4[18:22], "big")
        if template == 1:
            if forecast_time != horizon:
                raise ValueError("GEFS GRIB2 forecast lead does not match the plan")
        else:
            if parameter != (1, 8) or len(section4) != 61:
                raise ValueError("GEFS statistical template is not APCP accumulation")
            try:
                interval_end = datetime(
                    int.from_bytes(section4[37:39], "big"),
                    section4[39],
                    section4[40],
                    section4[41],
                    section4[42],
                    section4[43],
                    tzinfo=UTC,
                )
            except ValueError as error:
                raise ValueError("GEFS APCP interval end time is invalid") from error
            if (
                interval_end != spec.valid_time
                or section4[49] != 1
                or section4[51] != 1
                or int.from_bytes(section4[52:56], "big") != 6
                or forecast_time != max(0, horizon - 6)
            ):
                raise ValueError("GEFS APCP interval does not match the planned 6-hour lead")
        message_count += 1
        position += message_length

    required = set(GEFS_REQUIRED_PARAMETERS)
    if horizon == 0:
        required.remove((1, 8))
    if parameters != required or message_count != len(required):
        raise ValueError("GEFS response does not contain exactly the requested fields")
    return {
        "grib2_validation_version": 1,
        "grib2_message_count": message_count,
        "grib2_reference_time": spec.issue_time.isoformat().replace("+00:00", "Z"),
        "grib2_parameter_codes": [
            f"0.{category}.{number}" for category, number in sorted(parameters)
        ],
    }


def _validated_spec(spec: ArtifactSpec, payload: bytes) -> ArtifactSpec:
    if spec.source == "bom-kununurra-observations":
        event_time = _validate_bom(payload)
        metadata = dict(spec.metadata)
        metadata["newest_observation_time"] = event_time.isoformat().replace("+00:00", "Z")
        return ArtifactSpec(
            source=spec.source,
            source_key=spec.source_key,
            url=spec.url,
            event_time=event_time,
            issue_time=spec.issue_time,
            valid_time=spec.valid_time,
            media_type=spec.media_type,
            license_notice=spec.license_notice,
            max_bytes=spec.max_bytes,
            metadata=metadata,
        )
    if spec.source == "noaa-gefs-0p25":
        metadata = _thaw_json(spec.metadata)
        metadata.update(_validate_gefs(spec, payload))
        return ArtifactSpec(
            source=spec.source,
            source_key=spec.source_key,
            url=spec.url,
            event_time=spec.event_time,
            issue_time=spec.issue_time,
            valid_time=spec.valid_time,
            media_type=spec.media_type,
            license_notice=spec.license_notice,
            max_bytes=spec.max_bytes,
            metadata=metadata,
        )
    elif spec.source == "noaa-jma-himawari9":
        if not payload.startswith(b"BZh"):
            raise ValueError("Himawari response is not a bzip2-compressed AHI segment")
        decompressed_bytes = 0
        compressed = payload
        streams = 0
        try:
            while compressed:
                streams += 1
                if streams > HIMAWARI_MAX_BZIP2_STREAMS:
                    raise ValueError("Himawari segment has too many bzip2 streams")
                if not compressed.startswith(b"BZh"):
                    raise ValueError("Himawari segment has unexpected trailing bytes")
                decompressor = bz2.BZ2Decompressor()
                stream_input = compressed
                while True:
                    chunk = decompressor.decompress(
                        stream_input,
                        max_length=(
                            HIMAWARI_MAX_DECOMPRESSED_BYTES - decompressed_bytes + 1
                        ),
                    )
                    decompressed_bytes += len(chunk)
                    if decompressed_bytes > HIMAWARI_MAX_DECOMPRESSED_BYTES:
                        raise ValueError(
                            "Himawari decompressed segment exceeds its size limit"
                        )
                    if decompressor.eof:
                        compressed = decompressor.unused_data
                        break
                    if decompressor.needs_input:
                        raise ValueError("Himawari segment is truncated")
                    stream_input = b""
        except OSError as error:
            raise ValueError("Himawari segment has invalid bzip2 data") from error
    else:
        raise ValueError(f"no payload validator is registered for {spec.source}")
    return spec


def _fetch_artifact_once(
    spec: ArtifactSpec,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any],
    retrieved_at: datetime | None,
) -> FetchedArtifact:
    request = Request(
        spec.url,
        headers={
            "User-Agent": "OrdCast-RET/0.1 (+https://github.com/bassyboi/Weather-wizard)",
            "Accept-Encoding": "identity",
        },
    )
    with opener(request, timeout=float(timeout_seconds)) as response:
        payload = response.read(spec.max_bytes + 1)
        headers = {str(key): str(value) for key, value in response.headers.items()}
    if len(payload) > spec.max_bytes:
        raise ValueError(f"{spec.source_key} exceeds its {spec.max_bytes}-byte limit")
    if not payload:
        raise ValueError(f"{spec.source_key} returned an empty response")
    content_length = next(
        (value for key, value in headers.items() if key.casefold() == "content-length"),
        None,
    )
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise ValueError("upstream Content-Length is not an integer") from error
        if declared_length < 0 or declared_length != len(payload):
            raise ValueError("downloaded bytes do not match upstream Content-Length")
    validated = _validated_spec(spec, payload)
    received = retrieved_at or datetime.now(UTC)
    published: datetime | None = None
    last_modified = next(
        (value for key, value in headers.items() if key.casefold() == "last-modified"),
        None,
    )
    if last_modified:
        try:
            candidate = parsedate_to_datetime(last_modified)
            if candidate.tzinfo is not None and candidate.astimezone(UTC) <= received:
                published = candidate.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            published = None
    return FetchedArtifact(
        spec=validated,
        payload=payload,
        retrieved_at=received,
        sha256=hashlib.sha256(payload).hexdigest(),
        response_headers=headers,
        upstream_last_modified_at=published,
    )


def fetch_artifact(
    spec: ArtifactSpec,
    *,
    timeout_seconds: float = 60.0,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
    opener: Callable[..., Any] = urlopen,
    retrieved_at: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> FetchedArtifact:
    """Download, validate, and retry one transiently unavailable live artifact."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, Real)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
        or timeout_seconds > 300
    ):
        raise ValueError("timeout_seconds must be greater than 0 and at most 300")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 5:
        raise ValueError("attempts must be an integer from 1 to 5")
    if (
        isinstance(backoff_seconds, bool)
        or not isinstance(backoff_seconds, Real)
        or not math.isfinite(float(backoff_seconds))
        or backoff_seconds < 0
        or backoff_seconds > 60
    ):
        raise ValueError("backoff_seconds must be from 0 to 60")
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return _fetch_artifact_once(
                spec,
                timeout_seconds=float(timeout_seconds),
                opener=opener,
                retrieved_at=retrieved_at,
            )
        except HTTPError as error:
            last_error = error
            retryable = error.code in {404, 408, 425, 429} or 500 <= error.code <= 599
            if not retryable or attempt + 1 == attempts:
                raise
        except (URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            if attempt + 1 == attempts:
                raise
        except ValueError as error:
            # A just-published dynamic endpoint can briefly return HTML/XML with a
            # successful status. Retrying also protects against a truncated body.
            last_error = error
            if "byte limit" in str(error) or attempt + 1 == attempts:
                raise
        sleeper(float(backoff_seconds) * (2**attempt))
    assert last_error is not None  # loop and validation above make this unreachable
    raise last_error
