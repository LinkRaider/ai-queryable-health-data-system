from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from health_data.api import create_app
from health_data.db import apply_schema, connect

TEST_DATABASE_URL = os.getenv(
    "HEALTH_DATA_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/health_data",
)
START = datetime(2026, 1, 5, tzinfo=UTC)
END = datetime(2026, 1, 7, tzinfo=UTC)


class AvailablePlanner:
    def available(self) -> bool:
        return True

    def plan(self, _question: str):
        raise AssertionError("planner is not used by structured endpoint tests")


class StructuredQueryApiTests(unittest.TestCase):
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
        cls.connection.execute(
            """
            INSERT INTO users (
                user_id, device_id, timezone_name, resting_baseline_bpm, profile
            )
            VALUES ('user-0001', 'device-0001', 'UTC', 60.0, %s)
            """,
            (Jsonb({}),),
        )
        observations = [
            (uuid.UUID(int=101), START + timedelta(hours=1), 60.0),
            (uuid.UUID(int=102), START + timedelta(hours=2), 80.0),
            (uuid.UUID(int=103), START + timedelta(days=1, hours=1), 70.0),
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
                'rest'
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
                3,
                1,
                1,
                3,
                now()
            )
            """,
            ("a" * 64, Jsonb({"fixture": True})),
        )
        cls.connection.commit()
        cls.client = TestClient(
            create_app(TEST_DATABASE_URL, planner=AvailablePlanner())
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    @staticmethod
    def request_payload(**overrides):
        payload = {
            "operation": "daily_user_average",
            "user_id": "user-0001",
            "metric": "heart_rate",
            "start": START.isoformat(),
            "end": END.isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_health_endpoints_reflect_completed_bootstrap(self) -> None:
        self.assertEqual({"status": "alive"}, self.client.get("/health/live").json())
        response = self.client.get("/health/ready")
        self.assertEqual(200, response.status_code)
        self.assertEqual("complete", response.json()["ingestion"])

    def test_structured_query_returns_known_result(self) -> None:
        response = self.client.post("/v1/query", json=self.request_payload())
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [
                {
                    "day": "2026-01-05",
                    "average_bpm": 70.0,
                    "observation_count": 2,
                },
                {
                    "day": "2026-01-06",
                    "average_bpm": 70.0,
                    "observation_count": 1,
                },
            ],
            response.json()["rows"],
        )

    def test_empty_query_returns_200_with_empty_rows(self) -> None:
        response = self.client.post(
            "/v1/query",
            json=self.request_payload(
                start=(START + timedelta(days=10)).isoformat(),
                end=(START + timedelta(days=11)).isoformat(),
            ),
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json()["rows"])

    def test_invalid_requests_return_structured_422_errors(self) -> None:
        cases = (
            self.request_payload(start="2026-01-05T00:00:00"),
            self.request_payload(metric="steps"),
            self.request_payload(operation="raw_sql"),
            self.request_payload(sql="SELECT * FROM heart_rate_observations"),
            self.request_payload(end=(START + timedelta(days=32)).isoformat()),
            self.request_payload(user_id="user-9999"),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/v1/query", json=payload)
                self.assertEqual(422, response.status_code)
                self.assertIn("error", response.json())
                self.assertIn("code", response.json()["error"])
                self.assertIn("detail", response.json()["error"])

    def test_oversized_response_returns_413_without_partial_rows(self) -> None:
        response = self.client.post(
            "/v1/query",
            json={
                "operation": "population_daily_average",
                "metric": "heart_rate",
                "start": START.isoformat(),
                "end": END.isoformat(),
                "row_limit": 1,
            },
        )
        self.assertEqual(413, response.status_code)
        self.assertEqual("response_too_large", response.json()["error"]["code"])
        self.assertNotIn("rows", response.json())

    def test_openapi_documents_bounded_request_not_sql(self) -> None:
        schema = self.client.get("/openapi.json").json()
        properties = schema["components"]["schemas"]["StructuredQueryRequest"][
            "properties"
        ]
        self.assertEqual(
            {"operation", "start", "end", "metric", "user_id", "row_limit"},
            set(properties),
        )
        self.assertNotIn("sql", properties)

    def test_database_unavailability_returns_503_without_connection_details(self) -> None:
        unavailable_client = TestClient(
            create_app(
                "postgresql://postgres@127.0.0.1:1/health_data?connect_timeout=1",
                planner=AvailablePlanner(),
            )
        )
        response = unavailable_client.post("/v1/query", json=self.request_payload())
        self.assertEqual(503, response.status_code)
        self.assertEqual("database_unavailable", response.json()["error"]["code"])
        self.assertNotIn("127.0.0.1", response.text)


if __name__ == "__main__":
    unittest.main()
