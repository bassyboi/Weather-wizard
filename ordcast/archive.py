"""Immutable, content-addressed storage for weather-source snapshots.

The archive deliberately separates raw response bytes from record manifests.  Raw
bytes are addressed by SHA-256, while a caller-supplied semantic record identifier
points at an immutable JSON manifest.  Publication uses an exclusive hard link from
a fully flushed temporary file, so an interrupted write is never mistaken for a
complete blob or record.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1_000_000
MAX_METADATA_BYTES = 64_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTENT_TYPE = re.compile(
    r"[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+"
    r"(?:\s*;\s*[A-Za-z0-9!#$&^_.+\-]+=[^\x00-\x1f\x7f;]{1,128})*\Z"
)
_TEMP_PREFIX = ".ordcast-tmp-"
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "accesskey",
        "accesskeyid",
        "auth",
        "authorization",
        "awsaccesskeyid",
        "clientsecret",
        "credential",
        "email",
        "key",
        "password",
        "passwd",
        "privatekey",
        "sas",
        "secret",
        "sessionid",
        "sig",
        "signature",
        "subscriptionkey",
        "token",
        "user",
        "username",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
    }
)
_SECRET_IN_TEXT = re.compile(
    r"(?i)(?:api[_-]?key|authorization|credential|password|secret|signature|token)\s*[=:]"
)


class ArchiveError(RuntimeError):
    """Base error raised by the archive."""


class ArchiveCollisionError(ArchiveError):
    """A semantic record identifier is already bound to different content."""


class ArchiveIntegrityError(ArchiveError):
    """Stored data is missing, malformed, non-canonical, or corrupt."""


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not _IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError(
            f"{name} must be a filesystem-safe identifier containing only "
            "letters, numbers, '.', '_', and '-'"
        )
    return value


def _utc_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must have a valid UTC offset") from error
    if offset is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} cannot be represented in UTC") from error


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, name: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ArchiveIntegrityError(f"manifest {name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ArchiveIntegrityError(f"manifest {name} is not a valid timestamp") from error
    parsed = _utc_datetime(parsed, name)
    if _timestamp(parsed) != value:
        raise ArchiveIntegrityError(f"manifest {name} is not canonical")
    return parsed


def _normalized_secret_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_sensitive_key(value: str) -> bool:
    normalized = _normalized_secret_key(value)
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        (
            "accesskey",
            "apikey",
            "credential",
            "password",
            "secret",
            "signature",
            "subscriptionkey",
            "token",
        )
    )


def _json_value(value: Any, *, path: str = "metadata", depth: int = 0) -> Any:
    """Return a defensive, JSON-safe copy while refusing likely credentials."""
    if depth > 20:
        raise ValueError("metadata nesting exceeds the configured limit")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"{path} strings cannot contain NUL bytes")
        if _SECRET_IN_TEXT.search(value) or "-----BEGIN PRIVATE KEY-----" in value:
            raise ValueError(f"{path} appears to contain a secret")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Real):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{path} numbers must be finite")
        return number
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            if _is_sensitive_key(key):
                raise ValueError(f"{path} contains a secret-like key: {key!r}")
            copied[key] = _json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return copied
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{path} must contain only JSON-compatible values")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _safe_source_url(value: Any) -> str:
    """Canonicalize a public URL and redact common credential query parameters."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("source_url must be a non-empty HTTP(S) URL")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("source_url cannot contain control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("source_url is malformed") from error
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source_url must not contain user credentials")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise ValueError("source_url hostname is invalid") from error
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    safe_query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        safe_query.append((key, "[REDACTED]" if _is_sensitive_key(key) else item))
    return urlunsplit(
        (scheme, netloc, parsed.path or "/", urlencode(safe_query), "")
    )


def _content_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("content_type must be a string")
    cleaned = value.strip()
    if len(cleaned) > 255 or not _CONTENT_TYPE.fullmatch(cleaned):
        raise ValueError("content_type must be a valid MIME media type")
    media_type, *parameters = cleaned.split(";", maxsplit=1)
    if parameters:
        parameter_name = parameters[0].strip().split("=", maxsplit=1)[0]
        if _is_sensitive_key(parameter_name):
            raise ValueError("content_type cannot contain credential parameters")
    suffix = f";{parameters[0]}" if parameters else ""
    return media_type.strip().casefold() + suffix


