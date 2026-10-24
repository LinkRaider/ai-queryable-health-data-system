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

    def test_period_time_weighted_means_and_contributing_durations(self) -> None:
        for operation, mean, duration in (
            ("user_period_average", 85.0, 2160.0),
            ("user_period_resting_average", 62.5, 1440.0),
        ):
            with self.subTest(operation=operation):
                result = execute_query(self.connection, StructuredQuery(
                    operation=operation, user_id="user-0001", start=START, end=END,
                ))
                self.assertEqual(({
                    "time_weighted_mean_bpm": mean,
                    "contributing_duration_seconds": duration,
                },), result.rows)
                self.assertEqual(START.isoformat(), result.as_dict()["start"])
                self.assertEqual(END.isoformat(), result.as_dict()["end"])
                self.assertEqual(
                    "time_weighted_observation_hold",
                    result.as_dict()["aggregation_basis"],
                )
                self.assertEqual(360, result.as_dict()["max_hold_seconds"])
                sql, parameters = build_query(validate_query(StructuredQuery(
                    operation=operation, user_id="user-0001", start=START, end=END,
                )))
                self.assertNotIn("user-0001", sql)
                self.assertIn("user-0001", parameters)

    def test_period_boundaries_empty_and_unknown_user(self) -> None:
        for operation in ("user_period_average", "user_period_resting_average"):
            with self.subTest(operation=operation):
                result = execute_query(self.connection, StructuredQuery(
                    operation=operation, user_id="user-0001", start=START,
                    end=START + timedelta(hours=1),
                ))
                self.assertEqual(40.0, result.rows[0]["time_weighted_mean_bpm"])
                self.assertEqual(
                    360.0,
                    result.rows[0]["contributing_duration_seconds"],
                )
                for user, start, end in (
                    ("user-0001", END + timedelta(days=1), END + timedelta(days=2)),
                    ("user-0002", START, START + timedelta(hours=1)),
                ):
                    self.assertEqual((), execute_query(self.connection, StructuredQuery(
                        operation=operation, user_id=user, start=start, end=end,
                    )).rows)
                with self.assertRaises(UnknownUserError):
                    execute_query(self.connection, StructuredQuery(
                        operation=operation, user_id="user-9999", start=START, end=END,
                    ))
                for kwargs in (
                    {"user_id": None}, {"user_id": "user-0001-extra"},
                    {"metric": "steps"}, {"row_limit": 0}, {"row_limit": 1001},
                    {"end": START + timedelta(days=32)},
                    {"start": START.replace(tzinfo=None)}, {"end": START},
                ):
                    values = dict(operation=operation, user_id="user-0001", start=START, end=END)
                    values.update(kwargs)
                    with self.assertRaises(AnalyticalQueryError):
                        validate_query(StructuredQuery(**values))
        # A matching non-rest reading is not evidence of a resting observation.
        self.assertEqual((), execute_query(self.connection, StructuredQuery(
            operation="user_period_resting_average", user_id="user-0001",
            start=START + timedelta(hours=3), end=START + timedelta(hours=4),
        )).rows)

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
                    "time_weighted_mean_bpm": 60.0,
                    "contributing_duration_seconds": 1080.0,
                },
                {
                    "day": "2026-01-06",
                    "time_weighted_mean_bpm": 70.0,
                    "contributing_duration_seconds": 360.0,
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
                    "time_weighted_mean_bpm": 62.5,
                    "contributing_duration_seconds": 1440.0,
                },
                {
                    "activity_state": "ordinary_activity",
                    "time_weighted_mean_bpm": 100.0,
                    "contributing_duration_seconds": 360.0,
                },
                {
                    "activity_state": "workout",
                    "time_weighted_mean_bpm": 160.0,
                    "contributing_duration_seconds": 360.0,
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
                    "time_weighted_mean_bpm": 61.7,
                    "contributing_user_seconds": 2160.0,
                    "contributing_user_count": 2,
                },
                {
                    "day": "2026-01-06",
                    "time_weighted_mean_bpm": 113.3,
                    "contributing_user_seconds": 1080.0,
                    "contributing_user_count": 2,
                },
            ),
            result.rows,
        )

    def test_population_mean_weights_user_time_not_users_equally(self) -> None:
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                start=START,
                end=START + timedelta(days=1),
            ),
        )

        row = result.rows[0]
        self.assertEqual(2160.0, row["contributing_user_seconds"])
        self.assertEqual(2, row["contributing_user_count"])
        self.assertEqual(61.7, row["time_weighted_mean_bpm"])
        self.assertNotEqual(
            57.5,
            row["time_weighted_mean_bpm"],
            "the API pools valid user-seconds instead of weighting users equally",
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
                    "time_weighted_mean_bpm": 40.0,
                    "contributing_duration_seconds": 360.0,
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
                    "time_weighted_mean_bpm": 50.0,
                    "contributing_duration_seconds": 360.0,
                },
                {
                    "day": "2026-01-05",
                    "time_weighted_mean_bpm": 70.0,
                    "contributing_duration_seconds": 360.0,
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

    def _insert_fixture_user(
        self,
        user_id: str,
        timezone_name: str,
        observations: list[tuple[datetime, float, str]],
        event_id_start: int,
    ) -> None:
        device_id = user_id.replace("user-", "device-")
        self.connection.execute(
            """
            INSERT INTO users (
                user_id, device_id, timezone_name, resting_baseline_bpm, profile
            )
            VALUES (%s, %s, %s, 60.0, %s)
            """,
            (user_id, device_id, timezone_name, Jsonb({})),
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
                %s,
                %s,
                %s,
                'heart_rate',
                %s,
                'bpm',
                %s
            )
            """,
            [
                (
                    uuid.UUID(int=event_id_start + index),
                    user_id,
                    device_id,
                    observed_at,
                    value,
                    activity_state,
                )
                for index, (observed_at, value, activity_state) in enumerate(
                    observations
                )
            ],
        )
        self.connection.commit()
        self.addCleanup(self._delete_fixture_user, user_id)

    def _delete_fixture_user(self, user_id: str) -> None:
        self.connection.execute(
            "DELETE FROM heart_rate_observations WHERE user_id = %s",
            (user_id,),
        )
        self.connection.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        self.connection.commit()

    def test_unequal_intervals_change_the_mean_from_sample_arithmetic(self) -> None:
        query_start = datetime(2026, 2, 1, tzinfo=UTC)
        self._insert_fixture_user(
            "user-0010",
            "UTC",
            [
                (query_start, 60.0, "rest"),
                (query_start + timedelta(minutes=2), 120.0, "rest"),
                (query_start + timedelta(minutes=6), 90.0, "rest"),
            ],
            2000,
        )

        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.USER_PERIOD_AVERAGE,
                user_id="user-0010",
                start=query_start,
                end=query_start + timedelta(minutes=10),
            ),
        )

        self.assertEqual(
            (
                {
                    "time_weighted_mean_bpm": 96.0,
                    "contributing_duration_seconds": 600.0,
                },
            ),
            result.rows,
        )
        self.assertNotEqual(90.0, result.rows[0]["time_weighted_mean_bpm"])
        self.assertEqual(600.0, result.quality["observed_duration_seconds"])
        self.assertEqual(0, result.quality["gap_count"])

    def test_next_reading_is_found_before_rest_filter_and_quality_uses_all_states(
        self,
    ) -> None:
        query_start = datetime(2026, 2, 2, tzinfo=UTC)
        self._insert_fixture_user(
            "user-0011",
            "UTC",
            [
                (query_start, 60.0, "rest"),
                (query_start + timedelta(minutes=2), 120.0, "workout"),
            ],
            2100,
        )

        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.USER_PERIOD_RESTING_AVERAGE,
                user_id="user-0011",
                start=query_start,
                end=query_start + timedelta(minutes=10),
            ),
        )

        self.assertEqual(
            (
                {
                    "time_weighted_mean_bpm": 60.0,
                    "contributing_duration_seconds": 120.0,
                },
            ),
            result.rows,
        )
        self.assertEqual(480.0, result.quality["observed_duration_seconds"])
        self.assertEqual(120.0, result.quality["unobserved_duration_seconds"])
        self.assertEqual(1, result.quality["gap_count"])

    def test_carry_in_reading_uses_only_remaining_validity(self) -> None:
        query_start = datetime(2026, 2, 3, tzinfo=UTC)
        self._insert_fixture_user(
            "user-0012",
            "UTC",
            [(query_start - timedelta(minutes=5), 80.0, "rest")],
            2200,
        )

        contributing = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.USER_PERIOD_AVERAGE,
                user_id="user-0012",
                start=query_start,
                end=query_start + timedelta(minutes=10),
            ),
        )
        expired = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.USER_PERIOD_AVERAGE,
                user_id="user-0012",
                start=query_start + timedelta(minutes=2),
                end=query_start + timedelta(minutes=12),
            ),
        )

        self.assertEqual(60.0, contributing.rows[0]["contributing_duration_seconds"])
        self.assertEqual(80.0, contributing.rows[0]["time_weighted_mean_bpm"])
        self.assertEqual((), expired.rows)
        self.assertEqual(600.0, expired.quality["unobserved_duration_seconds"])
        self.assertEqual(1, expired.quality["gap_count"])

    def test_quality_includes_initial_interior_and_final_gaps(self) -> None:
        query_start = datetime(2026, 2, 4, tzinfo=UTC)
        self._insert_fixture_user(
            "user-0013",
            "UTC",
            [
                (query_start + timedelta(minutes=5), 60.0, "rest"),
                (query_start + timedelta(minutes=10), 90.0, "rest"),
                (query_start + timedelta(minutes=25), 120.0, "rest"),
            ],
            2300,
        )

        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.USER_PERIOD_AVERAGE,
                user_id="user-0013",
                start=query_start,
                end=query_start + timedelta(minutes=35),
            ),
        )

        self.assertEqual(91.8, result.rows[0]["time_weighted_mean_bpm"])
        self.assertEqual(1020.0, result.rows[0]["contributing_duration_seconds"])
        self.assertEqual(
            {
                "scope": "user",
                "requested_duration_seconds": 2100.0,
                "observed_duration_seconds": 1020.0,
                "unobserved_duration_seconds": 1080.0,
                "gap_count": 3,
                "largest_gap_seconds": 540.0,
            },
            result.quality,
        )

    def test_intervals_split_at_local_and_utc_midnight(self) -> None:
        local_start = datetime(2026, 2, 5, 7, 58, tzinfo=UTC)
        self._insert_fixture_user(
            "user-0014",
            "America/Los_Angeles",
            [
                (local_start, 60.0, "rest"),
                (local_start + timedelta(minutes=4), 120.0, "rest"),
            ],
            2400,
        )
        local_result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.DAILY_USER_AVERAGE,
                user_id="user-0014",
                start=local_start,
                end=local_start + timedelta(minutes=7),
            ),
        )
        self.assertEqual(
            (
                {
                    "day": "2026-02-04",
                    "time_weighted_mean_bpm": 60.0,
                    "contributing_duration_seconds": 120.0,
                },
                {
                    "day": "2026-02-05",
                    "time_weighted_mean_bpm": 96.0,
                    "contributing_duration_seconds": 300.0,
                },
            ),
            local_result.rows,
        )

        utc_start = datetime(2026, 2, 5, 23, 58, tzinfo=UTC)
        self._insert_fixture_user(
            "user-0015",
            "UTC",
            [
                (utc_start, 60.0, "rest"),
                (utc_start + timedelta(minutes=4), 120.0, "rest"),
            ],
            2500,
        )
        utc_result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                start=utc_start,
                end=utc_start + timedelta(minutes=7),
            ),
        )
        self.assertEqual(
            (
                {
                    "day": "2026-02-05",
                    "time_weighted_mean_bpm": 60.0,
                    "contributing_user_seconds": 120.0,
                    "contributing_user_count": 1,
                },
                {
                    "day": "2026-02-06",
                    "time_weighted_mean_bpm": 96.0,
                    "contributing_user_seconds": 300.0,
                    "contributing_user_count": 1,
                },
            ),
            utc_result.rows,
        )

    def test_local_day_duration_respects_daylight_saving_transitions(self) -> None:
        cases = (
            (
                "user-0016",
                datetime(2026, 3, 8, 5, tzinfo=UTC),
                datetime(2026, 3, 9, 4, tzinfo=UTC),
                82800.0,
                2600,
            ),
            (
                "user-0017",
                datetime(2026, 11, 1, 4, tzinfo=UTC),
                datetime(2026, 11, 2, 5, tzinfo=UTC),
                90000.0,
                2700,
            ),
        )
        for user_id, query_start, query_end, expected_seconds, event_id in cases:
            with self.subTest(user_id=user_id):
                self._insert_fixture_user(
                    user_id,
                    "America/New_York",
                    [(query_start, 75.0, "rest")],
                    event_id,
                )
                result = execute_query(
                    self.connection,
                    StructuredQuery(
                        operation=QueryOperation.DAILY_USER_AVERAGE,
                        user_id=user_id,
                        start=query_start,
                        end=query_end,
                    ),
                    max_hold_seconds=100_000,
                )
                self.assertEqual(
                    expected_seconds,
                    result.rows[0]["contributing_duration_seconds"],
                )
                self.assertEqual(
                    expected_seconds,
                    result.quality["requested_duration_seconds"],
                )

    def test_population_pools_observed_user_seconds_and_exposes_denominator(
        self,
    ) -> None:
        query_start = datetime(2026, 4, 1, tzinfo=UTC)
        self._insert_fixture_user(
            "user-0018",
            "UTC",
            [
                (query_start, 60.0, "rest"),
                (query_start + timedelta(minutes=6), 60.0, "rest"),
            ],
            2800,
        )
        self._insert_fixture_user(
            "user-0019",
            "UTC",
            [(query_start, 120.0, "rest")],
            2900,
        )

        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                start=query_start,
                end=query_start + timedelta(minutes=10),
            ),
        )

        self.assertEqual(82.5, result.rows[0]["time_weighted_mean_bpm"])
        self.assertEqual(960.0, result.rows[0]["contributing_user_seconds"])
        self.assertEqual(2, result.rows[0]["contributing_user_count"])
        self.assertEqual(4, result.quality["expected_user_count"])
        self.assertEqual(2, result.quality["observed_user_count"])
        self.assertEqual(2400.0, result.quality["requested_user_seconds"])
        self.assertEqual(960.0, result.quality["observed_user_seconds"])
        self.assertEqual(1440.0, result.quality["unobserved_user_seconds"])
        self.assertEqual(3, result.quality["gap_count"])
        self.assertEqual(600.0, result.quality["largest_individual_gap_seconds"])

    def test_insertion_order_does_not_change_weighted_results(self) -> None:
        query_start = datetime(2026, 4, 2, tzinfo=UTC)
        ordered = [
            (query_start, 60.0, "rest"),
            (query_start + timedelta(minutes=2), 120.0, "rest"),
            (query_start + timedelta(minutes=6), 90.0, "rest"),
        ]
        self._insert_fixture_user("user-0020", "UTC", ordered, 3000)
        self._insert_fixture_user("user-0021", "UTC", list(reversed(ordered)), 3100)

        results = [
            execute_query(
                self.connection,
                StructuredQuery(
                    operation=QueryOperation.USER_PERIOD_AVERAGE,
                    user_id=user_id,
                    start=query_start,
                    end=query_start + timedelta(minutes=10),
                ),
            )
            for user_id in ("user-0020", "user-0021")
        ]

        self.assertEqual(results[0].rows, results[1].rows)
        self.assertEqual(results[0].quality, results[1].quality)

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
        self.assertEqual(
            {
                "scope": "user",
                "requested_duration_seconds": 86400.0,
                "observed_duration_seconds": 0.0,
                "unobserved_duration_seconds": 86400.0,
                "gap_count": 1,
                "largest_gap_seconds": 86400.0,
            },
            result.quality,
        )

    def test_population_without_data_reports_each_expected_user_gap(self) -> None:
        start = START + timedelta(days=10)
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                start=start,
                end=start + timedelta(hours=1),
            ),
        )

        self.assertEqual((), result.rows)
        self.assertEqual(
            {
                "scope": "population",
                "requested_duration_seconds": 3600.0,
                "requested_user_seconds": 7200.0,
                "observed_user_seconds": 0.0,
                "unobserved_user_seconds": 7200.0,
                "expected_user_count": 2,
                "observed_user_count": 0,
                "gap_count": 2,
                "largest_individual_gap_seconds": 3600.0,
            },
            result.quality,
        )

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


class EmptyPopulationQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = connect(TEST_DATABASE_URL)
        apply_schema(cls.connection)
        cls.connection.execute(
            "TRUNCATE heart_rate_observations, ingestion_runs, users CASCADE"
        )
        cls.connection.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def test_empty_users_table_returns_zero_population_quality(self) -> None:
        result = execute_query(
            self.connection,
            StructuredQuery(
                operation=QueryOperation.POPULATION_DAILY_AVERAGE,
                start=START,
                end=START + timedelta(hours=1),
            ),
        )

        self.assertEqual((), result.rows)
        self.assertEqual(
            {
                "scope": "population",
                "requested_duration_seconds": 3600.0,
                "requested_user_seconds": 0.0,
                "observed_user_seconds": 0.0,
                "unobserved_user_seconds": 0.0,
                "expected_user_count": 0,
                "observed_user_count": 0,
                "gap_count": 0,
                "largest_individual_gap_seconds": 0.0,
            },
            result.quality,
        )


if __name__ == "__main__":
    unittest.main()
