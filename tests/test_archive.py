import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from ordcast.archive import (
    ArchiveCollisionError,
    ArchiveIntegrityError,
    ArchiveRecord,
    ArchiveStore,
)


UTC = timezone.utc
ISSUED = datetime(2026, 8, 12, 0, tzinfo=UTC)
AVAILABLE = datetime(2026, 8, 12, 1, tzinfo=UTC)
RETRIEVED = datetime(2026, 8, 12, 1, 5, tzinfo=UTC)
VALID = datetime(2026, 8, 12, 6, tzinfo=UTC)


def record_payload(store: ArchiveStore, payload: bytes = b"forecast-bytes"):
    return store.record(
        source="gefs",
        product="rain-ensemble",
        record_id="20260812T000000Z-f006",
        payload=payload,
        available_at=AVAILABLE,
        retrieved_at=RETRIEVED,
        source_issued_at=ISSUED,
        valid_at=VALID,
        source_url="https://example.test/gefs/file.grib2?run=00",
        content_type="application/x-grib2",
        metadata={"member_count": 31, "variables": ["apcp"]},
    )


def test_record_writes_content_addressed_blob_and_canonical_manifest(tmp_path):
    store = ArchiveStore(tmp_path / "archive")
    record = record_payload(store)

    expected_digest = hashlib.sha256(b"forecast-bytes").hexdigest()
    blob = store.root / "blobs" / "sha256" / expected_digest[:2] / expected_digest
    manifest_path = (
        store.root
        / "records"
        / "gefs"
        / "rain-ensemble"
        / "20260812T000000Z-f006.json"
    )

    assert record.sha256 == expected_digest
    assert record.size_bytes == len(b"forecast-bytes")
    assert blob.read_bytes() == b"forecast-bytes"
    assert store.read_blob(record) == b"forecast-bytes"
    assert store.load("gefs", "rain-ensemble", record.record_id) == record
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["content"] == {
        "blob_path": f"blobs/sha256/{expected_digest[:2]}/{expected_digest}",
        "content_type": "application/x-grib2",
        "sha256": expected_digest,
        "size_bytes": 14,
    }
    assert manifest_path.read_bytes().endswith(b"\n")
    assert not os.access(manifest_path, os.W_OK) or manifest_path.stat().st_mode & 0o222 == 0


def test_duplicate_is_idempotent_but_semantic_collision_is_rejected(tmp_path):
    store = ArchiveStore(tmp_path)
    first = record_payload(store)
    second = record_payload(store)
    assert first == second
    assert len(list(store.iter_records())) == 1

    with pytest.raises(ArchiveCollisionError, match="different archive semantics"):
        record_payload(store, b"different")
    assert store.read_blob(first) == b"forecast-bytes"


