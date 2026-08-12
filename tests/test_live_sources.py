from __future__ import annotations

import io
import json
import bz2
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from ordcast.live_sources import (
    ArtifactSpec,
    GEFS_REQUIRED_PARAMETERS,
    fetch_artifact,
    latest_gefs_cycle,
    plan_bom_kununurra,
    plan_gefs_ord,
    plan_himawari_ord,
)

UTC = timezone.utc


def _grib_section(number: int, body: bytes = b"") -> bytes:
    length = 5 + len(body)
    return length.to_bytes(4, "big") + bytes([number]) + body


def _fake_gefs_payload(spec: ArtifactSpec) -> bytes:
    issue = spec.issue_time
    valid = spec.valid_time
    assert issue is not None and valid is not None
    horizon = spec.metadata["forecast_hour"]
    member = spec.metadata["member"]
    ensemble = (
        bytes((1, 0, 30))
        if member == "c00"
        else bytes((3, int(str(member)[1:]), 30))
    )
    section1 = bytearray(21)
    section1[:5] = (21).to_bytes(4, "big") + b"\x01"
    section1[12:14] = issue.year.to_bytes(2, "big")
    section1[14:19] = bytes(
        (issue.month, issue.day, issue.hour, issue.minute, issue.second)
    )
    section3 = bytes.fromhex(
        "000000480300000003bd0000000006000000000000000000000000000000000000"
        "1d0000002100000000ffffffff81312d00077359403080b71b0007de29000003d0"
        "900003d09040"
    )

    def message(category: int, number: int) -> bytes:
        if (category, number) == (1, 8):
            section4 = bytearray(61)
            section4[:5] = (61).to_bytes(4, "big") + b"\x04"
            section4[7:9] = (11).to_bytes(2, "big")
            section4[9:11] = bytes((category, number))
            section4[17] = 1
            section4[18:22] = max(0, horizon - 6).to_bytes(4, "big")
            section4[34:37] = ensemble
            section4[37:44] = valid.year.to_bytes(2, "big") + bytes(
                (valid.month, valid.day, valid.hour, valid.minute, valid.second)
            )
            section4[44] = 1
            section4[49] = 1
            section4[50] = 2
            section4[51] = 1
            section4[52:56] = (6).to_bytes(4, "big")
            section4[56] = 255
        else:
            section4 = bytearray(37)
            section4[:5] = (37).to_bytes(4, "big") + b"\x04"
            section4[7:9] = (1).to_bytes(2, "big")
            section4[9:11] = bytes((category, number))
            section4[17] = 1
            section4[18:22] = horizon.to_bytes(4, "big")
            section4[34:37] = ensemble
        sections = (
            bytes(section1)
            + section3
            + bytes(section4)
            + _grib_section(5)
            + _grib_section(6)
            + _grib_section(7)
        )
        total = 16 + len(sections) + 4
        return b"GRIB\x00\x00\x00\x02" + total.to_bytes(8, "big") + sections + b"7777"

    parameters = set(GEFS_REQUIRED_PARAMETERS)
    if horizon == 0:
        parameters.remove((1, 8))
    return b"".join(message(category, number) for category, number in sorted(parameters))


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(payload)
        self.headers = headers or {"Content-Type": "application/octet-stream"}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_gefs_plan_freezes_explicit_cycle_members_horizons_and_ord_subset() -> None:
    as_of = datetime(2026, 8, 12, 12, 34, tzinfo=UTC)
    assert latest_gefs_cycle(as_of) == datetime(2026, 8, 12, 6, tzinfo=UTC)

    plan = plan_gefs_ord(as_of, horizons_hours=(0, 6), members=("c00", "p30"))

    assert len(plan) == 4
    assert len({item.source_key for item in plan}) == 4
    assert all(item.issue_time == datetime(2026, 8, 12, 6, tzinfo=UTC) for item in plan)
    assert plan[-1].valid_time == datetime(2026, 8, 12, 12, tzinfo=UTC)
    query = parse_qs(urlsplit(plan[-1].url).query, keep_blank_values=True)
    assert query["file"] == ["gep30.t06z.pgrb2s.0p25.f006"]
    assert query["dir"] == ["/gefs.20260812/06/atmos/pgrb2sp25"]
    assert query["leftlon"] == ["125"]
    assert query["rightlon"] == ["132"]
    assert query["toplat"] == ["-12"]
    assert query["bottomlat"] == ["-20"]
    assert query["var_APCP"] == ["on"]
    assert query["var_HLCY"] == ["on"]
    assert "latest" not in plan[-1].url


