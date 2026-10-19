from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg
from psycopg.types.json import Jsonb

from health_data.db import apply_schema, connect
from health_data.query import (
    AnalyticalQueryError,
    QueryOperation,
    ResponseTooLargeError,
    StructuredQuery,
    UnknownUserError,
    build_query,
    execute_query,
    validate_query,
)

TEST_DATABASE_URL = os.getenv(
    "HEALTH_DATA_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/health_data",
)
START = datetime(2026, 1, 5, tzinfo=UTC)
END = datetime(2026, 1, 7, tzinfo=UTC)


class AnalyticalQueryTests(unittest.TestCase):
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
        cls.connection.cursor().executemany(
            """
            INSERT INTO users (
                user_id, device_id, timezone_name, resting_baseline_bpm, profile
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                ("user-0001", "device-0001", "UTC", 60.0, Jsonb({})),
                ("user-0002", "device-0002", "UTC", 50.0, Jsonb({})),
            ],
        )
        observations = [
            ("user-0001", "device-0001", START, 40.0, "rest"),
            ("user-0001", "device-0001", START + timedelta(hours=1), 60.0, "rest"),
            ("user-0001", "device-0001", START + timedelta(hours=2), 80.0, "rest"),
            (
                "user-0001",
                "device-0001",
                START + timedelta(hours=3),
                100.0,
                "ordinary_activity",
            ),
            (
                "user-0002",
                "device-0002",
                START + timedelta(hours=4),
                50.0,
                "rest",
            ),
            (
                "user-0002",
                "device-0002",
                START + timedelta(hours=5),
                40.0,
                "sleep",
            ),
            (
                "user-0001",
                "device-0001",
                START + timedelta(days=1, hours=1),
                70.0,
                "rest",
            ),
            (
                "user-0001",
                "device-0001",
                START + timedelta(days=1, hours=2),
                160.0,
                "workout",
            ),
            (
                "user-0002",
                "device-0002",
                START + timedelta(days=1, hours=3),
                110.0,
                "ordinary_activity",
            ),
            ("user-0001", "device-0001", END, 200.0, "rest"),
        ]
        cls.connection.cursor().executemany(
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
            VALUES (%s, 'health.heart_rate.observation.v1', %s, %s, %s,
                    'heart_rate', %s, 'bpm', %s)
            """,
            [
                (uuid.UUID(int=index), *observation)
                for index, observation in enumerate(observations, start=1)
            ],
        )
        cls.connection.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def test_daily_resting_average_has_known_answer(self) -> None:
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                start=START,
                end=END,
            ),
        )
        self.assertEqual(
            (
                {
                    "day": "2026-01-05",
                    "average_bpm": 60.0,
                    "observation_count": 3,
                },
                {
                    "day": "2026-01-06",
                    "average_bpm": 70.0,
                    "observation_count": 1,
                },
            ),
            result.rows,
        )

    def test_activity_state_comparison_has_known_answer(self) -> None:
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.ACTIVITY_STATE_COMPARISON,
                user_id="user-0001",
                start=START,
                end=END,
            ),
        )
        self.assertEqual(
            (
                {
                    "activity_state": "rest",
                    "average_bpm": 62.5,
                    "observation_count": 4,
                },
                {
                    "activity_state": "ordinary_activity",
                    "average_bpm": 100.0,
                    "observation_count": 1,
                },
                {
                    "activity_state": "workout",
                    "average_bpm": 160.0,
                    "observation_count": 1,
                },
            ),
            result.rows,
        )

    def test_population_daily_average_has_known_answer(self) -> None:
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                start=START,
                end=END,
            ),
        )
        self.assertEqual(
            (
                {
                    "day": "2026-01-05",
                    "average_bpm": 61.7,
                    "observation_count": 6,
                    "user_count": 2,
                },
                {
                    "day": "2026-01-06",
                    "average_bpm": 113.3,
                    "observation_count": 3,
                    "user_count": 2,
                },
            ),
            result.rows,
        )

    def test_range_is_start_inclusive_and_end_exclusive(self) -> None:
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                start=START,
                end=START + timedelta(hours=1),
            ),
        )
        self.assertEqual(
            (
                {
                    "day": "2026-01-05",
                    "average_bpm": 40.0,
                    "observation_count": 1,
                },
            ),
            result.rows,
        )

    def test_user_day_grouping_uses_profile_timezone(self) -> None:
        self.connection.execute(
            """
            INSERT INTO users (
                user_id, device_id, timezone_name, resting_baseline_bpm, profile
            )
            VALUES ('user-0003', 'device-0003', 'America/Los_Angeles', 60.0, %s)
            """,
            (Jsonb({}),),
        )
        self.connection.cursor().executemany(
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
            VALUES (
                %s,
                'health.heart_rate.observation.v1',
                'user-0003',
                'device-0003',
                %s,
                'heart_rate',
                %s,
                'bpm',
                'rest'
            )
            """,
            [
                (uuid.UUID(int=1001), START + timedelta(hours=7, minutes=30), 50.0),
                (uuid.UUID(int=1002), START + timedelta(hours=8, minutes=30), 70.0),
            ],
        )
        self.connection.commit()
        self.addCleanup(self._delete_user_three_fixture)

        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0003",
                start=START,
                end=START + timedelta(days=1),
            ),
        )
        self.assertEqual(
            (
                {
                    "day": "2026-01-04",
                    "average_bpm": 50.0,
                    "observation_count": 1,
                },
                {
                    "day": "2026-01-05",
                    "average_bpm": 70.0,
                    "observation_count": 1,
                },
            ),
            result.rows,
        )

    def _delete_user_three_fixture(self) -> None:
        self.connection.execute(
            "DELETE FROM heart_rate_observations WHERE user_id = 'user-0003'"
        )
        self.connection.execute("DELETE FROM users WHERE user_id = 'user-0003'")
        self.connection.commit()

    def test_empty_result_is_an_empty_row_list(self) -> None:
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                start=START + timedelta(days=10),
                end=START + timedelta(days=11),
            ),
        )
        self.assertEqual((), result.rows)

    def test_invalid_and_unsupported_requests_are_rejected(self) -> None:
        invalid_queries = (
            StructuredQuery(
                operation="arbitrary_dimension",
                user_id="user-0001",
                start=START,
                end=END,
            ),
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                start=START.replace(tzinfo=None),
                end=END,
            ),
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                start=END,
                end=START,
            ),
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                start=START,
                end=START + timedelta(days=32),
            ),
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                metric="steps",
                start=START,
                end=END,
            ),
            StructuredQuery(
                operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                user_id="user-0001",
                start=START,
                end=END,
            ),
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001' OR true --",
                start=START,
                end=END,
            ),
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                with self.assertRaises(AnalyticalQueryError):
                    validate_query(query)

    def test_unknown_user_is_rejected(self) -> None:
        with self.assertRaisesRegex(UnknownUserError, "unknown user"):
            execute_query(
                self.connection,
                StructuredQuery(
                    operation=QueryOperation.DAILY_USER_AVERAGE,
                    user_id="user-9999",
                    start=START,
                    end=END,
                ),
            )

    def test_oversized_response_is_rejected_not_truncated(self) -> None:
        with self.assertRaisesRegex(ResponseTooLargeError, "more than 1 rows"):
            execute_query(
                self.connection,
                StructuredQuery(
                    operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                    start=START,
                    end=END,
                    row_limit=1,
                ),
            )

    def test_values_are_bound_not_interpolated(self) -> None:
        query = validate_query(
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0001",
                start=START,
                end=END,
            )
        )
        sql, parameters = build_query(query)
        self.assertNotIn("user-0001", sql)
        self.assertIn("user-0001", parameters)
        self.assertIn("%s", sql)


if __name__ == "__main__":
    unittest.main()
