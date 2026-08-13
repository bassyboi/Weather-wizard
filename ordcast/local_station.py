"""Run and inspect the append-only OrdCast archive on one local computer.

The live-source recorder is intentionally reusable in cloud and local deployments.
This module adds the small amount of local-machine coordination that a Windows Task
Scheduler job needs: one persistent archive root, a cross-process cycle lock, and a
bounded status reader.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import shutil
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from pipelines.ingest.live_archive import (
    DEFAULT_SOURCES,
    LivePlan,
    create_plan,
    record_plan,
    success_ratios,
    write_plan,
    write_run_report,
)


UTC = timezone.utc
MAX_RUN_REPORT_BYTES = 5_000_000
MAX_CONTINUITY_BYTES = 16_384
DEFAULT_STALE_AFTER_HOURS = 8.0
DEFAULT_MINIMUM_FREE_BYTES = 25_000_000_000
DEFAULT_MAX_CYCLE_GAP_HOURS = 7.0
LOCK_FILENAME = ".ordcast-cycle.lock"
CONTINUITY_FILENAME = "local-continuity.json"


class CycleAlreadyRunningError(RuntimeError):
    """Raised when another process already owns the local collection lock."""


@dataclass(frozen=True)
class LocalCycleResult:
    """Result of one local plan/download/archive cycle."""

    plan: LivePlan
    plan_path: Path
    report_path: Path
    planned_artifacts: int
    recorded_artifacts: int
    success_ratios: Mapping[str, float]
    required_success_ratio: float

    @property
    def complete(self) -> bool:
        return all(
            ratio >= self.required_success_ratio
            for ratio in self.success_ratios.values()
        )


@dataclass(frozen=True)
class LocalStationStatus:
    """Small operational status snapshot suitable for a console or dashboard."""

    state: str
    archive_root: Path
    checked_at: datetime
    latest_report: Path | None
    latest_completed_at: datetime | None
    latest_age_hours: float | None
    planned_artifacts: int | None
    recorded_artifacts: int | None
    failed_artifacts: int | None
    success_ratios: Mapping[str, float]
    disk_free_bytes: int
    disk_total_bytes: int
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "state": self.state,
            "archive_root": str(self.archive_root),
            "checked_at": _stamp(self.checked_at),
            "latest_report": (
                None if self.latest_report is None else str(self.latest_report)
            ),
            "latest_completed_at": (
                None
                if self.latest_completed_at is None
                else _stamp(self.latest_completed_at)
            ),
            "latest_age_hours": self.latest_age_hours,
            "planned_artifacts": self.planned_artifacts,
            "recorded_artifacts": self.recorded_artifacts,
            "failed_artifacts": self.failed_artifacts,
            "success_ratios": dict(self.success_ratios),
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "message": self.message,
        }


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    if _stamp(parsed) != value:
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    return parsed


def _validate_ratio(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= 1
    ):
        raise ValueError(f"{name} must be greater than 0 and at most 1")
    return float(value)


def _continuity_path(root: Path) -> Path:
    return Path(root) / "state" / CONTINUITY_FILENAME


def _read_continuity(root: Path) -> dict[str, object] | None:
    path = _continuity_path(root)
    if not path.exists():
        return None
    with path.open("rb") as handle:
        payload = handle.read(MAX_CONTINUITY_BYTES + 1)
    if len(payload) > MAX_CONTINUITY_BYTES:
        raise ValueError("local continuity state is too large")
    try:
        state = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("local continuity state is not valid UTF-8 JSON") from error
    required = {
        "schema_version",
        "latest_plan_as_of",
        "continuity_broken_at",
        "break_kind",
        "break_plan_as_of",
    }
    if not isinstance(state, dict) or set(state) != required:
        raise ValueError("local continuity state has an unsupported schema")
    if state["schema_version"] != 1:
        raise ValueError("local continuity state has an unsupported version")
    _parse_stamp(state["latest_plan_as_of"], "latest_plan_as_of")
    nullable_stamps = ("continuity_broken_at", "break_plan_as_of")
    for name in nullable_stamps:
        if state[name] is not None:
            _parse_stamp(state[name], name)
    kind = state["break_kind"]
    if kind not in {None, "cycle_gap", "incomplete_cycle"}:
        raise ValueError("local continuity state has an invalid break_kind")
    null_fields = [state[name] is None for name in (*nullable_stamps, "break_kind")]
    if any(null_fields) and not all(null_fields):
        raise ValueError("local continuity break fields must all be null or populated")
    return state


def _write_continuity(root: Path, state: Mapping[str, object]) -> Path:
    path = _continuity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_CONTINUITY_BYTES:
        raise ValueError("local continuity state is too large")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _update_continuity(
    root: Path,
    *,
    plan_as_of: datetime,
    complete: bool,
    max_cycle_gap_hours: float = DEFAULT_MAX_CYCLE_GAP_HOURS,
) -> None:
    """Persist a detected gap until an explicit retry repairs that same cycle."""

    current = plan_as_of.astimezone(UTC) if plan_as_of.tzinfo else None
    if current is None:
        raise ValueError("plan_as_of must include a timezone")
    existing = _read_continuity(root)
    previous = (
        None
        if existing is None
        else _parse_stamp(existing["latest_plan_as_of"], "latest_plan_as_of")
    )
    broken_at = None if existing is None else existing["continuity_broken_at"]
    break_kind = None if existing is None else existing["break_kind"]
    break_plan = None if existing is None else existing["break_plan_as_of"]

    # A complete retry of the exact incomplete slot repairs that slot. A time gap is
    # not cleared automatically because only a dedicated backfill can restore it.
    if (
        complete
        and break_kind == "incomplete_cycle"
        and break_plan == _stamp(current)
    ):
        broken_at = break_kind = break_plan = None

    if broken_at is None and previous is not None:
        gap_hours = (current - previous).total_seconds() / 3_600.0
        if gap_hours > float(max_cycle_gap_hours):
            broken_at = _stamp(current)
            break_kind = "cycle_gap"
            break_plan = _stamp(current)
    if broken_at is None and not complete:
        broken_at = _stamp(current)
        break_kind = "incomplete_cycle"
        break_plan = _stamp(current)

    latest = current if previous is None or current > previous else previous
    _write_continuity(
        root,
        {
            "schema_version": 1,
            "latest_plan_as_of": _stamp(latest),
            "continuity_broken_at": broken_at,
            "break_kind": break_kind,
            "break_plan_as_of": break_plan,
        },
    )


def _lock_byte(handle: object) -> None:
    # Inspect the length without reading byte zero. Windows denies a second handle's
    # read of an already locked region before ``msvcrt.locking`` gets a chance to
    # return the expected overlap error.
    handle.seek(0, os.SEEK_END)  # type: ignore[attr-defined]
    if handle.tell() == 0:  # type: ignore[attr-defined]
        handle.seek(0)  # type: ignore[attr-defined]
        handle.write(b"0")  # type: ignore[attr-defined]
        handle.flush()  # type: ignore[attr-defined]
        os.fsync(handle.fileno())  # type: ignore[attr-defined]
    handle.seek(0)  # type: ignore[attr-defined]


@contextmanager
def local_cycle_lock(archive_root: Path) -> Iterator[None]:
    """Hold a non-blocking OS lock without relying on a removable PID file."""

    root = Path(archive_root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_FILENAME
    with lock_path.open("a+b") as handle:
        _lock_byte(handle)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise CycleAlreadyRunningError(
                    f"another OrdCast cycle is already using {root}"
                ) from error
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise CycleAlreadyRunningError(
                    f"another OrdCast cycle is already using {root}"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_local_cycle(
    archive_root: Path,
    *,
    as_of: datetime | None = None,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    workers: int = 4,
    deadline_seconds: float = 2_700.0,
    min_success_ratio: float = 1.0,
) -> LocalCycleResult:
    """Plan and record one cycle into a persistent local archive root."""

    threshold = _validate_ratio(min_success_ratio, "min_success_ratio")
    root = Path(archive_root)
    observed_at = as_of or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise ValueError("as_of must include a timezone")

    with local_cycle_lock(root):
        plan = create_plan(observed_at, sources=sources)
        plan_path = write_plan(plan, root)
        results = record_plan(
            plan,
            root,
            workers=workers,
            deadline_seconds=deadline_seconds,
        )
        report_path = write_run_report(root, plan, results)
        ratios = success_ratios(results)
        cycle_result = LocalCycleResult(
            plan=plan,
            plan_path=plan_path,
            report_path=report_path,
            planned_artifacts=len(results),
            recorded_artifacts=sum(result.succeeded for result in results),
            success_ratios=ratios,
            required_success_ratio=threshold,
        )
        if tuple(sources) == tuple(DEFAULT_SOURCES):
            _update_continuity(
                root,
                plan_as_of=plan.as_of,
                # Continuity requires every planned source artifact even when a
                # manual caller deliberately chooses a looser process exit threshold.
                complete=all(
                    math.isclose(float(ratio), 1.0, rel_tol=0, abs_tol=1e-12)
                    for ratio in ratios.values()
                ),
            )
    return cycle_result


def _read_report(path: Path) -> tuple[bytes, dict[str, object]]:
    with path.open("rb") as handle:
        payload = handle.read(MAX_RUN_REPORT_BYTES + 1)
    if len(payload) > MAX_RUN_REPORT_BYTES:
        raise ValueError(f"run report exceeds {MAX_RUN_REPORT_BYTES} bytes: {path}")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"run report is not valid UTF-8 JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"run report must be a JSON object: {path}")
    return payload, value


def _report_summary(path: Path) -> tuple[datetime, dict[str, object]]:
    payload, report = _read_report(path)
    required = {
        "version",
        "plan_as_of",
        "plan_sha256",
        "completed_at",
        "planned_artifacts",
        "recorded_artifacts",
        "failed_artifacts",
        "success_ratios",
        "results",
    }
    if not isinstance(report, dict) or set(report) != required or report["version"] != 1:
        raise ValueError(f"run report has an unsupported schema: {path}")
    canonical = (
        json.dumps(
            report, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    if payload != canonical:
        raise ValueError(f"run report is not canonical JSON: {path}")
    digest = hashlib.sha256(payload).hexdigest()
    if not path.name.endswith(f"-{digest[:16]}.json"):
        raise ValueError(f"run report filename does not match its SHA-256: {path}")
    _parse_stamp(report["plan_as_of"], "plan_as_of")
    plan_digest = report["plan_sha256"]
    if (
        not isinstance(plan_digest, str)
        or len(plan_digest) != 64
        or any(character not in "0123456789abcdef" for character in plan_digest)
    ):
        raise ValueError("run report plan_sha256 must be lowercase hexadecimal")
    completed_at = _parse_stamp(report["completed_at"], "completed_at")
    for key in ("planned_artifacts", "recorded_artifacts", "failed_artifacts"):
        value = report[key]
        minimum = 1 if key == "planned_artifacts" else 0
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"run report {key} must be a non-negative integer")
    if report["recorded_artifacts"] + report["failed_artifacts"] != report["planned_artifacts"]:
        raise ValueError("run report artifact totals are inconsistent")
    results = report["results"]
    if not isinstance(results, list) or len(results) != report["planned_artifacts"]:
        raise ValueError("run report results do not match planned_artifacts")
    observed_recorded = 0
    for result in results:
        if not isinstance(result, dict) or set(result) != {
            "source_key",
            "source",
            "status",
            "record_id",
            "sha256",
            "error",
        }:
            raise ValueError("run report result has an unsupported schema")
        if result["status"] not in {"recorded", "failed"}:
            raise ValueError("run report result status is invalid")
        observed_recorded += int(result["status"] == "recorded")
    if observed_recorded != report["recorded_artifacts"]:
        raise ValueError("run report result statuses do not match recorded_artifacts")
    ratios = report["success_ratios"]
    if not isinstance(ratios, dict) or not ratios:
        raise ValueError("run report success_ratios must be a non-empty object")
    for name, ratio in ratios.items():
        if not isinstance(name, str) or not name:
            raise ValueError("run report success ratio names must be non-empty strings")
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(float(ratio))
            or not 0 <= float(ratio) <= 1
        ):
            raise ValueError("run report success ratios must be from zero to one")
    expected_overall = report["recorded_artifacts"] / report["planned_artifacts"]
    if not math.isclose(float(ratios.get("overall", -1)), expected_overall):
        raise ValueError("run report overall success ratio is inconsistent")
    return completed_at, report


def _disk_anchor(path: Path) -> Path:
    candidate = path.absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def local_station_status(
    archive_root: Path,
    *,
    now: datetime | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
    max_cycle_gap_hours: float = DEFAULT_MAX_CYCLE_GAP_HOURS,
) -> LocalStationStatus:
    """Read the newest valid report and classify local collection health."""

    if (
        isinstance(stale_after_hours, bool)
        or not isinstance(stale_after_hours, (int, float))
        or not math.isfinite(float(stale_after_hours))
        or stale_after_hours <= 0
    ):
        raise ValueError("stale_after_hours must be positive and finite")
    if (
        not isinstance(minimum_free_bytes, int)
        or isinstance(minimum_free_bytes, bool)
        or minimum_free_bytes < 0
    ):
        raise ValueError("minimum_free_bytes must be a non-negative integer")
    if (
        isinstance(max_cycle_gap_hours, bool)
        or not isinstance(max_cycle_gap_hours, (int, float))
        or not math.isfinite(float(max_cycle_gap_hours))
        or max_cycle_gap_hours <= 0
    ):
        raise ValueError("max_cycle_gap_hours must be positive and finite")
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("now must include a timezone")
    checked_at = checked_at.astimezone(UTC)
    root = Path(archive_root).absolute()
    usage = shutil.disk_usage(_disk_anchor(root))
    try:
        continuity = _read_continuity(root)
    except (ValueError, OSError, RecursionError, OverflowError):
        continuity = {"break_kind": "invalid"}
    report_paths = sorted((root / "runs").glob("live-*.json"))

    valid_reports: list[tuple[datetime, Path, dict[str, object]]] = []
    previous_plan_at: datetime | None = None
    invalid_count = 0
    newest_plan_prefix: str | None = None
    previous_search_index = -1
    for reverse_index, path in enumerate(reversed(report_paths)):
        parts = path.name.split("-", maxsplit=2)
        plan_prefix = parts[1] if len(parts) == 3 else None
        if newest_plan_prefix is not None and plan_prefix != newest_plan_prefix:
            previous_search_index = reverse_index
            break
        try:
            completed_at, report = _report_summary(path)
        except (ValueError, OSError, RecursionError, OverflowError, ZeroDivisionError):
            invalid_count += 1
            continue
        newest_plan_prefix = plan_prefix
        valid_reports.append((completed_at, path, report))

    if not valid_reports:
        message = "No completed local collection cycle was found."
        if invalid_count:
            message += f" {invalid_count} unreadable report(s) need inspection."
        if usage.free < minimum_free_bytes:
            message += " The data drive is below the minimum free-space reserve."
        return LocalStationStatus(
            state="not_started" if not invalid_count else "fault",
            archive_root=root,
            checked_at=checked_at,
            latest_report=None,
            latest_completed_at=None,
            latest_age_hours=None,
            planned_artifacts=None,
            recorded_artifacts=None,
            failed_artifacts=None,
            success_ratios={},
            disk_free_bytes=usage.free,
            disk_total_bytes=usage.total,
            message=message,
        )

    completed_at, latest_path, latest = max(valid_reports, key=lambda item: item[0])
    latest_plan_at = _parse_stamp(latest["plan_as_of"], "plan_as_of")
    # Find the newest valid report from the preceding plan slot. This is bounded in
    # the normal case (one file) and exposes a PC/offline gap that a later good cycle
    # would otherwise hide.
    remaining_paths = list(reversed(report_paths))
    if previous_search_index >= 0:
        remaining_paths = remaining_paths[previous_search_index:]
    else:
        remaining_paths = []
    for path in remaining_paths:
        try:
            _completed, report = _report_summary(path)
        except (ValueError, OSError, RecursionError, OverflowError, ZeroDivisionError):
            continue
        candidate = _parse_stamp(report["plan_as_of"], "plan_as_of")
        if candidate < latest_plan_at:
            previous_plan_at = candidate
            break
    age_hours = (checked_at - completed_at).total_seconds() / 3_600.0
    failed = int(latest["failed_artifacts"])
    ratios = {
        str(name): float(ratio)
        for name, ratio in dict(latest["success_ratios"]).items()
    }
    cycle_gap_hours = (
        None
        if previous_plan_at is None
        else (latest_plan_at - previous_plan_at).total_seconds() / 3_600.0
    )
    continuity_break = None if continuity is None else continuity.get("break_kind")
    if age_hours < -0.25:
        state = "fault"
        message = "Latest cycle completion time is implausibly in the future."
    elif continuity_break == "invalid":
        state = "fault"
        message = "Local continuity state is unreadable and needs inspection."
    elif cycle_gap_hours is not None and cycle_gap_hours > float(max_cycle_gap_hours):
        state = "degraded"
        message = (
            f"There is a {cycle_gap_hours:.1f}-hour gap between collection cycles; "
            "satellite backfill is required."
        )
    elif age_hours > float(stale_after_hours):
        state = "stale"
        message = f"Last completed cycle is {age_hours:.1f} hours old."
    elif usage.free < minimum_free_bytes:
        state = "degraded"
        message = "The data drive is below the minimum free-space reserve."
    elif failed:
        state = "degraded"
        message = f"Latest cycle recorded with {failed} failed artifact(s)."
    elif continuity_break == "cycle_gap":
        state = "degraded"
        message = "A previously detected collection gap still needs explicit backfill."
    elif continuity_break == "incomplete_cycle":
        state = "degraded"
        message = "A previously incomplete collection cycle has not been fully retried."
    elif invalid_count:
        state = "degraded"
        message = f"Latest cycle is complete, but {invalid_count} report(s) are unreadable."
    else:
        state = "healthy"
        message = "Latest local collection cycle completed without recorded gaps."

    return LocalStationStatus(
        state=state,
        archive_root=root,
        checked_at=checked_at,
        latest_report=latest_path,
        latest_completed_at=completed_at,
        latest_age_hours=age_hours,
        planned_artifacts=int(latest["planned_artifacts"]),
        recorded_artifacts=int(latest["recorded_artifacts"]),
        failed_artifacts=failed,
        success_ratios=ratios,
        disk_free_bytes=usage.free,
        disk_total_bytes=usage.total,
        message=message,
    )


def source_names(value: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize a CLI source list without accepting unknown upstream labels."""

    items = value.split(",") if isinstance(value, str) else value
    if not all(isinstance(item, str) for item in items):
        raise ValueError("sources must contain only strings")
    sources = tuple(item.strip().lower() for item in items if item.strip())
    unknown = sorted(set(sources) - set(DEFAULT_SOURCES))
    if not sources or len(set(sources)) != len(sources):
        raise ValueError("sources must be a non-empty unique list")
    if unknown:
        raise ValueError(f"unknown local source(s): {', '.join(unknown)}")
    return sources
