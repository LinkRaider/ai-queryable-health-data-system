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
START = datetime(2026, 10, 5, tzinfo=UTC)
END = datetime(2026, 10, 7, tzinfo=UTC)


class AvailablePlanner:
    def available(self) -> bool:
        return True

    def plan(self, _question: str):
        raise AssertionError("planner is not used by structured endpoint tests")


class RootRedirectTests(unittest.TestCase):
    def test_root_redirects_to_openapi_documentation(self) -> None:
        client = TestClient(create_app(TEST_DATABASE_URL))

        response = client.get("/", follow_redirects=False)

        self.assertEqual(307, response.status_code)
        self.assertEqual("/docs", response.headers["location"])


class StructuredQueryApiTests(unittest.TestCase):
    def test_period_operations_are_documented_and_http_validated(self) -> None:
        client = TestClient(create_app(TEST_DATABASE_URL))
        schema = client.get("/openapi.json").json()
        enum = schema["components"]["schemas"]["QueryOperation"]["enum"]
        self.assertEqual(5, len(enum))
        for operation in ("user_period_average", "user_period_resting_average"):
            self.assertIn(operation, enum)
            request = dict(operation=operation, user_id="user-0001",
                           start="2026-10-05T00:00:00Z", end="2026-10-07T00:00:00Z")
            response = client.post("/v1/query", json=request)
            self.assertEqual(200, response.status_code, response.text)
            row = response.json()["rows"][0]
            self.assertNotIn("day", row)
            self.assertGreater(row["contributing_duration_seconds"], 0)
            self.assertIn("time_weighted_mean_bpm", row)
            self.assertNotIn("mean_recorded_bpm", row)
            for extra in ({"activity_state": "workout"}, {"user_id": None}):
                self.assertEqual(422, client.post("/v1/query", json={**request, **extra}).status_code)

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
                3,
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
                    "day": "2026-10-05",
                    "time_weighted_mean_bpm": 70.0,
                    "contributing_duration_seconds": 720.0,
                },
                {
                    "day": "2026-10-06",
                    "time_weighted_mean_bpm": 70.0,
                    "contributing_duration_seconds": 360.0,
                },
            ],
            response.json()["rows"],
        )
        self.assertEqual(
            "time_weighted_observation_hold",
            response.json()["aggregation_basis"],
        )
        self.assertEqual(360, response.json()["max_hold_seconds"])
        self.assertEqual(
            {
                "scope": "user",
                "requested_duration_seconds": 172800.0,
                "observed_duration_seconds": 1080.0,
                "unobserved_duration_seconds": 171720.0,
                "gap_count": 4,
                "largest_gap_seconds": 82440.0,
            },
            response.json()["quality"],
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
        self.assertEqual(86400.0, response.json()["quality"]["largest_gap_seconds"])

    def test_invalid_requests_return_structured_422_errors(self) -> None:
        cases = (
            self.request_payload(start="2026-10-05T00:00:00"),
            self.request_payload(start=1767571200),
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
        structured_response = schema["paths"]["/v1/query"]["post"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        natural_response = schema["paths"]["/v1/query/natural-language"]["post"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            "#/components/schemas/AnalyticalQueryResponse",
            structured_response["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/NaturalLanguageQueryResponse",
            natural_response["$ref"],
        )
        response_properties = schema["components"]["schemas"][
            "AnalyticalQueryResponse"
        ]["properties"]
        self.assertIn("quality", response_properties)
        self.assertIn("max_hold_seconds", response_properties)
        self.assertEqual(
            "time_weighted_observation_hold",
            response_properties["aggregation_basis"]["const"],
        )
        self.assertNotIn(
            "mean_recorded_bpm",
            schema["components"]["schemas"]["PeriodAverageRow"]["properties"],
        )

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
