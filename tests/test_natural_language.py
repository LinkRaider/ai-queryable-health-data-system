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
    interpret_model_plan,
)

TEST_DATABASE_URL = os.getenv(
    "HEALTH_DATA_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/health_data",
)
CASES_PATH = Path(__file__).parent / "evaluation" / "natural_language_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))
SUPPORTED = [case for case in CASES if case["category"] == "supported"]


class EvaluationPlanner:
    def __init__(self, cases=SUPPORTED) -> None:
        self.plans = {case["question"]: case["plan"] for case in cases}
        self.call_count = 0

    def available(self) -> bool:
        return True

    def plan(self, question: str):
        self.call_count += 1
        for original_question, plan in self.plans.items():
            if question.startswith(original_question):
                return plan
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
                %s,
                'complete',
                4,
                1,
                1,
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

    def test_explicit_supported_evaluation_questions(self) -> None:
        planner = EvaluationPlanner()
        client = TestClient(create_app(TEST_DATABASE_URL, planner=planner))
        for case in SUPPORTED:
            with self.subTest(case=case["name"]):
                response = client.post(
                    "/v1/query/natural-language",
                    json={"question": case["question"]},
                )
                self.assertEqual(200, response.status_code, response.text)
                payload = response.json()
                self.assertEqual(
                    case["plan"]["operation"],
                    payload["interpreted_query"]["operation"],
                )
                self.assertEqual(case["plan"]["user_id"], payload["interpreted_query"]["user_id"])
                self.assertEqual("heart_rate", payload["interpreted_query"]["metric"])
                self.assertTrue(payload["result"]["rows"])
                temporal = payload["temporal_interpretation"]
                self.assertEqual(case["plan"]["start"], temporal["start"])
                self.assertEqual(case["plan"]["end"], temporal["end"])
                if "last week" in case["question"]:
                    self.assertEqual("previous_iso_week", temporal["kind"])
                    self.assertEqual("UTC", temporal["timezone"])
                    self.assertEqual(
                        "2026-01-12T00:00:00Z",
                        temporal["reference_time"],
                    )
                else:
                    self.assertEqual("explicit_range", temporal["kind"])
                    self.assertIsNone(temporal["timezone"])
                    self.assertIsNone(temporal["reference_time"])
        self.assertEqual(len(SUPPORTED), planner.call_count)

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
