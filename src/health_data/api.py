"""FastAPI surface for bounded structured analytics."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
import re
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from psycopg import Connection

from health_data.config import load_settings
from health_data.db import connect
from health_data.natural_language import (
    ModelUnavailableError,
    NaturalLanguageError,
    OllamaPlanner,
    QueryPlanner,
    interpret_question,
)
from health_data.query import (
    AnalyticalQueryError,
    MAX_RESPONSE_ROWS,
    QueryOperation,
    ResponseTooLargeError,
    StructuredQuery,
    execute_query,
)

OFFSET_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class StructuredQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: QueryOperation
    start: datetime
    end: datetime
    metric: Literal["heart_rate"] = "heart_rate"
    user_id: str | None = None
    row_limit: int = Field(default=MAX_RESPONSE_ROWS, ge=1, le=MAX_RESPONSE_ROWS)

    @field_validator("start", "end", mode="before")
    @classmethod
    def require_offset_timestamp_string(cls, value: Any) -> Any:
        if not isinstance(value, str) or OFFSET_TIMESTAMP_PATTERN.fullmatch(value) is None:
            raise ValueError("timestamp must be an offset-aware RFC 3339 string")
        return value

    def to_domain(self) -> StructuredQuery:
        return StructuredQuery(
            operation=self.operation,
            start=self.start,
            end=self.end,
            metric=self.metric,
            user_id=self.user_id,
            row_limit=self.row_limit,
        )


class ErrorBody(BaseModel):
    code: str
    detail: str
    fields: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class NaturalLanguageQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=5, max_length=500)


class DailyAverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str
    time_weighted_mean_bpm: float
    contributing_duration_seconds: float


class PeriodAverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_weighted_mean_bpm: float
    contributing_duration_seconds: float


class ActivityComparisonRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_state: Literal["sleep", "rest", "ordinary_activity", "workout"]
    time_weighted_mean_bpm: float
    contributing_duration_seconds: float


class PopulationAverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str
    time_weighted_mean_bpm: float
    contributing_user_seconds: float
    contributing_user_count: int


class UserQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["user"]
    requested_duration_seconds: float
    observed_duration_seconds: float
    unobserved_duration_seconds: float
    gap_count: int
    largest_gap_seconds: float


class PopulationQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["population"]
    requested_duration_seconds: float
    requested_user_seconds: float
    observed_user_seconds: float
    unobserved_user_seconds: float
    expected_user_count: int
    observed_user_count: int
    gap_count: int
    largest_individual_gap_seconds: float


class AnalyticalQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: QueryOperation
    metric: Literal["heart_rate"]
    start: datetime
    end: datetime
    aggregation_basis: Literal["time_weighted_observation_hold"]
    max_hold_seconds: int
    quality: UserQualitySummary | PopulationQualitySummary
    rows: list[DailyAverageRow | ActivityComparisonRow | PopulationAverageRow | PeriodAverageRow]


class InterpretedQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: QueryOperation
    user_id: str | None
    metric: Literal["heart_rate"]
    start: datetime
    end: datetime
    row_limit: int


class TemporalInterpretationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["explicit_range", "previous_iso_week"]
    start: datetime
    end: datetime
    timezone: str | None
    reference_time: datetime | None


class NaturalLanguageQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    interpreted_query: InterpretedQueryResponse
    temporal_interpretation: TemporalInterpretationResponse
    result: AnalyticalQueryResponse


def _error(
    status_code: int,
    code: str,
    detail: str,
    *,
    fields: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": code, "detail": detail}}
    if fields is not None:
        payload["error"]["fields"] = fields
    return JSONResponse(status_code=status_code, content=payload)


def database_connection(request: Request) -> Generator[Connection, None, None]:
    connection = connect(request.app.state.database_url)
    try:
        yield connection
    finally:
        connection.close()


DatabaseConnection = Annotated[Connection, Depends(database_connection)]


def create_app(
    database_url: str | None = None,
    planner: QueryPlanner | None = None,
    query_reference_time: datetime | None = None,
    relative_date_timezone: str | None = None,
    max_hold_seconds: int | None = None,
) -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title="AI-Queryable Health Data System",
        version="1.0.0",
        description=(
            "Bounded analytics over a fixed synthetic heart-rate snapshot. "
            "Reported means weight held observations by valid elapsed time and "
            "expose unavailable intervals. "
            "No endpoint accepts arbitrary SQL."
        ),
    )
    app.state.database_url = database_url or settings.database_url
    app.state.planner = planner or OllamaPlanner(
        settings.ollama_url,
        settings.ollama_model,
        settings.ollama_timeout_seconds,
    )
    app.state.query_reference_time = query_reference_time
    app.state.relative_date_timezone = (
        relative_date_timezone or settings.relative_date_timezone
    )
    app.state.max_hold_seconds = (
        max_hold_seconds
        if max_hold_seconds is not None
        else settings.max_hold_seconds
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        fields = [
            {
                "location": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
                "type": item["type"],
            }
            for item in error.errors()
        ]
        return _error(422, "invalid_request", "request validation failed", fields=fields)

    @app.exception_handler(AnalyticalQueryError)
    async def analytical_query_error(
        _request: Request,
        error: AnalyticalQueryError,
    ) -> JSONResponse:
        status_code = 413 if isinstance(error, ResponseTooLargeError) else 422
        return _error(status_code, error.code, error.detail)

    @app.exception_handler(NaturalLanguageError)
    async def natural_language_error(
        _request: Request,
        error: NaturalLanguageError,
    ) -> JSONResponse:
        status_code = 503 if isinstance(error, ModelUnavailableError) else 422
        return _error(status_code, error.code, error.detail)

    @app.exception_handler(psycopg.Error)
    async def database_error(_request: Request, _error_value: psycopg.Error) -> JSONResponse:
        return _error(
            503,
            "database_unavailable",
            "the analytical database is unavailable",
        )

    @app.get("/", include_in_schema=False)
    def documentation_redirect() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get(
        "/health/ready",
        tags=["health"],
        responses={503: {"model": ErrorResponse}},
    )
    def readiness(request: Request, connection: DatabaseConnection) -> Any:
        row = connection.execute(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM ingestion_runs WHERE status = 'complete'
                ) AS ingestion_complete,
                EXISTS (
                    SELECT 1 FROM heart_rate_observations
                ) AS observations_present
            """
        ).fetchone()
        connection.commit()
        if not row["ingestion_complete"] or not row["observations_present"]:
            return _error(
                503,
                "not_ready",
                "the deterministic bootstrap has not completed",
            )
        if not request.app.state.planner.available():
            return _error(
                503,
                "model_unavailable",
                "the local interpretation model is unavailable",
            )
        return {
            "status": "ready",
            "database": "ready",
            "ingestion": "complete",
            "model": "ready",
        }

    @app.post(
        "/v1/query",
        tags=["analytics"],
        response_model=AnalyticalQueryResponse,
        responses={
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def structured_query(
        request: StructuredQueryRequest,
        http_request: Request,
        connection: DatabaseConnection,
    ) -> dict[str, Any]:
        return execute_query(
            connection,
            request.to_domain(),
            max_hold_seconds=http_request.app.state.max_hold_seconds,
        ).as_dict()

    @app.post(
        "/v1/query/natural-language",
        tags=["analytics"],
        response_model=NaturalLanguageQueryResponse,
        responses={
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def natural_language_query(
        request: NaturalLanguageQueryRequest,
        http_request: Request,
        connection: DatabaseConnection,
    ) -> dict[str, Any]:
        interpretation = interpret_question(
            request.question,
            http_request.app.state.planner,
            reference_time=http_request.app.state.query_reference_time,
            timezone_name=http_request.app.state.relative_date_timezone,
        )
        interpreted = interpretation.query
        result = execute_query(
            connection,
            interpreted,
            max_hold_seconds=http_request.app.state.max_hold_seconds,
        )
        return {
            "question": request.question,
            "interpreted_query": {
                "operation": QueryOperation(interpreted.operation).value,
                "user_id": interpreted.user_id,
                "metric": interpreted.metric,
                "start": interpreted.start.isoformat(),
                "end": interpreted.end.isoformat(),
                "row_limit": interpreted.row_limit,
            },
            "temporal_interpretation": {
                "kind": interpretation.temporal.kind,
                "start": interpretation.temporal.start.isoformat(),
                "end": interpretation.temporal.end.isoformat(),
                "timezone": interpretation.temporal.timezone_name,
                "reference_time": (
                    interpretation.temporal.reference_time.isoformat()
                    if interpretation.temporal.reference_time is not None
                    else None
                ),
            },
            "result": result.as_dict(),
        }

    return app


app = create_app()
