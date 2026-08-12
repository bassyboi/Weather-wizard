import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ordcast import local_station
from ordcast.local_station import (
    CycleAlreadyRunningError,
    local_cycle_lock,
    local_station_status,
    run_local_cycle,
    source_names,
)


UTC = timezone.utc


def _write_report(
    root: Path,
    *,
    completed_at: datetime,
    planned: int = 10,
    recorded: int = 10,
) -> Path:
    report = {
        "version": 1,
        "plan_as_of": completed_at.isoformat().replace("+00:00", "Z"),
        "plan_sha256": "0" * 64,
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "planned_artifacts": planned,
        "recorded_artifacts": recorded,
        "failed_artifacts": planned - recorded,
        "success_ratios": {
            "bom-kununurra-observations": recorded / planned,
            "overall": recorded / planned,
        },
        "results": [
            {
                "source_key": f"key-{index}",
                "source": "bom-kununurra-observations",
                "status": "recorded" if index < recorded else "failed",
                "record_id": f"record-{index}" if index < recorded else None,
                "sha256": "1" * 64 if index < recorded else None,
                "error": None if index < recorded else "test failure",
            }
            for index in range(planned)
        ],
    }
    directory = root / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(report, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path = directory / f"live-{completed_at:%Y%m%dT%H%M%SZ}-{digest[:16]}.json"
    path.write_text(payload, encoding="utf-8")
    return path


def test_local_cycle_uses_one_persistent_root_and_reports_threshold(monkeypatch, tmp_path):
    plan = SimpleNamespace(artifacts=("a", "b"))
    results = [SimpleNamespace(succeeded=True), SimpleNamespace(succeeded=False)]
    calls = []

    monkeypatch.setattr(local_station, "create_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        local_station,
        "write_plan",
        lambda supplied, root: calls.append(("plan", supplied, root))
        or root / "plans" / "plan.json",
    )
    monkeypatch.setattr(
        local_station,
        "record_plan",
        lambda supplied, root, **kwargs: calls.append(("record", supplied, root))
        or results,
    )
    monkeypatch.setattr(
        local_station,
        "write_run_report",
        lambda root, supplied, observed: calls.append(("report", supplied, root))
        or root / "runs" / "report.json",
    )
    monkeypatch.setattr(
        local_station,
        "success_ratios",
        lambda observed: {"source": 0.5, "overall": 0.5},
    )

    result = run_local_cycle(
        tmp_path / "archive",
        as_of=datetime(2026, 8, 12, tzinfo=UTC),
        sources=("bom",),
        min_success_ratio=0.9,
    )

    assert [call[0] for call in calls] == ["plan", "record", "report"]
    assert all(call[2] == tmp_path / "archive" for call in calls)
    assert result.recorded_artifacts == 1
    assert result.planned_artifacts == 2
    assert not result.complete


def test_loose_manual_exit_threshold_cannot_mark_archive_continuity_complete(
    monkeypatch, tmp_path
):
    issue = datetime(2026, 8, 12, tzinfo=UTC)
    plan = SimpleNamespace(artifacts=("a", "b"), as_of=issue)
    results = [SimpleNamespace(succeeded=True), SimpleNamespace(succeeded=False)]
    monkeypatch.setattr(local_station, "create_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        local_station, "write_plan", lambda *args, **kwargs: tmp_path / "plan.json"
    )
    monkeypatch.setattr(
        local_station, "record_plan", lambda *args, **kwargs: results
    )
    monkeypatch.setattr(
        local_station,
        "write_run_report",
        lambda *args, **kwargs: tmp_path / "report.json",
    )
    monkeypatch.setattr(
        local_station,
        "success_ratios",
        lambda _results: {"source": 0.5, "overall": 0.5},
    )

    result = run_local_cycle(tmp_path, as_of=issue, min_success_ratio=0.5)

    assert result.complete
    state = local_station._read_continuity(tmp_path)
    assert state is not None
    assert state["break_kind"] == "incomplete_cycle"


def test_local_cycle_lock_refuses_overlap(tmp_path):
    with local_cycle_lock(tmp_path):
        with pytest.raises(CycleAlreadyRunningError):
            with local_cycle_lock(tmp_path):
                pass


def test_status_reports_healthy_latest_cycle_and_disk(tmp_path):
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    older = _write_report(tmp_path, completed_at=now - timedelta(hours=7))
    latest = _write_report(tmp_path, completed_at=now - timedelta(hours=1))

    status = local_station_status(tmp_path, now=now)

    assert status.state == "healthy"
    assert status.latest_report == latest
    assert status.latest_report != older
    assert status.latest_age_hours == pytest.approx(1.0)
    assert status.recorded_artifacts == 10
    assert status.disk_free_bytes > 0
    assert status.to_dict()["state"] == "healthy"


def test_status_distinguishes_degraded_stale_and_not_started(tmp_path):
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    empty = local_station_status(tmp_path / "empty", now=now)
    assert empty.state == "not_started"

    degraded_root = tmp_path / "degraded"
    _write_report(degraded_root, completed_at=now, planned=10, recorded=9)
    assert local_station_status(degraded_root, now=now).state == "degraded"

    stale_root = tmp_path / "stale"
    _write_report(stale_root, completed_at=now - timedelta(hours=9))
    assert local_station_status(stale_root, now=now).state == "stale"


def test_status_skips_corrupt_reports_but_surfaces_fault(tmp_path):
    report_dir = tmp_path / "runs"
    report_dir.mkdir(parents=True)
    (report_dir / "live-corrupt.json").write_text("{", encoding="utf-8")

    status = local_station_status(
        tmp_path, now=datetime(2026, 8, 12, tzinfo=UTC)
    )

    assert status.state == "fault"
    assert "unreadable" in status.message


def test_status_degrades_before_the_data_drive_fills(monkeypatch, tmp_path):
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    _write_report(tmp_path, completed_at=now)
    monkeypatch.setattr(
        local_station.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=500_000_000_000, free=10_000_000_000),
    )

    status = local_station_status(tmp_path, now=now)

    assert status.state == "degraded"
    assert "free-space" in status.message


