"""Operate the self-contained OrdCast recorder on one local computer.

Examples::

    python -m pipelines.local_station cycle --archive-root C:\\OrdCastData\\archive
    python -m pipelines.local_station status --archive-root C:\\OrdCastData\\archive
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ordcast.local_station import (
    CycleAlreadyRunningError,
    local_station_status,
    run_local_cycle,
    source_names,
)


UTC = timezone.utc


def _as_of(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "use an ISO-8601 timestamp that includes a timezone"
        ) from error
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return result.astimezone(UTC)


def _sources(value: str) -> tuple[str, ...]:
    try:
        return source_names(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    cycle = commands.add_parser(
        "cycle", help="plan, download, validate, and archive one local cycle"
    )
    cycle.add_argument("--archive-root", required=True, type=Path)
    cycle.add_argument("--as-of", type=_as_of)
    cycle.add_argument("--sources", type=_sources, default=source_names("bom,gefs,himawari"))
    cycle.add_argument("--workers", type=int, default=4)
    cycle.add_argument("--deadline-seconds", type=float, default=2_700.0)
    cycle.add_argument("--min-success-ratio", type=float, default=1.0)

    status = commands.add_parser("status", help="show the newest local archive cycle")
    status.add_argument("--archive-root", required=True, type=Path)
    status.add_argument("--stale-after-hours", type=float, default=8.0)
    status.add_argument("--json", action="store_true")
    return parser


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < 1_000 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1_000
    raise AssertionError("unreachable")


def _print_status(status: object) -> None:
    print(f"OrdCast local station: {status.state.upper()}")
    print(status.message)
    print(f"Archive: {status.archive_root}")
    print(
        f"Disk free: {_human_bytes(status.disk_free_bytes)} of "
        f"{_human_bytes(status.disk_total_bytes)}"
    )
    if status.latest_completed_at is not None:
        print(f"Last cycle: {status.latest_completed_at:%Y-%m-%d %H:%M UTC}")
        print(
            f"Recorded: {status.recorded_artifacts}/{status.planned_artifacts} "
            f"artifacts"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "cycle":
        try:
            result = run_local_cycle(
                args.archive_root,
                as_of=args.as_of,
                sources=args.sources,
                workers=args.workers,
                deadline_seconds=args.deadline_seconds,
                min_success_ratio=args.min_success_ratio,
            )
        except CycleAlreadyRunningError as error:
            print(str(error), file=sys.stderr)
            return 3
        print(
            f"Recorded {result.recorded_artifacts}/{result.planned_artifacts} "
            f"artifacts; report: {result.report_path}"
        )
        if not result.complete:
            failed = ", ".join(
                f"{name}={ratio:.1%}"
                for name, ratio in sorted(result.success_ratios.items())
                if ratio < result.required_success_ratio
            )
            print(f"Collection was incomplete: {failed}", file=sys.stderr)
            return 1
        return 0

    status = local_station_status(
        args.archive_root, stale_after_hours=args.stale_after_hours
    )
    if args.json:
        print(json.dumps(status.to_dict(), allow_nan=False, indent=2, sort_keys=True))
    else:
        _print_status(status)
    return 0 if status.state == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