def test_timezone_validation_and_utc_normalization(tmp_path):
    store = ArchiveStore(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.record(
            source="gefs",
            product="rain",
            record_id="run",
            payload=b"x",
            available_at=datetime(2026, 8, 12),
            retrieved_at=RETRIEVED,
            source_url="https://example.test/file",
            content_type="application/octet-stream",
        )
    with pytest.raises(ValueError, match="cannot precede"):
        ArchiveRecord(
            "gefs",
            "rain",
            "run",
            AVAILABLE,
            AVAILABLE - timedelta(seconds=1),
            ISSUED,
            VALID,
            "https://example.test/file",
            "application/octet-stream",
            1,
            "0" * 64,
        )
    with pytest.raises(ValueError, match="valid_at cannot precede"):
        ArchiveRecord(
            "gefs",
            "rain",
            "run",
            AVAILABLE,
            RETRIEVED,
            ISSUED,
            ISSUED - timedelta(seconds=1),
            "https://example.test/file",
            "application/octet-stream",
            1,
            "0" * 64,
        )

    offset = timezone(timedelta(hours=8))
    record = store.record(
        source="silo",
        product="daily-rain",
        record_id="20260812",
        payload=b"csv",
        available_at=datetime(2026, 8, 12, 9, tzinfo=offset),
        retrieved_at=datetime(2026, 8, 12, 9, 5, tzinfo=offset),
        valid_at=datetime(2026, 8, 12, tzinfo=offset),
        source_url="https://example.test/silo",
        content_type="text/csv; charset=utf-8",
    )
    assert record.available_at == AVAILABLE
    assert record.available_at.tzinfo is UTC


@pytest.mark.parametrize("identifier", ["../escape", "/absolute", "a/b", "", ".", ".."])
def test_identifiers_cannot_escape_archive(identifier, tmp_path):
    store = ArchiveStore(tmp_path)
    with pytest.raises(ValueError, match="filesystem-safe"):
        store.record(
            source=identifier,
            product="rain",
            record_id="run",
            payload=b"x",
            available_at=AVAILABLE,
            retrieved_at=RETRIEVED,
            source_url="https://example.test/file",
            content_type="application/octet-stream",
        )


def test_secrets_are_not_persisted_in_url_or_metadata(tmp_path):
    store = ArchiveStore(tmp_path)
    record = store.record(
        source="ecmwf",
        product="open-data",
        record_id="run",
        payload=b"data",
        available_at=AVAILABLE,
        retrieved_at=RETRIEVED,
        source_url="https://example.test/file?step=6&token=top-secret#fragment-secret",
        content_type="application/octet-stream",
    )
    manifest_text = (
        store.root / "records" / "ecmwf" / "open-data" / "run.json"
    ).read_text(encoding="utf-8")
    assert "top-secret" not in manifest_text
    assert "fragment-secret" not in manifest_text
    assert "%5BREDACTED%5D" in record.source_url

    with pytest.raises(ValueError, match="secret-like key"):
        store.record(
            source="ecmwf",
            product="open-data",
            record_id="run-2",
            payload=b"data",
            available_at=AVAILABLE,
            retrieved_at=RETRIEVED,
            source_url="https://example.test/file",
            content_type="application/octet-stream",
            metadata={"api_token": "top-secret"},
        )


def test_record_metadata_is_a_defensive_immutable_snapshot(tmp_path):
    source_metadata = {"variables": ["rain"], "nested": {"member": 1}}
    record = ArchiveStore(tmp_path).record(
        source="gefs",
        product="rain",
        record_id="run",
        payload=b"x",
        available_at=AVAILABLE,
        retrieved_at=RETRIEVED,
        source_url="https://example.test/file",
        content_type="application/octet-stream",
        metadata=source_metadata,
    )
    source_metadata["variables"].append("wind")
    assert record.metadata["variables"] == ("rain",)
    with pytest.raises(TypeError):
        record.metadata["nested"]["member"] = 2
    detached = record.to_manifest()
    detached["metadata"]["variables"].append("temperature")
    assert record.metadata["variables"] == ("rain",)


def test_corrupt_blob_and_manifest_are_detected(tmp_path):
    store = ArchiveStore(tmp_path)
    record = record_payload(store)
    blob = store.root / record.blob_relative_path
    blob.chmod(0o600)
    blob.write_bytes(b"corrupt-bytes")
    with pytest.raises(ArchiveIntegrityError, match="SHA-256|size"):
        store.load(record.source, record.product, record.record_id)

    manifest = (
        store.root / "records" / record.source / record.product / f"{record.record_id}.json"
    )
    manifest.chmod(0o600)
    manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ArchiveIntegrityError, match="schema"):
        store.load(record.source, record.product, record.record_id)


def test_partial_temp_files_are_invisible_and_recoverable(tmp_path):
    store = ArchiveStore(tmp_path)
    partial = store.root / "records" / ".ordcast-tmp-abandoned"
    partial.write_bytes(b'{"partial":')

    assert list(store.iter_records()) == []
    assert store.recover_temporary_files(older_than_seconds=0) == 1
    assert not partial.exists()
    assert store.recover_temporary_files(older_than_seconds=0) == 0


def test_blob_deduplication_keeps_separate_semantic_records(tmp_path):
    store = ArchiveStore(tmp_path)
    first = record_payload(store)
    second = store.record(
        source="gefs",
        product="rain-ensemble",
        record_id="20260812T000000Z-f012",
        payload=b"forecast-bytes",
        available_at=AVAILABLE,
        retrieved_at=RETRIEVED,
        source_issued_at=ISSUED,
        valid_at=VALID + timedelta(hours=6),
        source_url="https://example.test/gefs/file2.grib2",
        content_type="application/x-grib2",
    )
    assert first.sha256 == second.sha256
    assert len(list((store.root / "blobs" / "sha256").rglob(first.sha256))) == 1
    assert len(list(store.iter_records(source="gefs"))) == 2


def test_windows_atomic_publication_uses_rename_without_directory_fsync(
    monkeypatch, tmp_path
):
    destination = tmp_path / "published"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Windows publication must not open/fsync a directory")

    real_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        if path == tmp_path:
            return forbidden()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)

    assert ArchiveStore._atomic_exclusive_create(
        destination, b"windows-safe", _windows=True
    )
    assert destination.read_bytes() == b"windows-safe"
    assert not list(tmp_path.glob(".ordcast-tmp-*"))


def test_windows_atomic_publication_preserves_a_concurrent_winner(monkeypatch, tmp_path):
    destination = tmp_path / "published"
    destination.write_bytes(b"winner")

    def windows_rename(source, target):
        assert target == destination
        raise FileExistsError(target)

    monkeypatch.setattr(os, "rename", windows_rename)

    assert not ArchiveStore._atomic_exclusive_create(
        destination, b"loser", _windows=True
    )
    assert destination.read_bytes() == b"winner"
    assert not list(tmp_path.glob(".ordcast-tmp-*"))
