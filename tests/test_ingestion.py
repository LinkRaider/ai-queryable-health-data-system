from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from health_data.db import apply_schema, connect, ingest, ingestion_run_key
from health_data.generator import GenerationConfig

TEST_DATABASE_URL = os.getenv(
    "HEALTH_DATA_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/health_data",
)


class PostgreSQLIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.connection = connect(TEST_DATABASE_URL)
        except psycopg.OperationalError as error:
            raise unittest.SkipTest(f"test PostgreSQL unavailable: {error}") from error
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
            ingestion_run_key(self.config, 500),
            ingestion_run_key(self.config, 500),
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
