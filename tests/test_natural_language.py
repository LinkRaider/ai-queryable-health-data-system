from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from health_data.api import create_app
from health_data.db import apply_schema, connect
from health_data.natural_language import (
    InvalidModelOutputError,
    ModelUnavailableError,
    OllamaPlanner,
    TIMESTAMP_PATTERN,
    interpret_model_plan,
)

TEST_DATABASE_URL = os.getenv(
    "HEALTH_DATA_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/health_data",
)
CASES_PATH = Path(__file__).parent / "evaluation" / "natural_language_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))
SUPPORTED = [
    case
    for case in CASES
    if case["category"] in {"supported", "supported_relative"}
]
TEST_REFERENCE_TIME = datetime(2026, 1, 12, tzinfo=UTC)


class EvaluationPlanner:
    def __init__(self, cases=SUPPORTED) -> None:
        self.cases = cases
        self.call_count = 0

    def available(self) -> bool:
        return True

    def plan(self, question: str):
        self.call_count += 1
        for case in self.cases:
            if question.startswith(case["question"]):
                if case["category"] == "supported":
                    return case["plan"]
                timestamps = TIMESTAMP_PATTERN.findall(question)
                return {
                    "operation": case["expected_operation"],
                    "user_id": case["expected_user_id"],
                    "metric": "heart_rate",
                    "start": timestamps[0],
                    "end": timestamps[1],
                }
        raise KeyError(question)


class FixedPlanner:
    def __init__(self, output=None, error=None, available=True) -> None:
        self.output = output
        self.error = error
        self.is_available = available
        self.call_count = 0

    def available(self) -> bool:
        return self.is_available

    def plan(self, _question: str):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.output