@pytest.mark.parametrize("invalid", [-6, 3, 241, 384, True])
def test_gefs_plan_rejects_invalid_horizons(invalid: int) -> None:
    with pytest.raises(ValueError, match="horizons"):
        plan_gefs_ord(
            datetime(2026, 8, 12, tzinfo=UTC),
            horizons_hours=(invalid,),
            members=("c00",),
        )


def test_himawari_plan_is_ordered_and_uses_only_ord_segment() -> None:
    plan = plan_himawari_ord(
        datetime(2026, 8, 12, 6, 59, tzinfo=UTC),
        lookback_hours=1,
        publication_lag_minutes=30,
    )

    assert len(plan) == 6
    assert plan[0].event_time == datetime(2026, 8, 12, 5, 30, tzinfo=UTC)
    assert plan[-1].event_time == datetime(2026, 8, 12, 6, 20, tzinfo=UTC)
    assert all("_B13_FLDK_R20_S0710.DAT.bz2" in item.url for item in plan)
    assert all(item.metadata["segment"] == 7 for item in plan)


def test_artifact_spec_round_trip_is_strict() -> None:
    spec = plan_bom_kununurra(datetime(2026, 8, 12, 6, tzinfo=UTC))
    restored = ArtifactSpec.from_dict(spec.to_dict())
    assert restored.to_dict() == spec.to_dict()

    malformed = spec.to_dict()
    malformed["typo"] = True
    with pytest.raises(ValueError, match="missing or unknown"):
        ArtifactSpec.from_dict(malformed)


def test_fetch_bom_validates_station_and_records_source_event_time() -> None:
    spec = plan_bom_kununurra(datetime(2026, 8, 12, 6, 15, tzinfo=UTC))
    payload = json.dumps(
        {
            "observations": {
                "header": [{"ID": "IDW60801"}],
                "data": [
                    {
                        "wmo": 94216,
                        "aifstime_utc": "20260812060000",
                        "air_temp": 31.2,
                        "rain_trace": "0.0",
                    }
                ],
            }
        }
    ).encode()
    retrieved_at = datetime(2026, 8, 12, 6, 16, tzinfo=UTC)

    fetched = fetch_artifact(
        spec,
        opener=lambda *_args, **_kwargs: FakeResponse(
            payload, {"Last-Modified": "Wed, 12 Aug 2026 06:13:00 GMT"}
        ),
        retrieved_at=retrieved_at,
    )

    assert fetched.spec.event_time == datetime(2026, 8, 12, 6, tzinfo=UTC)
    assert fetched.spec.metadata["newest_observation_time"] == "2026-08-12T06:00:00Z"
    assert fetched.retrieved_at == retrieved_at
    assert fetched.response_headers["last-modified"].endswith("GMT")
    assert fetched.upstream_last_modified_at == datetime(
        2026, 8, 12, 6, 13, tzinfo=UTC
    )


