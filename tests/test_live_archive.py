from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ordcast.archive import ArchiveStore
from ordcast.live_sources import ArtifactSpec, FetchedArtifact
from pipelines.ingest.live_archive import (
    LivePlan,
    RecordResult,
    create_plan,
    read_plan,
    record_plan,
    success_ratios,
    write_plan,
    write_run_report,
)

UTC = timezone.utc


def _spec(source: str, source_key: str, event_time: datetime) -> ArtifactSpec:
    if source == "bom-kununurra-observations":
        media_type = "application/json"
    else:
        media_type = "application/x-grib2"
    return ArtifactSpec(
        source=source,
        source_key=source_key,
        url=f"https://example.test/{source_key}",
        event_time=event_time,
        issue_time=(event_time if source == "noaa-gefs-0p25" else None),
        valid_time=event_time,
        media_type=media_type,
        license_notice="test-only fixture",
        max_bytes=1_000,
        metadata={"fixture": True},
    )


def _fetched(spec: ArtifactSpec, payload: bytes, retrieved_at: datetime) -> FetchedArtifact:
    return FetchedArtifact(
        spec=spec,
        payload=payload,
        retrieved_at=retrieved_at,
        sha256=hashlib.sha256(payload).hexdigest(),
        response_headers={"Content-Type": spec.media_type, "Authorization": "not-kept"},
    )


def test_default_starter_plan_has_three_sources_and_bounded_artifacts() -> None:
    plan = create_plan(
        datetime(2026, 8, 12, 12, 34, tzinfo=UTC),
        created_at=datetime(2026, 8, 12, 12, 35, tzinfo=UTC),
    )

    assert plan.sources == ("bom", "gefs", "himawari")
    assert len(plan.artifacts) == 1 + (31 * 41) + 42
    assert len({artifact.source_key for artifact in plan.artifacts}) == len(plan.artifacts)
    assert max(artifact.max_bytes for artifact in plan.artifacts) <= 15_000_000
    assert [artifact.source for artifact in plan.artifacts[:3]] == [
        "bom-kununurra-observations",
        "noaa-gefs-0p25",
        "noaa-jma-himawari9",
    ]


def test_plan_round_trip_checks_canonical_bytes_and_filename_digest(tmp_path: Path) -> None:
    plan = create_plan(
        datetime(2026, 8, 12, 12, 34, tzinfo=UTC),
        sources=("bom",),
        created_at=datetime(2026, 8, 12, 12, 35, tzinfo=UTC),
    )
    path = write_plan(plan, tmp_path)
    assert read_plan(path).to_dict() == plan.to_dict()

    document = json.loads(path.read_text())
    document["created_at"] = "2026-08-12T12:36:00Z"
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(ValueError, match="filename does not match"):
        read_plan(path)


def test_record_plan_archives_success_and_preserves_independent_failure(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 12, 12, tzinfo=UTC)
    bom = _spec("bom-kununurra-observations", "bom/request.json", as_of)
    gefs = _spec("noaa-gefs-0p25", "gefs/member.grib2", as_of)
    plan = LivePlan(
        as_of=as_of,
        created_at=as_of,
        sources=("bom", "gefs"),
        artifacts=(bom, gefs),
    )
    retrieved_at = datetime(2026, 8, 12, 12, 1, tzinfo=UTC)

    def fetcher(spec: ArtifactSpec) -> FetchedArtifact:
        if spec is gefs:
            raise OSError("fixture upstream unavailable")
        return _fetched(spec, b'{"validated":"fixture"}', retrieved_at)

    results = record_plan(plan, tmp_path, workers=2, fetcher=fetcher)

    assert [result.succeeded for result in results] == [True, False]
    assert "upstream unavailable" in (results[1].error or "")
    record = results[0].record
    assert record is not None
    assert record.available_at == retrieved_at
    assert record.valid_at == as_of
    assert record.metadata["source_key"] == "bom/request.json"
    assert record.metadata["source_adapter_version"] == 1
    assert "authorization" not in record.metadata["response_headers"]
    store = ArchiveStore(tmp_path)
    assert store.read_blob(record) == b'{"validated":"fixture"}'

    report = write_run_report(
        tmp_path,
        plan,
        results,
        completed_at=datetime(2026, 8, 12, 12, 2, tzinfo=UTC),
    )
    contents = json.loads(report.read_text())
    assert contents["recorded_artifacts"] == 1
    assert contents["failed_artifacts"] == 1
    assert len(contents["plan_sha256"]) == 64
    assert success_ratios(results) == {
        "bom-kununurra-observations": 1.0,
        "noaa-gefs-0p25": 0.0,
        "overall": 0.5,
    }


def test_record_plan_rejects_unbounded_parallelism(tmp_path: Path) -> None:
    plan = create_plan(
        datetime(2026, 8, 12, 12, tzinfo=UTC),
        sources=("bom",),
        created_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="1 to 8"):
        record_plan(plan, tmp_path, workers=100)


def test_live_plan_rejects_source_label_mismatch() -> None:
    as_of = datetime(2026, 8, 12, 12, tzinfo=UTC)
    bom = _spec("bom-kununurra-observations", "bom/request.json", as_of)
    with pytest.raises(ValueError, match="do not match"):
        LivePlan(
            as_of=as_of,
            created_at=as_of,
            sources=("gefs",),
            artifacts=(bom,),
        )


def test_cycle_deadline_reports_every_unstarted_artifact(tmp_path: Path) -> None:
    plan = create_plan(
        datetime(2026, 8, 12, 12, tzinfo=UTC),
        sources=("bom",),
        created_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
    )
    ticks = iter((0.0, 2.0))

    results = record_plan(
        plan,
        tmp_path,
        deadline_seconds=1.0,
        monotonic=lambda: next(ticks),
        fetcher=lambda _spec: pytest.fail("deadline must prevent the download"),
    )

    assert len(results) == len(plan.artifacts)
    assert all(not result.succeeded for result in results)
    assert all("DeadlineExceeded" in (result.error or "") for result in results)


def test_success_ratios_expose_concentrated_gefs_lead_gaps() -> None:
    successful_record = object()
    results = [
        # The ratio helper needs only record presence; persistence is tested above.
        RecordResult(
            "noaa-gefs-0p25",
            f"noaa-gefs/cycle/{member}/f006-ord.grib2",
            record=successful_record,  # type: ignore[arg-type]
        )
        for member in ("c00", "p01")
    ] + [
        RecordResult(
            "noaa-gefs-0p25",
            "noaa-gefs/cycle/p02/f012-ord.grib2",
            error="missing",
        )
    ]

    ratios = success_ratios(results)

    assert ratios["noaa-gefs-0p25"] == pytest.approx(2 / 3)
    assert ratios["noaa-gefs-0p25/f006"] == 1.0
    assert ratios["noaa-gefs-0p25/f012"] == 0.0