def test_status_rejects_future_and_internally_inconsistent_reports(tmp_path):
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    future_root = tmp_path / "future"
    _write_report(future_root, completed_at=now + timedelta(hours=1))
    assert local_station_status(future_root, now=now).state == "fault"

    bad_root = tmp_path / "bad-ratio"
    path = _write_report(bad_root, completed_at=now)
    report = json.loads(path.read_text(encoding="utf-8"))
    report["success_ratios"]["overall"] = 0.0
    payload = (
        json.dumps(report, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    replacement = path.with_name(f"live-{now:%Y%m%dT%H%M%SZ}-{digest[:16]}.json")
    path.unlink()
    replacement.write_text(payload, encoding="utf-8")
    assert local_station_status(bad_root, now=now).state == "fault"


def test_status_surfaces_an_offline_cycle_gap_after_a_good_latest_run(tmp_path):
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    _write_report(tmp_path, completed_at=now - timedelta(hours=9))
    _write_report(tmp_path, completed_at=now)

    status = local_station_status(tmp_path, now=now)

    assert status.state == "degraded"
    assert "backfill" in status.message


def test_persisted_cycle_gap_does_not_disappear_after_a_later_good_run(tmp_path):
    first = datetime(2026, 8, 12, 0, tzinfo=UTC)
    local_station._update_continuity(
        tmp_path, plan_as_of=first, complete=True
    )
    local_station._update_continuity(
        tmp_path, plan_as_of=first + timedelta(hours=9), complete=True
    )
    local_station._update_continuity(
        tmp_path, plan_as_of=first + timedelta(hours=15), complete=True
    )
    _write_report(tmp_path, completed_at=first + timedelta(hours=15))

    status = local_station_status(
        tmp_path, now=first + timedelta(hours=15, minutes=5)
    )

    assert status.state == "degraded"
    assert "backfill" in status.message


def test_complete_same_slot_retry_repairs_incomplete_continuity(tmp_path):
    issue = datetime(2026, 8, 12, 0, tzinfo=UTC)
    local_station._update_continuity(
        tmp_path, plan_as_of=issue, complete=False
    )
    local_station._update_continuity(
        tmp_path, plan_as_of=issue, complete=True
    )
    _write_report(tmp_path, completed_at=issue)

    status = local_station_status(tmp_path, now=issue + timedelta(minutes=5))

    assert status.state == "healthy"


def test_sources_are_strict_and_unique():
    assert source_names("bom, GEFS,himawari") == ("bom", "gefs", "himawari")
    with pytest.raises(ValueError, match="unknown"):
        source_names("bom,ecmwf")
    with pytest.raises(ValueError, match="unique"):
        source_names("bom,bom")
    with pytest.raises(ValueError, match="strings"):
        source_names(("bom", 1))
