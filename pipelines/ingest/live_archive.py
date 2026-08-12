"""Plan and record a bounded live-data cycle for the Ord district.

Examples::

    python -m pipelines.ingest.live_archive plan --output data/live-archive
    python -m pipelines.ingest.live_archive record --output data/live-archive

``plan`` freezes every remote URL and issue/scan time.  ``record`` consumes that
frozen plan, validates each response, and passes exact bytes to the content-addressed
archive.  Running them as separate steps makes a scheduled job auditable before it
downloads anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from ordcast.archive import ArchiveRecord, ArchiveStore
from ordcast.live_sources import (
    ArtifactSpec,
    FetchedArtifact,
    fetch_artifact,
    plan_live_artifacts,
)

UTC = timezone.utc
PLAN_VERSION = 1
MAX_PLAN_BYTES = 5_000_000
MAX_PLAN_ARTIFACTS = 1_500
DEFAULT_SOURCES = ("bom", "gefs", "himawari")
PRODUCT_BY_SOURCE = {
    "bom-kununurra-observations": "kununurra-aero-72h-json",
    "noaa-gefs-0p25": "ord-regional-subset",
    "noaa-jma-himawari9": "ahi-b13-segment-07",
}
PLAN_SOURCE_NAMES = {
    "bom": "bom-kununurra-observations",
    "gefs": "noaa-gefs-0p25",
    "himawari": "noaa-jma-himawari9",
}
GEFS_LEAD_KEY = re.compile(r"/f(\d{3})-ord\.grib2\Z")


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be a canonical UTC string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("timestamp is invalid") from error
    if _stamp(parsed) != value:
        raise ValueError("timestamp must be a canonical UTC string")
    return parsed


def _canonical_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode() + b"\n"
    except (TypeError, ValueError) as error:
        raise ValueError("live recorder JSON is not serializable") from error
    return encoded


@dataclass(frozen=True)
class LivePlan:
    as_of: datetime
    created_at: datetime
    sources: tuple[str, ...]
    artifacts: tuple[ArtifactSpec, ...]
    version: int = PLAN_VERSION

    def __post_init__(self) -> None:
        if self.version != PLAN_VERSION:
            raise ValueError(f"unsupported live plan version {self.version}")
        as_of = self.as_of.astimezone(UTC) if self.as_of.tzinfo else None
        created_at = self.created_at.astimezone(UTC) if self.created_at.tzinfo else None
        if as_of is None or created_at is None:
            raise ValueError("plan timestamps must be timezone-aware")
        sources = tuple(self.sources)
        artifacts = tuple(self.artifacts)
        if not sources or len(set(sources)) != len(sources):
            raise ValueError("plan sources must be non-empty and unique")
        if not artifacts or len(artifacts) > MAX_PLAN_ARTIFACTS:
            raise ValueError(
                f"plan must contain from 1 to {MAX_PLAN_ARTIFACTS} artifacts"
            )
        keys = [artifact.source_key for artifact in artifacts]
        if len(set(keys)) != len(keys):
            raise ValueError("plan artifact keys must be unique")
        expected_artifact_sources = {PLAN_SOURCE_NAMES.get(source) for source in sources}
        if None in expected_artifact_sources or {
            artifact.source for artifact in artifacts
        } != expected_artifact_sources:
            raise ValueError("plan source labels do not match its artifact sources")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "artifacts", artifacts)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "as_of": _stamp(self.as_of),
            "created_at": _stamp(self.created_at),
            "sources": list(self.sources),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: object) -> "LivePlan":
        if not isinstance(value, dict) or set(value) != {
            "version",
            "as_of",
            "created_at",
            "sources",
            "artifacts",
        }:
            raise ValueError("live plan has missing or unknown fields")
        if not isinstance(value["version"], int) or isinstance(value["version"], bool):
            raise ValueError("live plan version must be an integer")
        if not isinstance(value["sources"], list) or not all(
            isinstance(source, str) for source in value["sources"]
        ):
            raise ValueError("live plan sources must be strings")
        if not isinstance(value["artifacts"], list):
            raise ValueError("live plan artifacts must be a list")
        return cls(
            version=value["version"],
            as_of=_parse_stamp(value["as_of"]),
            created_at=_parse_stamp(value["created_at"]),
            sources=tuple(value["sources"]),
            artifacts=tuple(
                ArtifactSpec.from_dict(artifact) for artifact in value["artifacts"]
            ),
        )


def create_plan(
    as_of: datetime,
    *,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    created_at: datetime | None = None,
) -> LivePlan:
    observed_at = as_of.astimezone(UTC) if as_of.tzinfo else None
    if observed_at is None:
        raise ValueError("as_of must include a timezone")
    made_at = created_at or datetime.now(UTC)
    return LivePlan(
        as_of=observed_at,
        created_at=made_at,
        sources=sources,
        artifacts=tuple(
            _fair_source_order(plan_live_artifacts(observed_at, sources=sources))
        ),
    )


def _fair_source_order(artifacts: Sequence[ArtifactSpec]) -> list[ArtifactSpec]:
    """Interleave upstreams so one outage cannot starve every later source."""
    queues: dict[str, deque[ArtifactSpec]] = {}
    source_order: list[str] = []
    for artifact in artifacts:
        if artifact.source not in queues:
            queues[artifact.source] = deque()
            source_order.append(artifact.source)
        queues[artifact.source].append(artifact)
    ordered: list[ArtifactSpec] = []
    while any(queues.values()):
        for source in source_order:
            if queues[source]:
                ordered.append(queues[source].popleft())
    return ordered


def write_plan(plan: LivePlan, output: Path) -> Path:
    """Exclusively write a canonical plan whose filename commits to its contents."""
    root = Path(output)
    plan_dir = root / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(plan.to_dict())
    digest = hashlib.sha256(payload).hexdigest()
    path = plan_dir / f"live-{plan.as_of:%Y%m%dT%H%M%SZ}-{digest[:16]}.json"
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError(f"plan collision at {path}") from None
    return path


def read_plan(path: Path) -> LivePlan:
    plan_path = Path(path)
    with plan_path.open("rb") as handle:
        payload = handle.read(MAX_PLAN_BYTES + 1)
    if len(payload) > MAX_PLAN_BYTES:
        raise ValueError("live plan exceeds the configured size limit")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("live plan is not valid UTF-8 JSON") from error
    plan = LivePlan.from_dict(document)
    canonical = _canonical_json(plan.to_dict())
    if payload != canonical:
        raise ValueError("live plan is not canonical JSON")
    digest = hashlib.sha256(payload).hexdigest()
    expected_suffix = f"-{digest[:16]}.json"
    if not plan_path.name.endswith(expected_suffix):
        raise ValueError("live plan filename does not match its SHA-256")
    return plan


def find_plan(output: Path) -> Path:
    paths = sorted((Path(output) / "plans").glob("live-*.json"))
    if len(paths) != 1:
        raise ValueError(
            "record requires exactly one plan in OUTPUT/plans; pass --plan explicitly"
        )
    return paths[0]


def _record_id(artifact: FetchedArtifact) -> str:
    receipt = artifact.retrieved_at.strftime("%Y%m%dT%H%M%S%fZ").lower()
    key_digest = hashlib.sha256(artifact.spec.source_key.encode()).hexdigest()[:16]
    return f"{receipt}-{key_digest}"


def archive_artifact(store: ArchiveStore, artifact: FetchedArtifact) -> ArchiveRecord:
    """Write validated bytes and complete availability metadata to the archive."""
    spec = artifact.spec
    product = PRODUCT_BY_SOURCE.get(spec.source)
    if product is None:
        raise ValueError(f"no archive product is registered for {spec.source}")
    response_headers = {
        key: value
        for key, value in artifact.response_headers.items()
        if key in {"content-length", "content-type", "etag", "last-modified"}
    }
    return store.record(
        source=spec.source,
        product=product,
        record_id=_record_id(artifact),
        payload=artifact.payload,
        available_at=artifact.retrieved_at,
        retrieved_at=artifact.retrieved_at,
        source_issued_at=spec.issue_time,
        valid_at=spec.valid_time or spec.event_time,
        source_url=spec.url,
        content_type=spec.media_type,
        metadata={
            "recorder_schema_version": 1,
            "source_adapter_version": 1,
            "source_key": spec.source_key,
            "event_time": _stamp(spec.event_time),
            "license_notice": spec.license_notice,
            "response_headers": response_headers,
            "upstream_last_modified_at": (
                None
                if artifact.upstream_last_modified_at is None
                else _stamp(artifact.upstream_last_modified_at)
            ),
            **dict(spec.metadata),
        },
    )


@dataclass(frozen=True)
class RecordResult:
    source: str
    source_key: str
    record: ArchiveRecord | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.record is not None and self.error is None


def record_plan(
    plan: LivePlan,
    output: Path,
    *,
    workers: int = 4,
    deadline_seconds: float = 2_700.0,
    fetcher: Callable[[ArtifactSpec], FetchedArtifact] = fetch_artifact,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[RecordResult]:
    """Fetch a frozen plan concurrently, retaining independent failure details."""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 to 8")
    if (
        isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or not math.isfinite(float(deadline_seconds))
        or not 1 <= deadline_seconds <= 10_800
    ):
        raise ValueError("deadline_seconds must be from 1 to 10,800")
    store = ArchiveStore(Path(output))
    results: list[RecordResult | None] = [None] * len(plan.artifacts)
    deadline = monotonic() + float(deadline_seconds)
    deadline_message = "DeadlineExceeded: live archive cycle deadline expired"
    rate_lock = threading.Lock()
    next_start: dict[str, float] = {}
    minimum_interval = {
        "bom-kununurra-observations": 0.0,
        "noaa-gefs-0p25": 0.25,
        "noaa-jma-himawari9": 0.05,
    }

    def wait_for_source(spec: ArtifactSpec) -> None:
        interval = minimum_interval.get(spec.source, 0.25)
        with rate_lock:
            now = monotonic()
            planned = max(now, next_start.get(spec.source, now))
            next_start[spec.source] = planned + interval
        delay = planned - now
        if delay > 0:
            time.sleep(delay)

    def retrieve(index: int, spec: ArtifactSpec) -> tuple[int, FetchedArtifact]:
        if monotonic() >= deadline:
            raise TimeoutError("live archive cycle deadline expired")
        wait_for_source(spec)
        if monotonic() >= deadline:
            raise TimeoutError("live archive cycle deadline expired")
        return index, fetcher(spec)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ordcast-live") as pool:
        futures: dict[Future[tuple[int, FetchedArtifact]], int] = {}
        next_index = 0
        while next_index < len(plan.artifacts) or futures:
            while (
                next_index < len(plan.artifacts)
                and len(futures) < workers
                and monotonic() < deadline
            ):
                future = pool.submit(
                    retrieve,
                    next_index,
                    plan.artifacts[next_index],
                )
                futures[future] = next_index
                next_index += 1
            if not futures:
                break
            remaining = max(0.001, deadline - monotonic())
            try:
                future = next(as_completed(tuple(futures), timeout=remaining))
            except TimeoutError:
                break
            index = futures.pop(future)
            spec = plan.artifacts[index]
            try:
                _, artifact = future.result()
                record = archive_artifact(store, artifact)
                results[index] = RecordResult(spec.source, spec.source_key, record=record)
            except Exception as error:  # keep other independent feeds recording
                results[index] = RecordResult(
                    spec.source,
                    spec.source_key,
                    error=f"{type(error).__name__}: {error}",
                )
        for future, index in futures.items():
            future.cancel()
            spec = plan.artifacts[index]
            results[index] = RecordResult(
                spec.source,
                spec.source_key,
                error=deadline_message,
            )
        for index in range(next_index, len(plan.artifacts)):
            spec = plan.artifacts[index]
            results[index] = RecordResult(
                spec.source,
                spec.source_key,
                error=deadline_message,
            )
    return [result for result in results if result is not None]


def write_run_report(
    output: Path,
    plan: LivePlan,
    results: Sequence[RecordResult],
    *,
    completed_at: datetime | None = None,
) -> Path:
    finished = completed_at or datetime.now(UTC)
    successes = sum(result.succeeded for result in results)
    report = {
        "version": 1,
        "plan_as_of": _stamp(plan.as_of),
        "plan_sha256": hashlib.sha256(_canonical_json(plan.to_dict())).hexdigest(),
        "completed_at": _stamp(finished),
        "planned_artifacts": len(plan.artifacts),
        "recorded_artifacts": successes,
        "failed_artifacts": len(results) - successes,
        "success_ratios": success_ratios(results),
        "results": [
            {
                "source_key": result.source_key,
                "source": result.source,
                "status": "recorded" if result.succeeded else "failed",
                "record_id": (
                    None if result.record is None else result.record.record_id
                ),
                "sha256": (
                    None if result.record is None else result.record.sha256
                ),
                "error": result.error,
            }
            for result in results
        ],
    }
    payload = _canonical_json(report)
    digest = hashlib.sha256(payload).hexdigest()
    directory = Path(output) / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"live-{plan.as_of:%Y%m%dT%H%M%SZ}-{digest[:16]}.json"
    with path.open("xb") as handle:
        handle.write(payload)
    return path


def success_ratios(results: Sequence[RecordResult]) -> dict[str, float]:
    """Return source and GEFS-lead completion, so concentrated gaps cannot hide."""
    if not results:
        raise ValueError("at least one record result is required")
    totals: dict[str, int] = {}
    successes: dict[str, int] = {}
    for result in results:
        totals[result.source] = totals.get(result.source, 0) + 1
        successes[result.source] = successes.get(result.source, 0) + int(result.succeeded)
        if result.source == "noaa-gefs-0p25":
            match = GEFS_LEAD_KEY.search(result.source_key)
            if match is not None:
                group = f"{result.source}/f{match.group(1)}"
                totals[group] = totals.get(group, 0) + 1
                successes[group] = successes.get(group, 0) + int(result.succeeded)
    ratios = {
        source: successes.get(source, 0) / total for source, total in totals.items()
    }
    ratios["overall"] = sum(result.succeeded for result in results) / len(results)
    return ratios


def _sources(value: str) -> tuple[str, ...]:
    sources = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if not sources:
        raise argparse.ArgumentTypeError("at least one source is required")
    return sources


def _as_of(value: str) -> datetime:
    try:
        parsed = value.replace("Z", "+00:00")
        result = datetime.fromisoformat(parsed)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use an ISO-8601 timestamp with timezone") from error
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("--as-of must include a timezone")
    return result.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="freeze source URLs and issue times")
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--as-of", type=_as_of)
    plan.add_argument("--sources", type=_sources, default=DEFAULT_SOURCES)

    record = commands.add_parser("record", help="download and archive a frozen plan")
    record.add_argument("--output", required=True, type=Path)
    record.add_argument("--plan", type=Path)
    record.add_argument("--workers", type=int, default=4)
    record.add_argument("--deadline-seconds", type=float, default=2_700.0)
    record.add_argument("--min-success-ratio", type=float, default=0.90)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        observed_at = args.as_of or datetime.now(UTC)
        plan = create_plan(observed_at, sources=args.sources)
        path = write_plan(plan, args.output)
        print(f"Planned {len(plan.artifacts)} immutable artifacts in {path}")
        return 0

    if not 0 < args.min_success_ratio <= 1:
        raise SystemExit("--min-success-ratio must be greater than 0 and at most 1")
    plan_path = args.plan or find_plan(args.output)
    plan = read_plan(plan_path)
    results = record_plan(
        plan,
        args.output,
        workers=args.workers,
        deadline_seconds=args.deadline_seconds,
    )
    report_path = write_run_report(args.output, plan, results)
    succeeded = sum(result.succeeded for result in results)
    ratios = success_ratios(results)
    ratio = ratios["overall"]
    print(
        f"Recorded {succeeded}/{len(results)} artifacts ({ratio:.1%}); "
        f"report: {report_path}"
    )
    failing_ratios = {
        source: value
        for source, value in ratios.items()
        if value < args.min_success_ratio
    }
    if failing_ratios:
        details = ", ".join(
            f"{source}={value:.1%}" for source, value in sorted(failing_ratios.items())
        )
        print(
            f"source success ratio is below the required "
            f"{args.min_success_ratio:.1%}: {details}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
