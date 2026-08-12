"""Verify the optional Windows GPU environment and save an auditable status file."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from ordcast.accelerator import probe_accelerator


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def write_gpu_status(path: Path, payload: dict[str, object]) -> None:
    """Atomically replace derived status; it is not training evidence."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_json(payload)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".ordcast-gpu-", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status = probe_accelerator()
    payload = status.to_dict()
    if args.output is not None:
        write_gpu_status(args.output, payload)
    if args.json:
        print(json.dumps(payload, allow_nan=False, indent=2, sort_keys=True))
    else:
        label = "READY" if status.fully_ready else "NOT READY"
        print(f"OrdCast GPU: {label}")
        print(f"Training: {status.training_backend} ({status.device_name or 'no GPU'})")
        print(f"Prediction: {status.inference_backend}")
        for note in status.notes:
            print(f"- {note}")
    return 0 if status.fully_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