def test_fetch_rejects_invalid_or_oversize_payloads() -> None:
    spec = plan_gefs_ord(
        datetime(2026, 8, 12, 12, tzinfo=UTC),
        horizons_hours=(6,),
        members=("c00",),
    )[0]
    valid_payload = _fake_gefs_payload(spec)
    fetched = fetch_artifact(
        spec,
        opener=lambda *_args, **_kwargs: FakeResponse(valid_payload),
        retrieved_at=datetime(2026, 8, 12, 12, 1, tzinfo=UTC),
    )
    assert fetched.payload == valid_payload
    assert fetched.spec.metadata["grib2_message_count"] == 12

    with pytest.raises(ValueError, match="complete GRIB2"):
        fetch_artifact(
            spec,
            opener=lambda *_args, **_kwargs: FakeResponse(b"<html>not ready</html>"),
            attempts=1,
        )

    small = ArtifactSpec(
        source=spec.source,
        source_key=spec.source_key,
        url=spec.url,
        event_time=spec.event_time,
        issue_time=spec.issue_time,
        valid_time=spec.valid_time,
        media_type=spec.media_type,
        license_notice=spec.license_notice,
        max_bytes=4,
        metadata=spec.metadata,
    )
    with pytest.raises(ValueError, match="byte limit"):
        fetch_artifact(
            small,
            opener=lambda *_args, **_kwargs: FakeResponse(b"GRIBpayload7777"),
            attempts=1,
        )


def test_fetch_retries_a_transient_invalid_response() -> None:
    spec = plan_gefs_ord(
        datetime(2026, 8, 12, 12, tzinfo=UTC),
        horizons_hours=(6,),
        members=("c00",),
    )[0]
    valid_payload = _fake_gefs_payload(spec)
    payloads = iter((b"temporarily not ready", valid_payload))
    waits: list[float] = []

    fetched = fetch_artifact(
        spec,
        opener=lambda *_args, **_kwargs: FakeResponse(next(payloads)),
        attempts=2,
        backoff_seconds=0.25,
        sleeper=waits.append,
        retrieved_at=datetime(2026, 8, 12, 12, 1, tzinfo=UTC),
    )

    assert fetched.payload == valid_payload
    assert waits == [0.25]


def test_himawari_fetch_rejects_truncated_bzip2_and_accepts_complete_stream() -> None:
    spec = plan_himawari_ord(
        datetime(2026, 8, 12, 7, tzinfo=UTC), lookback_hours=1
    )[-1]
    valid = bz2.compress(b"fixture AHI segment bytes") + bz2.compress(
        b"second concatenated HSD block"
    )
    fetched = fetch_artifact(
        spec,
        opener=lambda *_args, **_kwargs: FakeResponse(
            valid, {"Content-Length": str(len(valid))}
        ),
        attempts=1,
        retrieved_at=datetime(2026, 8, 12, 7, 1, tzinfo=UTC),
    )
    assert fetched.payload == valid

    with pytest.raises(ValueError, match="truncated|invalid bzip2"):
        fetch_artifact(
            spec,
            opener=lambda *_args, **_kwargs: FakeResponse(b"BZh9truncated-not-a-stream"),
            attempts=1,
        )
    with pytest.raises(ValueError, match="unexpected trailing"):
        fetch_artifact(
            spec,
            opener=lambda *_args, **_kwargs: FakeResponse(valid + b"garbage"),
            attempts=1,
        )


def test_fetch_checks_declared_content_length() -> None:
    spec = plan_gefs_ord(
        datetime(2026, 8, 12, 12, tzinfo=UTC),
        horizons_hours=(6,),
        members=("c00",),
    )[0]
    valid_payload = _fake_gefs_payload(spec)
    with pytest.raises(ValueError, match="Content-Length"):
        fetch_artifact(
            spec,
            opener=lambda *_args, **_kwargs: FakeResponse(
                valid_payload, {"Content-Length": "999"}
            ),
            attempts=1,
        )


def test_gefs_fetch_rejects_a_valid_file_for_the_wrong_member() -> None:
    as_of = datetime(2026, 8, 12, 12, tzinfo=UTC)
    control, perturbation = plan_gefs_ord(
        as_of,
        horizons_hours=(6,),
        members=("c00", "p01"),
    )
    control_payload = _fake_gefs_payload(control)

    with pytest.raises(ValueError, match="ensemble member"):
        fetch_artifact(
            perturbation,
            opener=lambda *_args, **_kwargs: FakeResponse(control_payload),
            attempts=1,
        )
