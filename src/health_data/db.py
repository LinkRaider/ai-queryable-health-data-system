"""PostgreSQL schema management and idempotent JSONL batch ingestion."""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from health_data.generator import (
    GENERATOR_VERSION,
    SCHEMA_VERSION,
    GenerationConfig,
    HeartRateEvent,
    generate_profiles,
)
from health_data.source import EventSource, read_events, source_configuration

DEFAULT_BATCH_SIZE = 1_000
INGESTION_LOCK_KEY = "health-data-single-snapshot-ingestion"
SCHEMA_PATH = Path(
    os.getenv(
        "HEALTH_DATA_SCHEMA_PATH",
        str(Path(__file__).resolve().parents[2] / "sql" / "schema.sql"),
    )
)
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    source_key: str
    status: str
    expected_event_count: int
    inserted_event_count: int
    inserted_this_attempt: int
    processed_this_attempt: int
    batches_processed: int


def connect(database_url: str) -> Connection:
    return psycopg.connect(database_url, row_factory=dict_row)


def apply_schema(connection: Connection) -> None:
    connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()


def _configuration(
    config: GenerationConfig,
    source: EventSource,
) -> dict[str, object]:
    return {
        **source_configuration(config),
        "source_sha256": source.sha256,
    }


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
    ON CONFLICT DO NOTHING
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


def _result_from_row(
    row: dict,
    *,
    inserted_this_attempt: int,
    processed_this_attempt: int,
    batches_processed: int,
) -> IngestionResult:
    return IngestionResult(
        source_key=row["source_key"],
        status=row["status"],
        expected_event_count=row["expected_event_count"],
        inserted_event_count=row["inserted_event_count"],
        inserted_this_attempt=inserted_this_attempt,
        processed_this_attempt=processed_this_attempt,
        batches_processed=batches_processed,
    )


def _assert_batch_matches_database(
    connection: Connection,
    batch: list[HeartRateEvent],
) -> None:
    rows = connection.execute(
        """
        SELECT
            event_id,
            schema_version,
            user_id,
            device_id,
            observed_at,
            metric,
            value,
            unit,
            activity_state
        FROM heart_rate_observations
        WHERE event_id = ANY(%s)
        """,
        ([event.event_id for event in batch],),
    ).fetchall()
    by_id = {row["event_id"]: row for row in rows}
    for event in batch:
        row = by_id.get(event.event_id)
        if row is None:
            raise RuntimeError(
                f"event identity conflict for {event.event_id}: "
                "its natural key already belongs to different content"
            )
        actual = (
            row["schema_version"],
            row["user_id"],
            row["device_id"],
            row["observed_at"],
            row["metric"],
            row["value"],
            row["unit"],
            row["activity_state"],
        )
        expected = (
            event.schema_version,
            event.user_id,
            event.device_id,
            event.observed_at,
            event.metric,
            Decimal(str(event.value)),
            event.unit,
            event.activity_state.value,
        )
        if actual != expected:
            raise RuntimeError(
                f"event identity conflict for {event.event_id}: "
                "the persisted content differs from the source"
            )


def ingest(
    connection: Connection,
    source: EventSource,
    config: GenerationConfig = GenerationConfig(),
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_delay_seconds: float = 0,
    after_batch_commit: Callable[[int, IngestionResult], None] | None = None,
) -> IngestionResult:
    """Replay a persisted source from the beginning using idempotent batches."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if batch_delay_seconds < 0:
        raise ValueError("batch_delay_seconds cannot be negative")

    run_configuration = _configuration(config, source)
    inserted_this_attempt = 0
    processed_this_attempt = 0
    batches_processed = 0
    lock_acquired = False

    try:
        connection.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (INGESTION_LOCK_KEY,),
        )
        connection.commit()
        lock_acquired = True

        run = connection.execute(
            "SELECT * FROM ingestion_runs WHERE source_key = %s",
            (source.source_key,),
        ).fetchone()
        connection.commit()

        if run is None:
            existing_run = connection.execute(
                "SELECT source_key FROM ingestion_runs ORDER BY started_at LIMIT 1"
            ).fetchone()
            connection.commit()
            if existing_run is not None:
                raise RuntimeError(
                    "database already contains a different ingestion run; "
                    "use an isolated database for a different generation configuration"
                )
            with connection.transaction():
                _insert_profiles(connection, config)
                run = connection.execute(
                    """
                    INSERT INTO ingestion_runs (
                        source_key,
                        schema_version,
                        generator_version,
                        configuration,
                        status,
                        expected_event_count
                    )
                    VALUES (%s, %s, %s, %s, 'in_progress', %s)
                    RETURNING *
                    """,
                    (
                        source.source_key,
                        SCHEMA_VERSION,
                        GENERATOR_VERSION,
                        Jsonb(run_configuration),
                        source.unique_event_count,
                    ),
                ).fetchone()
        elif run["configuration"] != run_configuration:
            raise RuntimeError(
                f"ingestion source configuration mismatch for {source.source_key}"
            )

        assert run is not None
        for batch_number, batch in enumerate(
            _batches(read_events(source), batch_size),
            start=1,
        ):
            with connection.transaction():
                cursor = connection.cursor()
                cursor.executemany(EVENT_INSERT_SQL, [_event_values(event) for event in batch])
                inserted = cursor.rowcount
                _assert_batch_matches_database(connection, batch)
                updated = connection.execute(
                    """
                    UPDATE ingestion_runs
                    SET
                        inserted_event_count = inserted_event_count + %s,
                        updated_at = now(),
                        status = 'in_progress',
                        completed_at = NULL
                    WHERE source_key = %s
                    RETURNING *
                    """,
                    (
                        inserted,
                        source.source_key,
                    ),
                ).fetchone()
                if updated is None:
                    raise RuntimeError("ingestion run disappeared during replay")
            inserted_this_attempt += inserted
            processed_this_attempt += len(batch)
            batches_processed = batch_number
            run = updated
            progress = _result_from_row(
                run,
                inserted_this_attempt=inserted_this_attempt,
                processed_this_attempt=processed_this_attempt,
                batches_processed=batches_processed,
            )
            if after_batch_commit is not None:
                after_batch_commit(batch_number, progress)
            if batch_delay_seconds:
                time.sleep(batch_delay_seconds)

        if processed_this_attempt != source.event_count:
            raise RuntimeError(
                "persisted source event count changed after publication"
            )
        database_count = connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        connection.commit()
        if database_count != source.unique_event_count:
            raise RuntimeError(
                f"database contains {database_count} observations; "
                f"source declares {source.unique_event_count} unique events"
            )
        with connection.transaction():
            run = connection.execute(
                """
                UPDATE ingestion_runs
                SET
                    status = 'complete',
                    inserted_event_count = %s,
                    updated_at = now(),
                    completed_at = now()
                WHERE source_key = %s
                RETURNING *
                """,
                (database_count, source.source_key),
            ).fetchone()
        assert run is not None
        return _result_from_row(
            run,
            inserted_this_attempt=inserted_this_attempt,
            processed_this_attempt=processed_this_attempt,
            batches_processed=batches_processed,
        )
    finally:
        if lock_acquired:
            if connection.info.transaction_status.name != "IDLE":
                connection.rollback()
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (INGESTION_LOCK_KEY,),
            )
            connection.commit()
