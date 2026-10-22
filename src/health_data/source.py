"""Atomic JSONL source publication and strict event replay."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Iterator

from health_data.generator import (
    EVENT_NAMESPACE,
    GENERATOR_VERSION,
    SCHEMA_VERSION,
    ActivityState,
    GenerationConfig,
    HeartRateEvent,
    canonical_timestamp,
    generate_events,
)

SOURCE_FORMAT_VERSION = "health-data-jsonl-source.v1"
USER_ID_PATTERN = re.compile(r"^user-[0-9]{4}$")
DEVICE_ID_PATTERN = re.compile(r"^device-[0-9]{4}$")
TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$"
)


class SourceError(RuntimeError):
    """The persisted source is absent, corrupt, or incompatible."""


@dataclass(frozen=True, slots=True)
class EventSource:
    path: Path
    metadata_path: Path
    source_key: str
    configuration: dict[str, object]
    event_count: int
    unique_event_count: int
    sha256: str
    reused: bool


def source_configuration(config: GenerationConfig) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "seed": config.seed,
        "window_start": canonical_timestamp(config.window_start),
        "window_end": canonical_timestamp(config.window_end),
        "user_count": config.user_count,
    }


def source_key(config: GenerationConfig) -> str:
    encoded = json.dumps(
        source_configuration(config),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def metadata_path_for(path: Path) -> Path:
    return path.with_suffix(f"{path.suffix}.metadata.json")


def _encode_event(event: HeartRateEvent) -> bytes:
    return (
        json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, sort_keys=True, separators=(",", ":"))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def publish_source(
    path: Path,
    config: GenerationConfig,
    events: Iterable[HeartRateEvent],
) -> EventSource:
    """Write a complete source beside its metadata, publishing JSONL by rename."""

    path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = metadata_path_for(path)
    temporary_path: Path | None = None
    digest = hashlib.sha256()
    event_count = 0
    event_ids: set[uuid.UUID] = set()
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            for event in events:
                encoded = _encode_event(event)
                temporary.write(encoded)
                digest.update(encoded)
                event_count += 1
                event_ids.add(event.event_id)
            temporary.flush()
            os.fsync(temporary.fileno())

        configuration = source_configuration(config)
        metadata = {
            "format_version": SOURCE_FORMAT_VERSION,
            "source_key": source_key(config),
            "configuration": configuration,
            "event_count": event_count,
            "unique_event_count": len(event_ids),
            "sha256": digest.hexdigest(),
        }
        os.replace(temporary_path, path)
        temporary_path = None
        _write_json_atomic(metadata_path, metadata)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return EventSource(
        path=path,
        metadata_path=metadata_path,
        source_key=metadata["source_key"],
        configuration=configuration,
        event_count=event_count,
        unique_event_count=len(event_ids),
        sha256=metadata["sha256"],
        reused=False,
    )


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceError(f"source metadata is unreadable: {path}") from error
    if not isinstance(payload, dict):
        raise SourceError(f"source metadata must be a JSON object: {path}")
    return payload


def _validate_existing_source(path: Path, config: GenerationConfig) -> EventSource:
    metadata_path = metadata_path_for(path)
    metadata = _read_metadata(metadata_path)
    expected_configuration = source_configuration(config)
    if metadata.get("format_version") != SOURCE_FORMAT_VERSION:
        raise SourceError("persisted source uses an unsupported format version")
    if metadata.get("configuration") != expected_configuration:
        raise SourceError(
            "persisted source configuration is incompatible with the requested configuration"
        )
    if metadata.get("source_key") != source_key(config):
        raise SourceError("persisted source key does not match its configuration")
    persisted_source_key = metadata["source_key"]
    event_count = metadata.get("event_count")
    unique_event_count = metadata.get("unique_event_count")
    expected_digest = metadata.get("sha256")
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 0
        or isinstance(unique_event_count, bool)
        or not isinstance(unique_event_count, int)
        or not 0 <= unique_event_count <= event_count
        or not isinstance(expected_digest, str)
        or len(expected_digest) != 64
        or not isinstance(persisted_source_key, str)
    ):
        raise SourceError("persisted source metadata has invalid count or digest fields")

    digest = hashlib.sha256()
    observed_count = 0
    try:
        with path.open("rb") as source_file:
            for line in source_file:
                digest.update(line)
                observed_count += 1
    except OSError as error:
        raise SourceError(f"persisted source is unreadable: {path}") from error
    if observed_count != event_count or digest.hexdigest() != expected_digest:
        raise SourceError("persisted source content does not match its published metadata")

    source = EventSource(
        path=path,
        metadata_path=metadata_path,
        source_key=persisted_source_key,
        configuration=expected_configuration,
        event_count=event_count,
        unique_event_count=unique_event_count,
        sha256=expected_digest,
        reused=True,
    )
    observed_event_ids = {event.event_id for event in read_events(source)}
    if len(observed_event_ids) != unique_event_count:
        raise SourceError(
            "persisted source unique-event count does not match its published metadata"
        )
    return source


def ensure_source(
    path: Path,
    config: GenerationConfig = GenerationConfig(),
    *,
    event_generator: Callable[[GenerationConfig], Iterable[HeartRateEvent]] = generate_events,
) -> EventSource:
    """Reuse a complete compatible source or atomically create it once."""

    metadata_path = metadata_path_for(path)
    if path.exists() and metadata_path.exists():
        return _validate_existing_source(path, config)

    # A lone file is not a published source. It can only result from an
    # interrupted initial publication and is safe to replace.
    path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    return publish_source(path, config, event_generator(config))


def _parse_event(
    payload: object,
    line_number: int,
    configuration: dict[str, object],
) -> HeartRateEvent:
    if not isinstance(payload, dict):
        raise SourceError(f"invalid source line {line_number}: expected a JSON object")
    expected_fields = {
        "schema_version",
        "event_id",
        "user_id",
        "device_id",
        "observed_at",
        "metric",
        "value",
        "unit",
        "activity_state",
    }
    if set(payload) != expected_fields:
        raise SourceError(
            f"invalid source line {line_number}: event fields do not match the v1 contract"
        )
    try:
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if payload["metric"] != "heart_rate" or payload["unit"] != "bpm":
            raise ValueError("unsupported metric or unit")
        if not isinstance(payload["event_id"], str):
            raise ValueError("event_id must be a UUID string")
        if (
            not isinstance(payload["user_id"], str)
            or USER_ID_PATTERN.fullmatch(payload["user_id"]) is None
            or not isinstance(payload["device_id"], str)
            or DEVICE_ID_PATTERN.fullmatch(payload["device_id"]) is None
        ):
            raise ValueError("invalid user_id or device_id")
        user_number = int(payload["user_id"][-4:])
        if (
            not 1 <= user_number <= int(configuration["user_count"])
            or payload["device_id"] != f"device-{user_number:04d}"
        ):
            raise ValueError(
                "user_id and device_id do not match a configured source profile"
            )
        if (
            not isinstance(payload["observed_at"], str)
            or TIMESTAMP_PATTERN.fullmatch(payload["observed_at"]) is None
        ):
            raise ValueError("observed_at must be an offset-aware RFC 3339 timestamp")
        value = payload["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("value must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError("value must be finite")
        if Decimal(str(value)).as_tuple().exponent < -1:
            raise ValueError("value must have at most one decimal place")
        event = HeartRateEvent(
            schema_version=payload["schema_version"],
            event_id=uuid.UUID(payload["event_id"]),
            user_id=payload["user_id"],
            device_id=payload["device_id"],
            observed_at=datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00")),
            metric=payload["metric"],
            value=float(value),
            unit=payload["unit"],
            activity_state=ActivityState(payload["activity_state"]),
        )
        if event.observed_at.tzinfo is None or event.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include an offset")
        if not 30 <= event.value <= 220:
            raise ValueError("value is outside the supported range")
        canonical_identity = "|".join(
            (
                SCHEMA_VERSION,
                str(configuration["seed"]),
                str(configuration["window_start"]),
                str(configuration["window_end"]),
                event.user_id,
                event.device_id,
                canonical_timestamp(event.observed_at),
            )
        )
        if event.event_id != uuid.uuid5(EVENT_NAMESPACE, canonical_identity):
            raise ValueError(
                "event_id does not match the canonical v1 event identity"
            )
        return event
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as error:
        raise SourceError(f"invalid source line {line_number}: {error}") from error


def read_events(source: EventSource) -> Iterator[HeartRateEvent]:
    try:
        with source.path.open("rb") as source_file:
            for line_number, encoded_line in enumerate(source_file, start=1):
                try:
                    line = encoded_line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise SourceError(
                        f"invalid source line {line_number}: invalid UTF-8"
                    ) from error
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as error:
                    raise SourceError(
                        f"invalid source line {line_number}: malformed JSON"
                    ) from error
                yield _parse_event(payload, line_number, source.configuration)
    except OSError as error:
        raise SourceError(f"persisted source is unreadable: {source.path}") from error
