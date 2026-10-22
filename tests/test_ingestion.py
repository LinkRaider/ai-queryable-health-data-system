from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from health_data.db import apply_schema, connect, ingest
from health_data.generator import GenerationConfig, generate_events
from health_data.query import QueryOperation, StructuredQuery, execute_query
from health_data.source import EventSource, SourceError, publish_source

TEST_DATABASE_URL = os.getenv(
    "HEALTH_DATA_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/health_data",
)


class IngestionRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.connection = connect(TEST_DATABASE_URL)
        except psycopg.OperationalError as error:
            raise RuntimeError(f"test PostgreSQL unavailable: {error}") from error
        apply_schema(cls.connection)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def setUp(self) -> None:
        self.connection.execute(
            "TRUNCATE heart_rate_observations, ingestion_runs, users CASCADE"
        )
        self.connection.commit()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

    def source(self, config, events=None) -> EventSource:
        path = Path(self.temporary_directory.name) / "events.jsonl"
        return publish_source(
            path,
            config,
            generate_events(config) if events is None else events,
        )

    def test_failure_after_committed_batch_resumes_without_duplicates(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        events = list(generate_events(config))[:250]
        source = self.source(config, events)

        def interrupt(batch_number, _progress) -> None:
            if batch_number == 2:
                raise RuntimeError("simulated interruption after committed batch 2")

        with self.assertRaisesRegex(RuntimeError, "committed batch 2"):
            ingest(
                self.connection,
                source,
                config,
                batch_size=100,
                after_batch_commit=interrupt,
            )

        interrupted = self.connection.execute(
            "SELECT * FROM ingestion_runs WHERE source_key = %s",
            (source.source_key,),
        ).fetchone()
        persisted_count = self.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        self.connection.commit()
        self.assertEqual("in_progress", interrupted["status"])
        self.assertEqual(200, persisted_count)

        resumed = ingest(self.connection, source, config, batch_size=100)
        database_ids = [
            row["event_id"]
            for row in self.connection.execute(
                "SELECT event_id FROM heart_rate_observations ORDER BY user_id, observed_at"
            ).fetchall()
        ]
        self.connection.commit()
        expected_ids = [event.event_id for event in events]
        self.assertEqual("complete", resumed.status)
        self.assertEqual(expected_ids, database_ids)
        self.assertEqual(len(expected_ids) - persisted_count, resumed.inserted_this_attempt)
        self.assertEqual(len(events), resumed.processed_this_attempt)

    def test_invalid_batch_controls_are_rejected(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        source = self.source(config)
        with self.assertRaisesRegex(ValueError, "batch_size"):
            ingest(self.connection, source, config, batch_size=0)
        with self.assertRaisesRegex(ValueError, "batch_delay"):
            ingest(self.connection, source, config, batch_delay_seconds=-0.1)

    def test_changed_batch_size_replays_same_source_safely(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        events = list(generate_events(config))[:250]
        source = self.source(config, events)

        def interrupt(_batch_number, _progress) -> None:
            raise RuntimeError("stop after first committed batch")

        with self.assertRaisesRegex(RuntimeError, "first committed batch"):
            ingest(
                self.connection,
                source,
                config,
                batch_size=100,
                after_batch_commit=interrupt,
            )
        resumed = ingest(self.connection, source, config, batch_size=200)

        runs = self.connection.execute(
            "SELECT count(*) AS count FROM ingestion_runs"
        ).fetchone()["count"]
        self.connection.commit()
        self.assertEqual(1, runs)
        self.assertEqual("complete", resumed.status)
        self.assertEqual(150, resumed.inserted_this_attempt)
        self.assertEqual(250, resumed.processed_this_attempt)

    def test_different_generation_configuration_requires_isolated_database(
        self,
    ) -> None:
        original = GenerationConfig(days=1, user_count=1)
        different = GenerationConfig(
            seed=original.seed + 1,
            days=1,
            user_count=1,
        )
        source = self.source(original, list(generate_events(original))[:20])
        ingest(self.connection, source, original, batch_size=500)
        different_source = publish_source(
            Path(self.temporary_directory.name) / "different.jsonl",
            different,
            list(generate_events(different))[:20],
        )

        with self.assertRaisesRegex(RuntimeError, "isolated database"):
            ingest(
                self.connection,
                different_source,
                different,
                batch_size=500,
            )

        run_count = self.connection.execute(
            "SELECT count(*) AS count FROM ingestion_runs"
        ).fetchone()["count"]
        self.connection.commit()
        self.assertEqual(1, run_count)

    def test_invalid_line_reports_its_line_number(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        events = list(generate_events(config))[:2]
        source = self.source(config, events)
        lines = source.path.read_text(encoding="utf-8").splitlines()
        source.path.write_text(f"{lines[0]}\nnot-json\n", encoding="utf-8")

        with self.assertRaisesRegex(SourceError, "line 2"):
            ingest(self.connection, source, config, batch_size=1)

        count = self.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        self.connection.commit()
        self.assertEqual(1, count)

    def test_same_event_id_with_different_content_is_rejected(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        event = next(generate_events(config))
        conflicting = replace(event, value=event.value + 1)
        source = self.source(config, [event, conflicting])

        with self.assertRaisesRegex(RuntimeError, "persisted content differs"):
            ingest(self.connection, source, config, batch_size=10)

        count = self.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        self.connection.commit()
        self.assertEqual(0, count)

    def test_exact_duplicates_are_accepted_without_duplicate_rows(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        first, second = list(generate_events(config))[:2]
        source = self.source(config, [first, first, second])

        result = ingest(self.connection, source, config, batch_size=10)

        self.assertEqual(3, result.processed_this_attempt)
        self.assertEqual(2, result.inserted_event_count)
        self.assertEqual(2, result.inserted_this_attempt)

    def test_out_of_order_and_late_old_events_have_identical_final_rows(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        events = list(generate_events(config))[:20]
        source = self.source(config, [*events[10:], *events[1:10], events[0]])

        ingest(self.connection, source, config, batch_size=2)
        query = StructuredQuery(
            operation=QueryOperation.ACTIVITY_STATE_COMPARISON,
            user_id="user-0001",
            start=config.window_start,
            end=config.window_end,
        )
        out_of_order_result = execute_query(self.connection, query).rows

        observed = self.connection.execute(
            "SELECT event_id FROM heart_rate_observations ORDER BY observed_at"
        ).fetchall()
        self.connection.commit()
        self.assertEqual(
            [event.event_id for event in events],
            [row["event_id"] for row in observed],
        )

        self.connection.execute(
            "TRUNCATE heart_rate_observations, ingestion_runs, users CASCADE"
        )
        self.connection.commit()
        ordered_source = publish_source(
            Path(self.temporary_directory.name) / "ordered.jsonl",
            config,
            events,
        )
        ingest(self.connection, ordered_source, config, batch_size=3)
        self.assertEqual(out_of_order_result, execute_query(self.connection, query).rows)

    def test_database_rejects_complete_source_with_missing_events(self) -> None:
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.connection.execute(
                """
                INSERT INTO ingestion_runs (
                    source_key,
                    schema_version,
                    generator_version,
                    configuration,
                    status,
                    expected_event_count,
                    inserted_event_count,
                    completed_at
                )
                VALUES (
                    %s,
                    'health.heart_rate.observation.v1',
                    'test',
                    '{}'::jsonb,
                    'complete',
                    1,
                    0,
                    now()
                )
                """,
                ("c" * 64,),
            )
        self.connection.rollback()


class PostgreSQLIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.connection = connect(TEST_DATABASE_URL)
        except psycopg.OperationalError as error:
            raise RuntimeError(f"test PostgreSQL unavailable: {error}") from error
        apply_schema(cls.connection)
        cls.connection.execute(
            "TRUNCATE heart_rate_observations, ingestion_runs, users CASCADE"
        )
        cls.connection.commit()
        cls.config = GenerationConfig(days=1, user_count=3)
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.source = publish_source(
            Path(cls.temporary_directory.name) / "events.jsonl",
            cls.config,
            generate_events(cls.config),
        )
        cls.first = ingest(
            cls.connection,
            cls.source,
            cls.config,
            batch_size=500,
        )
        cls.count_after_first = cls.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        cls.connection.commit()
        cls.replay = ingest(
            cls.connection,
            cls.source,
            cls.config,
            batch_size=500,
        )
        cls.count_after_replay = cls.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        cls.connection.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()
        cls.temporary_directory.cleanup()

    def test_ingestion_completes_all_planned_batches(self) -> None:
        self.assertEqual("complete", self.first.status)
        self.assertEqual(self.first.expected_event_count, self.count_after_first)
        self.assertEqual(self.first.expected_event_count, self.first.inserted_event_count)
        self.assertEqual(self.first.expected_event_count, self.first.inserted_this_attempt)
        self.assertEqual(self.source.event_count, self.first.processed_this_attempt)

    def test_replay_does_not_duplicate_measurements(self) -> None:
        self.assertEqual(self.count_after_first, self.count_after_replay)
        self.assertEqual(0, self.replay.inserted_this_attempt)
        self.assertEqual(self.first.source_key, self.replay.source_key)
        self.assertEqual(self.source.event_count, self.replay.processed_this_attempt)
        distinct = self.connection.execute(
            """
            SELECT
                count(DISTINCT event_id) AS event_ids,
                count(DISTINCT (user_id, device_id, observed_at, metric)) AS natural_keys
            FROM heart_rate_observations
            """
        ).fetchone()
        self.connection.commit()
        self.assertEqual(self.count_after_replay, distinct["event_ids"])
        self.assertEqual(self.count_after_replay, distinct["natural_keys"])

    def test_source_identity_and_configuration_are_durable(self) -> None:
        run = self.connection.execute(
            "SELECT * FROM ingestion_runs WHERE source_key = %s",
            (self.first.source_key,),
        ).fetchone()
        self.connection.commit()
        self.assertIsNotNone(run["completed_at"])
        self.assertEqual("complete", run["status"])
        self.assertEqual(self.config.seed, run["configuration"]["seed"])
        self.assertEqual(self.source.sha256, run["configuration"]["source_sha256"])

    def test_natural_key_rejects_a_different_event_id(self) -> None:
        existing = self.connection.execute(
            "SELECT * FROM heart_rate_observations LIMIT 1"
        ).fetchone()
        self.connection.commit()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.connection.execute(
                """
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
                """,
                (
                    uuid.uuid4(),
                    existing["schema_version"],
                    existing["user_id"],
                    existing["device_id"],
                    existing["observed_at"],
                    existing["metric"],
                    existing["value"],
                    existing["unit"],
                    existing["activity_state"],
                ),
            )
        self.connection.rollback()


if __name__ == "__main__":
    unittest.main()
