"""PostgreSQL schema management and interruption-safe batch ingestion."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, TypeVar

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from health_data.generator import (
    GENERATOR_VERSION,
    SCHEMA_VERSION,
    GenerationConfig,
    HeartRateEvent,
    canonical_timestamp,
    generate_events,
    generate_profiles,
)

DEFAULT_BATCH_SIZE = 1_000
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    run_key: str
    status: str
    expected_event_count: int
    total_batches: int
    next_batch_index: int
    inserted_event_count: int
    inserted_this_attempt: int


def connect(database_url: str) -> Connection:
    return psycopg.connect(database_url, row_factory=dict_row)


def apply_schema(connection: Connection) -> None:
    connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()


def _configuration(config: GenerationConfig, batch_size: int) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "seed": config.seed,
        "window_start": canonical_timestamp(config.window_start),
        "window_end": canonical_timestamp(config.window_end),
        "user_count": config.user_count,
        "batch_size": batch_size,
    }


def ingestion_run_key(config: GenerationConfig, batch_size: int = DEFAULT_BATCH_SIZE) -> str:
    encoded = json.dumps(
        _configuration(config, batch_size),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _batches(values: Iterable[T], batch_size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for value in values:
        batch.append(value)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _profile_payload(profile) -> dict[str, object]:
    payload = asdict(profile)
    payload.pop("user_id")
    payload.pop("device_id")
    return payload


def _insert_profiles(connection: Connection, config: GenerationConfig) -> None:
    rows = [
        (
            profile.user_id,
            profile.device_id,
            profile.timezone_name,
            profile.resting_baseline_bpm,
            Jsonb(_profile_payload(profile)),
        )
        for profile in generate_profiles(config)
    ]
    connection.cursor().executemany(
        """
        INSERT INTO users (
            user_id, device_id, timezone_name, resting_baseline_bpm, profile
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        rows,
    )


EVENT_INSERT_SQL = """
    INSERT INTO heart_rate_observations (
        event_id,
        schema_version,
        user_id,
        device_id,
        observed_at,
        metric,
        value,
        unit,
        activity_state
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (event_id) DO NOTHING
"""


def _event_values(event: HeartRateEvent) -> tuple[object, ...]:
    return (
        event.event_id,
        event.schema_version,
        event.user_id,
        event.device_id,
        event.observed_at,
        event.metric,
        event.value,
        event.unit,
        event.activity_state.value,
    )


def _result_from_row(row: dict, inserted_this_attempt: int) -> IngestionResult:
    return IngestionResult(
        run_key=row["run_key"],
        status=row["status"],
        expected_event_count=row["expected_event_count"],
        total_batches=row["total_batches"],
        next_batch_index=row["next_batch_index"],
        inserted_event_count=row["inserted_event_count"],
        inserted_this_attempt=inserted_this_attempt,
    )


def ingest(
    connection: Connection,
    config: GenerationConfig = GenerationConfig(),
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> IngestionResult:
    """Ingest a deterministic run, resuming after its last committed batch."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    run_configuration = _configuration(config, batch_size)
    run_key = ingestion_run_key(config, batch_size)
    inserted_this_attempt = 0
    lock_acquired = False

    try:
        connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (run_key,))
        connection.commit()
        lock_acquired = True

        run = connection.execute(
            "SELECT * FROM ingestion_runs WHERE run_key = %s",
            (run_key,),
        ).fetchone()
        connection.commit()

        if run is None:
            expected_event_count = sum(1 for _ in generate_events(config))
            total_batches = math.ceil(expected_event_count / batch_size)
            with connection.transaction():
                _insert_profiles(connection, config)
                run = connection.execute(
                    """
                    INSERT INTO ingestion_runs (
                        run_key,
                        schema_version,
                        generator_version,
                        configuration,
                        status,
                        expected_event_count,
                        total_batches
                    )
                    VALUES (%s, %s, %s, %s, 'in_progress', %s, %s)
                    RETURNING *
                    """,
                    (
                        run_key,
                        SCHEMA_VERSION,
                        GENERATOR_VERSION,
                        Jsonb(run_configuration),
                        expected_event_count,
                        total_batches,
                    ),
                ).fetchone()
        elif run["configuration"] != run_configuration:
            raise RuntimeError(f"ingestion run configuration mismatch for {run_key}")

        assert run is not None
        if run["status"] == "complete":
            return _result_from_row(run, inserted_this_attempt)

        next_batch_index = run["next_batch_index"]
        for batch_index, batch in enumerate(_batches(generate_events(config), batch_size)):
            if batch_index < next_batch_index:
                continue
            with connection.transaction():
                cursor = connection.cursor()
                cursor.executemany(EVENT_INSERT_SQL, [_event_values(event) for event in batch])
                inserted = cursor.rowcount
                updated = connection.execute(
                    """
                    UPDATE ingestion_runs
                    SET
                        next_batch_index = %s,
                        inserted_event_count = inserted_event_count + %s,
                        updated_at = now(),
                        status = CASE
                            WHEN %s = total_batches THEN 'complete'
                            ELSE status
                        END,
                        completed_at = CASE
                            WHEN %s = total_batches THEN now()
                            ELSE completed_at
                        END
                    WHERE run_key = %s AND next_batch_index = %s
                    RETURNING *
                    """,
                    (
                        batch_index + 1,
                        inserted,
                        batch_index + 1,
                        batch_index + 1,
                        run_key,
                        batch_index,
                    ),
                ).fetchone()
                if updated is None:
                    raise RuntimeError(
                        f"checkpoint moved unexpectedly for batch {batch_index}"
                    )
            inserted_this_attempt += inserted
            run = updated

        assert run is not None
        if run["status"] != "complete":
            raise RuntimeError(
                f"run ended at batch {run['next_batch_index']} of {run['total_batches']}"
            )
        return _result_from_row(run, inserted_this_attempt)
    finally:
        if lock_acquired:
            if connection.info.transaction_status.name != "IDLE":
                connection.rollback()
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (run_key,),
            )
            connection.commit()
