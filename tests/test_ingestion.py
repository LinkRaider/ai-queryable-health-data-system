from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from health_data.db import apply_schema, connect, ingest, ingestion_run_key
from health_data.generator import GenerationConfig, generate_events

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

    def test_failure_after_committed_batch_resumes_without_duplicates(self) -> None:
        config = GenerationConfig(days=1, user_count=1)

        def interrupt(batch_number, _progress) -> None:
            if batch_number == 2:
                raise RuntimeError("simulated interruption after committed batch 2")

        with self.assertRaisesRegex(RuntimeError, "committed batch 2"):
            ingest(
                self.connection,
                config,
                batch_size=100,
                after_batch_commit=interrupt,
            )

        interrupted = self.connection.execute(
            "SELECT * FROM ingestion_runs WHERE run_key = %s",
            (ingestion_run_key(config),),
        ).fetchone()
        persisted_count = self.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        self.connection.commit()
        self.assertEqual("in_progress", interrupted["status"])
        self.assertEqual(2, interrupted["next_batch_index"])
        self.assertEqual(200, persisted_count)

        resumed = ingest(self.connection, config, batch_size=100)
        database_ids = [
            row["event_id"]
            for row in self.connection.execute(
                "SELECT event_id FROM heart_rate_observations ORDER BY user_id, observed_at"
            ).fetchall()
        ]
        self.connection.commit()
        expected_ids = [event.event_id for event in generate_events(config)]
        self.assertEqual("complete", resumed.status)
        self.assertEqual(expected_ids, database_ids)
        self.assertEqual(len(expected_ids) - persisted_count, resumed.inserted_this_attempt)

    def test_invalid_batch_controls_are_rejected(self) -> None:
        config = GenerationConfig(days=1, user_count=1)
        with self.assertRaisesRegex(ValueError, "batch_size"):
            ingest(self.connection, config, batch_size=0)
        with self.assertRaisesRegex(ValueError, "batch_delay"):
            ingest(self.connection, config, batch_delay_seconds=-0.1)

    def test_changed_batch_size_cannot_fork_or_resume_a_partial_run(self) -> None:
        config = GenerationConfig(days=1, user_count=1)

        def interrupt(_batch_number, _progress) -> None:
            raise RuntimeError("stop after first committed batch")

        with self.assertRaisesRegex(RuntimeError, "first committed batch"):
            ingest(
                self.connection,
                config,
                batch_size=100,
                after_batch_commit=interrupt,
            )
        with self.assertRaisesRegex(RuntimeError, "configuration mismatch"):
            ingest(self.connection, config, batch_size=200)

        runs = self.connection.execute(
            "SELECT count(*) AS count FROM ingestion_runs"
        ).fetchone()["count"]
        checkpoint = self.connection.execute(
            "SELECT next_batch_index FROM ingestion_runs"
        ).fetchone()["next_batch_index"]
        self.connection.commit()
        self.assertEqual(1, runs)
        self.assertEqual(1, checkpoint)
        self.assertEqual(
            ingestion_run_key(config),
            ingestion_run_key(config),
        )
        self.assertNotEqual(
            ingestion_run_key(config),
            ingestion_run_key(
                GenerationConfig(seed=config.seed + 1, days=1, user_count=1)
            ),
        )

    def test_database_rejects_complete_run_with_missing_events(self) -> None:
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.connection.execute(
                """
                INSERT INTO ingestion_runs (
                    run_key,
                    schema_version,
                    generator_version,
                    configuration,
                    status,
                    expected_event_count,
                    total_batches,
                    next_batch_index,
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
                    1,
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
        cls.first = ingest(cls.connection, cls.config, batch_size=500)
        cls.count_after_first = cls.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        cls.connection.commit()
        cls.replay = ingest(cls.connection, cls.config, batch_size=500)
        cls.count_after_replay = cls.connection.execute(
            "SELECT count(*) AS count FROM heart_rate_observations"
        ).fetchone()["count"]
        cls.connection.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def test_ingestion_completes_all_planned_batches(self) -> None:
        self.assertEqual("complete", self.first.status)
        self.assertEqual(self.first.total_batches, self.first.next_batch_index)
        self.assertEqual(self.first.expected_event_count, self.count_after_first)
        self.assertEqual(self.first.expected_event_count, self.first.inserted_event_count)
        self.assertEqual(self.first.expected_event_count, self.first.inserted_this_attempt)

    def test_replay_does_not_duplicate_measurements(self) -> None:
        self.assertEqual(self.count_after_first, self.count_after_replay)
        self.assertEqual(0, self.replay.inserted_this_attempt)
        self.assertEqual(self.first.run_key, self.replay.run_key)
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

    def test_run_key_is_stable_and_configuration_is_durable(self) -> None:
        self.assertEqual(
            ingestion_run_key(self.config),
            ingestion_run_key(self.config),
        )
        run = self.connection.execute(
            "SELECT * FROM ingestion_runs WHERE run_key = %s",
            (self.first.run_key,),
        ).fetchone()
        self.connection.commit()
        self.assertIsNotNone(run["completed_at"])
        self.assertEqual("complete", run["status"])
        self.assertEqual(500, run["configuration"]["batch_size"])
        self.assertEqual(self.config.seed, run["configuration"]["seed"])

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