def _canonical_json(manifest: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as error:
        raise ValueError("manifest is not JSON serializable") from error
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the configured size limit")
    return encoded


@dataclass(frozen=True)
class ArchiveRecord:
    """Validated immutable provenance for one archived source response."""

    source: str
    product: str
    record_id: str
    available_at: datetime
    retrieved_at: datetime
    source_issued_at: datetime | None
    valid_at: datetime | None
    source_url: str
    content_type: str
    size_bytes: int
    sha256: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        source = _identifier(self.source, "source")
        product = _identifier(self.product, "product")
        record_id = _identifier(self.record_id, "record_id")
        available = _utc_datetime(self.available_at, "available_at")
        retrieved = _utc_datetime(self.retrieved_at, "retrieved_at")
        issued = (
            None
            if self.source_issued_at is None
            else _utc_datetime(self.source_issued_at, "source_issued_at")
        )
        valid = (
            None if self.valid_at is None else _utc_datetime(self.valid_at, "valid_at")
        )
        if retrieved < available:
            raise ValueError("retrieved_at cannot precede available_at")
        if issued is not None and issued > available:
            raise ValueError("source_issued_at cannot follow available_at")
        if issued is not None and valid is not None and valid < issued:
            raise ValueError("valid_at cannot precede source_issued_at")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValueError("size_bytes must be an integer")
        if self.size_bytes <= 0:
            raise ValueError("size_bytes must be positive")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        metadata = _json_value(self.metadata)
        metadata_bytes = json.dumps(
            metadata, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(metadata_bytes) > MAX_METADATA_BYTES:
            raise ValueError("metadata exceeds the configured size limit")

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "product", product)
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "retrieved_at", retrieved)
        object.__setattr__(self, "source_issued_at", issued)
        object.__setattr__(self, "valid_at", valid)
        object.__setattr__(self, "source_url", _safe_source_url(self.source_url))
        object.__setattr__(self, "content_type", _content_type(self.content_type))
        object.__setattr__(self, "metadata", _freeze(metadata))

    @property
    def blob_relative_path(self) -> Path:
        return Path("blobs") / "sha256" / self.sha256[:2] / self.sha256

    def to_manifest(self) -> dict[str, Any]:
        """Return a detached JSON-compatible manifest mapping."""
        return {
            "schema_version": self.schema_version,
            "identity": {
                "source": self.source,
                "product": self.product,
                "record_id": self.record_id,
            },
            "times": {
                "available_at": _timestamp(self.available_at),
                "retrieved_at": _timestamp(self.retrieved_at),
                "source_issued_at": _timestamp(self.source_issued_at),
                "valid_at": _timestamp(self.valid_at),
            },
            "content": {
                "sha256": self.sha256,
                "size_bytes": self.size_bytes,
                "content_type": self.content_type,
                "blob_path": self.blob_relative_path.as_posix(),
            },
            "source_url": self.source_url,
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_manifest(cls, manifest: Any) -> "ArchiveRecord":
        """Validate and construct a record from its persisted representation."""
        try:
            if not isinstance(manifest, Mapping) or set(manifest) != {
                "schema_version",
                "identity",
                "times",
                "content",
                "source_url",
                "metadata",
            }:
                raise ArchiveIntegrityError("manifest has an unexpected schema")
            if (
                not isinstance(manifest["schema_version"], int)
                or isinstance(manifest["schema_version"], bool)
                or manifest["schema_version"] != SCHEMA_VERSION
            ):
                raise ArchiveIntegrityError("manifest schema version is unsupported")
            identity = manifest["identity"]
            times = manifest["times"]
            content = manifest["content"]
            if not isinstance(identity, Mapping) or set(identity) != {
                "source",
                "product",
                "record_id",
            }:
                raise ArchiveIntegrityError("manifest identity is malformed")
            if not isinstance(times, Mapping) or set(times) != {
                "available_at",
                "retrieved_at",
                "source_issued_at",
                "valid_at",
            }:
                raise ArchiveIntegrityError("manifest times are malformed")
            if not isinstance(content, Mapping) or set(content) != {
                "sha256",
                "size_bytes",
                "content_type",
                "blob_path",
            }:
                raise ArchiveIntegrityError("manifest content is malformed")
            record = cls(
                source=identity["source"],
                product=identity["product"],
                record_id=identity["record_id"],
                available_at=_parse_timestamp(times["available_at"], "available_at"),
                retrieved_at=_parse_timestamp(times["retrieved_at"], "retrieved_at"),
                source_issued_at=_parse_timestamp(
                    times["source_issued_at"], "source_issued_at", optional=True
                ),
                valid_at=_parse_timestamp(times["valid_at"], "valid_at", optional=True),
                source_url=manifest["source_url"],
                content_type=content["content_type"],
                size_bytes=content["size_bytes"],
                sha256=content["sha256"],
                metadata=manifest["metadata"],
            )
        except ArchiveIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise ArchiveIntegrityError("manifest failed validation") from error
        if content["blob_path"] != record.blob_relative_path.as_posix():
            raise ArchiveIntegrityError("manifest blob path does not match its digest")
        return record


class ArchiveStore:
    """Filesystem-backed, append-only archive of exact upstream response bytes."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise TypeError("archive root must be a filesystem path")
        supplied = Path(root).expanduser()
        supplied.mkdir(parents=True, exist_ok=True)
        self._root = supplied.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("archive root must be a directory")
        self._ensure_directory(self._root / "blobs" / "sha256")
        self._ensure_directory(self._root / "records")

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_directory(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ArchiveIntegrityError("archive directory cannot be a symbolic link")
        try:
            directory.resolve(strict=True).relative_to(self._root)
        except ValueError as error:
            raise ArchiveIntegrityError("archive directory escapes the archive root") from error

    def _blob_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("digest must be a lowercase hexadecimal SHA-256 value")
        directory = self._root / "blobs" / "sha256" / digest[:2]
        self._ensure_directory(directory)
        return directory / digest

    def _manifest_path(self, source: str, product: str, record_id: str) -> Path:
        source = _identifier(source, "source")
        product = _identifier(product, "product")
        record_id = _identifier(record_id, "record_id")
        directory = self._root / "records" / source / product
        self._ensure_directory(directory)
        return directory / f"{record_id}.json"

    @staticmethod
    def _payload_bytes(payload: Any) -> bytes:
        if isinstance(payload, bytes):
            copied = payload
        elif isinstance(payload, (bytearray, memoryview)):
            copied = bytes(payload)
        else:
            raise ValueError("payload must be bytes-like")
        if not copied:
            raise ValueError("payload cannot be empty")
        return copied

    @staticmethod
    def _atomic_exclusive_create(
        path: Path, payload: bytes, *, _windows: bool | None = None
    ) -> bool:
        """Publish bytes atomically without ever replacing an existing path.

        POSIX uses a hard-link publication so rename cannot replace a concurrent
        winner. Windows ``os.rename`` already fails when the destination exists and
        works on NTFS, FAT, and exFAT; it also avoids Windows directory-fsync and
        read-only-hard-link cleanup failures.
        """

        windows = os.name == "nt" if _windows is None else _windows
        temporary = path.parent / f"{_TEMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if windows:
                try:
                    os.rename(temporary, path)
                except FileExistsError:
                    return False
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return False
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            if not windows:
                directory_fd = os.open(
                    path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_limited(path: Path, maximum: int) -> bytes:
        try:
            if path.is_symlink():
                raise ArchiveIntegrityError("archive entries cannot be symbolic links")
            with path.open("rb") as stream:
                payload = stream.read(maximum + 1)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise ArchiveIntegrityError("archive entry could not be read") from error
        if len(payload) > maximum:
            raise ArchiveIntegrityError("archive entry exceeds its size limit")
        return payload

    def _verify_blob(self, record: ArchiveRecord) -> bytes:
        path = self._blob_path(record.sha256)
        try:
            payload = self._read_limited(path, record.size_bytes)
        except FileNotFoundError as error:
            raise ArchiveIntegrityError("record content blob is missing") from error
        if len(payload) != record.size_bytes:
            raise ArchiveIntegrityError("record content blob has an unexpected size")
        if hashlib.sha256(payload).hexdigest() != record.sha256:
            raise ArchiveIntegrityError("record content blob failed SHA-256 verification")
        return payload

    def _store_blob(self, payload: bytes, digest: str) -> None:
        path = self._blob_path(digest)
        if self._atomic_exclusive_create(path, payload):
            return
        if path.is_symlink():
            raise ArchiveIntegrityError("content blob cannot be a symbolic link")
        try:
            existing_size = path.stat().st_size
        except OSError as error:
            raise ArchiveIntegrityError("existing content blob cannot be inspected") from error
        if existing_size != len(payload):
            raise ArchiveIntegrityError("existing content blob has an unexpected size")
        existing = self._read_limited(path, len(payload))
        if hashlib.sha256(existing).hexdigest() != digest:
            raise ArchiveIntegrityError("existing content blob failed SHA-256 verification")

    def record(
        self,
        *,
        source: str,
        product: str,
        record_id: str,
        payload: bytes | bytearray | memoryview,
        available_at: datetime,
        retrieved_at: datetime,
        source_issued_at: datetime | None = None,
        valid_at: datetime | None = None,
        source_url: str,
        content_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArchiveRecord:
        """Atomically append a raw response and its immutable semantic record."""
        exact_payload = self._payload_bytes(payload)
        digest = hashlib.sha256(exact_payload).hexdigest()
        record = ArchiveRecord(
            source=source,
            product=product,
            record_id=record_id,
            available_at=available_at,
            retrieved_at=retrieved_at,
            source_issued_at=source_issued_at,
            valid_at=valid_at,
            source_url=source_url,
            content_type=content_type,
            size_bytes=len(exact_payload),
            sha256=digest,
            metadata={} if metadata is None else metadata,
        )
        manifest_payload = _canonical_json(record.to_manifest())
        path = self._manifest_path(record.source, record.product, record.record_id)

        # Avoid leaving a new, unreferenced blob after a routine collision with a
        # record that was already present before this call. The later exclusive
        # create remains necessary to close the concurrent-writer race.
        if path.exists() or path.is_symlink():
            existing = self._load_path(path, verify_blob=True)
            if _canonical_json(existing.to_manifest()) != manifest_payload:
                raise ArchiveCollisionError(
                    "record identifier is already bound to different archive semantics"
                )
            return existing

        self._store_blob(exact_payload, digest)
        if self._atomic_exclusive_create(path, manifest_payload):
            return record

        existing = self._load_path(path, verify_blob=True)
        if _canonical_json(existing.to_manifest()) != manifest_payload:
            raise ArchiveCollisionError(
                "record identifier is already bound to different archive semantics"
            )
        return existing

    def _load_path(self, path: Path, *, verify_blob: bool) -> ArchiveRecord:
        try:
            payload = self._read_limited(path, MAX_MANIFEST_BYTES)
        except FileNotFoundError:
            raise
        try:
            manifest = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArchiveIntegrityError("record manifest is not valid UTF-8 JSON") from error
        record = ArchiveRecord.from_manifest(manifest)
        if _canonical_json(record.to_manifest()) != payload:
            raise ArchiveIntegrityError("record manifest is not canonical")
        expected = self._manifest_path(record.source, record.product, record.record_id)
        if expected != path:
            raise ArchiveIntegrityError("record manifest identity does not match its path")
        if verify_blob:
            self._verify_blob(record)
        return record

    def load(self, source: str, product: str, record_id: str) -> ArchiveRecord:
        """Load a record and verify its manifest and referenced content blob."""
        return self._load_path(
            self._manifest_path(source, product, record_id), verify_blob=True
        )

    def read_blob(self, record_or_digest: ArchiveRecord | str) -> bytes:
        """Return exact archived bytes after size and digest verification."""
        if isinstance(record_or_digest, ArchiveRecord):
            return self._verify_blob(record_or_digest)
        digest = record_or_digest
        path = self._blob_path(digest)
        try:
            if path.is_symlink():
                raise ArchiveIntegrityError("content blob cannot be a symbolic link")
            payload = path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as error:
            raise ArchiveIntegrityError("content blob could not be read") from error
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ArchiveIntegrityError("content blob failed SHA-256 verification")
        return payload

    def iter_records(
        self, *, source: str | None = None, product: str | None = None
    ) -> Iterator[ArchiveRecord]:
        """Yield verified records in deterministic path order."""
        if source is None and product is not None:
            raise ValueError("product filtering requires a source")
        start = self._root / "records"
        if source is not None:
            start = start / _identifier(source, "source")
        if product is not None:
            start = start / _identifier(product, "product")
        if not start.exists():
            return
        for path in sorted(start.rglob("*.json")):
            relative = path.relative_to(self._root / "records")
            if len(relative.parts) != 3 or not relative.name.endswith(".json"):
                raise ArchiveIntegrityError("record path has an unexpected layout")
            yield self.load(
                relative.parts[0], relative.parts[1], relative.name.removesuffix(".json")
            )

    def recover_temporary_files(
        self,
        *,
        older_than_seconds: float = 3600.0,
        now: datetime | None = None,
    ) -> int:
        """Remove abandoned private temp files; canonical entries are never touched."""
        if isinstance(older_than_seconds, bool) or not isinstance(older_than_seconds, Real):
            raise ValueError("older_than_seconds must be a finite non-negative number")
        age = float(older_than_seconds)
        if not math.isfinite(age) or age < 0:
            raise ValueError("older_than_seconds must be a finite non-negative number")
        current = time.time() if now is None else _utc_datetime(now, "now").timestamp()
        removed = 0
        for base in (self._root / "blobs", self._root / "records"):
            for path in base.rglob(f"{_TEMP_PREFIX}*"):
                try:
                    details = path.lstat()
                    if not stat.S_ISREG(details.st_mode) or current - details.st_mtime < age:
                        continue
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise ArchiveIntegrityError(
                        "temporary archive file cannot be removed"
                    ) from error
        return removed


__all__ = [
    "ArchiveCollisionError",
    "ArchiveError",
    "ArchiveIntegrityError",
    "ArchiveRecord",
    "ArchiveStore",
    "SCHEMA_VERSION",
]