class NaturalLanguageEvaluationTests(unittest.TestCase):
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
        cls.connection.execute(
            """
            INSERT INTO users (
                user_id, device_id, timezone_name, resting_baseline_bpm, profile
            )
            VALUES ('user-0001', 'device-0001', 'UTC', 60.0, %s)
            """,
            (Jsonb({}),),
        )
        start = datetime(2026, 1, 5, tzinfo=UTC)
        observations = [
            (uuid.UUID(int=201), start + timedelta(hours=1), 60.0, "rest"),
            (uuid.UUID(int=202), start + timedelta(hours=2), 80.0, "rest"),
            (
                uuid.UUID(int=203),
                start + timedelta(hours=3),
                100.0,
                "ordinary_activity",
            ),
            (uuid.UUID(int=204), start + timedelta(hours=4), 150.0, "workout"),
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
            VALUES (
                %s,
                'health.heart_rate.observation.v1',
                'user-0001',
                'device-0001',
                %s,
                'heart_rate',
                %s,
                'bpm',
                %s
            )
            """,
            observations,
        )
        cls.connection.execute(
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
                %s,
                'complete',
                4,
                4,
                now()
            )
            """,
            ("b" * 64, Jsonb({"fixture": True})),
        )
        cls.connection.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def test_supported_evaluation_questions_with_injected_clock(self) -> None:
        planner = EvaluationPlanner()
        client = TestClient(
            create_app(
                TEST_DATABASE_URL,
                planner=planner,
                query_reference_time=TEST_REFERENCE_TIME,
            )
        )
        for case in SUPPORTED:
            with self.subTest(case=case["name"]):
                response = client.post(
                    "/v1/query/natural-language",
                    json={"question": case["question"]},
                )
                self.assertEqual(200, response.status_code, response.text)
                payload = response.json()
                expected_operation = case.get(
                    "expected_operation",
                    case.get("plan", {}).get("operation"),
                )
                expected_user_id = case.get(
                    "expected_user_id",
                    case.get("plan", {}).get("user_id"),
                )
                self.assertEqual(
                    expected_operation,
                    payload["interpreted_query"]["operation"],
                )
                self.assertEqual(
                    expected_user_id,
                    payload["interpreted_query"]["user_id"],
                )
                self.assertEqual("heart_rate", payload["interpreted_query"]["metric"])
                self.assertTrue(payload["result"]["rows"])
                temporal = payload["temporal_interpretation"]
                if case["category"] == "supported_relative":
                    self.assertEqual("2026-01-05T00:00:00Z", temporal["start"])
                    self.assertEqual("2026-01-12T00:00:00Z", temporal["end"])
                    self.assertEqual("previous_iso_week", temporal["kind"])
                    self.assertEqual("UTC", temporal["timezone"])
                    self.assertEqual(
                        "2026-01-12T00:00:00Z",
                        temporal["reference_time"],
                    )
                else:
                    self.assertEqual(case["plan"]["start"], temporal["start"])
                    self.assertEqual(case["plan"]["end"], temporal["end"])
                    self.assertEqual("explicit_range", temporal["kind"])
                    self.assertIsNone(temporal["timezone"])
                    self.assertIsNone(temporal["reference_time"])
        self.assertEqual(len(SUPPORTED), planner.call_count)

    def test_last_week_uses_request_time_and_returns_empty_when_snapshot_has_no_data(
        self,
    ) -> None:
        question = (
            "What was user-0001's average resting heart rate each day last week?"
        )
        reference_time = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
        planner = FixedPlanner(
            output={
                "operation": "daily_user_average",
                "user_id": "user-0001",
                "metric": "heart_rate",
                "start": "2026-10-12T00:00:00Z",
                "end": "2026-10-19T00:00:00Z",
            }
        )
        response = TestClient(
            create_app(
                TEST_DATABASE_URL,
                planner=planner,
                query_reference_time=reference_time,
            )
        ).post(
            "/v1/query/natural-language",
            json={"question": question},
        )

        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual([], payload["result"]["rows"])
        self.assertEqual(
            "2026-10-20T13:00:00Z",
            payload["temporal_interpretation"]["reference_time"],
        )
        self.assertEqual(
            "2026-10-12T00:00:00Z",
            payload["temporal_interpretation"]["start"],
        )
        self.assertEqual(
            "2026-10-19T00:00:00Z",
            payload["temporal_interpretation"]["end"],
        )

    def test_unsupported_intents_are_rejected_before_model_call(self) -> None:
        unsupported_cases = [
            case
            for case in CASES
            if case["category"] in {
                "unsupported_operation",
                "unsupported_question",
            }
        ]
        for case in unsupported_cases:
            with self.subTest(case=case["name"]):
                planner = FixedPlanner(output={})
                response = TestClient(
                    create_app(TEST_DATABASE_URL, planner=planner)
                ).post(
                    "/v1/query/natural-language",
                    json={"question": case["question"]},
                )
                self.assertEqual(422, response.status_code)
                self.assertEqual(
                    case["category"],
                    response.json()["error"]["code"],
                )
                self.assertEqual(0, planner.call_count)

    def test_fractional_seconds_are_accepted_in_explicit_range(self) -> None:
        question = (
            "What was user-0001's average resting heart rate each day from "
            "2026-01-05T00:00:00.500Z to 2026-01-06T00:00:00.500Z?"
        )
        planner = FixedPlanner(
            output={
                "operation": "daily_user_average",
                "user_id": "user-0001",
                "metric": "heart_rate",
                "start": "2026-01-05T00:00:00.500Z",
                "end": "2026-01-06T00:00:00.500Z",
            }
        )
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={"question": question},
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, planner.call_count)
        self.assertEqual(
            "2026-01-05T00:00:00.500000Z",
            response.json()["temporal_interpretation"]["start"],
        )

    def test_conflicting_relative_and_explicit_dates_are_rejected(self) -> None:
        planner = FixedPlanner(output={})
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={
                "question": (
                    "What was user-0001's average resting heart rate each day today "
                    "from 2026-01-05T00:00:00Z to 2026-01-06T00:00:00Z?"
                )
            },
        )

        self.assertEqual(422, response.status_code)
        self.assertEqual("ambiguous_question", response.json()["error"]["code"])
        self.assertEqual(0, planner.call_count)

    def test_invalid_explicit_timestamps_return_structured_422(self) -> None:
        invalid_starts = (
            "2026-02-30T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-01-05T25:00:00Z",
            "2026-01-05T00:00:00+25:00",
        )
        for invalid_start in invalid_starts:
            with self.subTest(start=invalid_start):
                planner = FixedPlanner(output={})
                response = TestClient(
                    create_app(TEST_DATABASE_URL, planner=planner)
                ).post(
                    "/v1/query/natural-language",
                    json={
                        "question": (
                            "What was user-0001's average resting heart rate each "
                            f"day from {invalid_start} to 2026-03-02T00:00:00Z?"
                        )
                    },
                )
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(
                    "invalid_timestamp",
                    response.json()["error"]["code"],
                )
                self.assertEqual(0, planner.call_count)

    def test_malformed_user_id_does_not_ground_valid_prefix(self) -> None:
        planner = FixedPlanner(
            output={
                "operation": "daily_user_average",
                "user_id": "user-0001",
                "metric": "heart_rate",
                "start": "2026-01-05T00:00:00Z",
                "end": "2026-01-07T00:00:00Z",
            }
        )
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={
                "question": (
                    "What was user-0001-extra's average resting heart rate each "
                    "day from 2026-01-05T00:00:00Z to 2026-01-07T00:00:00Z?"
                )
            },
        )

        self.assertEqual(422, response.status_code)
        self.assertEqual("ambiguous_question", response.json()["error"]["code"])
        self.assertEqual(0, planner.call_count)

    def test_daily_average_requires_explicit_daily_grouping(self) -> None:
        question = (
            "What was user-0001's average resting heart rate from "
            "2026-01-05T00:00:00Z to 2026-01-07T00:00:00Z?"
        )
        planner = FixedPlanner(
            output={
                "operation": "daily_user_average",
                "user_id": "user-0001",
                "metric": "heart_rate",
                "start": "2026-01-05T00:00:00Z",
                "end": "2026-01-07T00:00:00Z",
            }
        )
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={"question": question},
        )

        self.assertEqual(422, response.status_code)
        self.assertEqual("unsupported_operation", response.json()["error"]["code"])
        self.assertEqual(0, planner.call_count)

    def test_ambiguous_question_is_rejected_before_model_call(self) -> None:
        case = next(case for case in CASES if case["category"] == "ambiguous")
        planner = FixedPlanner(output={})
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={"question": case["question"]},
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("ambiguous_question", response.json()["error"]["code"])
        self.assertEqual(0, planner.call_count)

    def test_first_person_last_week_requires_explicit_user_identity(self) -> None:
        planner = FixedPlanner(output={})
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={
                "question": (
                    "What was my average resting heart rate last week?"
                )
            },
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("ambiguous_question", response.json()["error"]["code"])
        self.assertIn("explicit user ID", response.json()["error"]["detail"])
        self.assertEqual(0, planner.call_count)

    def test_unsupported_metric_is_rejected_before_model_call(self) -> None:
        case = next(
            case for case in CASES if case["category"] == "unsupported_metric"
        )
        planner = FixedPlanner(output={})
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={"question": case["question"]},
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("unsupported_metric", response.json()["error"]["code"])
        self.assertEqual(0, planner.call_count)

    def test_invalid_model_output_with_sql_is_rejected(self) -> None:
        case = next(
            case for case in CASES if case["category"] == "invalid_model_output"
        )
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=FixedPlanner(case["plan"]))
        ).post(
            "/v1/query/natural-language",
            json={"question": case["question"]},
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("invalid_model_output", response.json()["error"]["code"])

    def test_model_cannot_invent_a_different_user_or_range(self) -> None:
        case = SUPPORTED[0]
        for invalid_plan in (
            {**case["plan"], "user_id": "user-0002"},
            {**case["plan"], "start": "2026-01-06T00:00:00Z"},
        ):
            with self.subTest(plan=invalid_plan):
                with self.assertRaises(InvalidModelOutputError):
                    interpret_model_plan(case["question"], invalid_plan)

    def test_model_unavailability_returns_503(self) -> None:
        case = next(
            case for case in CASES if case["category"] == "model_unavailable"
        )
        planner = FixedPlanner(
            error=ModelUnavailableError(
                "the local interpretation model is unavailable"
            ),
            available=False,
        )
        response = TestClient(
            create_app(TEST_DATABASE_URL, planner=planner)
        ).post(
            "/v1/query/natural-language",
            json={"question": case["question"]},
        )
        self.assertEqual(503, response.status_code)
        self.assertEqual("model_unavailable", response.json()["error"]["code"])
        self.assertNotIn("http://", response.text)

    @patch("health_data.natural_language.httpx.post")
    def test_non_json_ollama_output_is_rejected(self, post: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"response": "not JSON"}
        post.return_value = response
        planner = OllamaPlanner("http://ollama:11434", "test-model")
        with self.assertRaisesRegex(InvalidModelOutputError, "invalid JSON"):
            planner.plan(SUPPORTED[0]["question"])

    @patch("health_data.natural_language.httpx.post")
    def test_ollama_connection_failure_is_model_unavailable(self, post: Mock) -> None:
        import httpx

        post.side_effect = httpx.ConnectError("connection refused")
        planner = OllamaPlanner("http://ollama:11434", "test-model")
        with self.assertRaisesRegex(ModelUnavailableError, "unavailable"):
            planner.plan(SUPPORTED[0]["question"])


if __name__ == "__main__":
    unittest.main()
