"""FastAPI surface for bounded structured analytics."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
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


class StructuredQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: QueryOperation
    start: datetime
    end: datetime
    metric: Literal["heart_rate"] = "heart_rate"
    user_id: str | None = None
    row_limit: int = Field(default=MAX_RESPONSE_ROWS, ge=1, le=MAX_RESPONSE_ROWS)

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
) -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title="AI-Queryable Health Data System",
        version="1.0.0",
        description=(
            "Bounded synthetic heart-rate analytics. "
            "No endpoint accepts arbitrary SQL."
        ),
    )
    app.state.database_url = database_url or settings.database_url
    app.state.planner = planner or OllamaPlanner(
        settings.ollama_url,
        settings.ollama_model,
        settings.ollama_timeout_seconds,
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
        responses={
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def structured_query(
        request: StructuredQueryRequest,
        connection: DatabaseConnection,
    ) -> dict[str, Any]:
        return execute_query(connection, request.to_domain()).as_dict()

    @app.post(
        "/v1/query/natural-language",
        tags=["analytics"],
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
        interpreted = interpret_question(
            request.question,
            http_request.app.state.planner,
        )
        result = execute_query(connection, interpreted)
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
            "result": result.as_dict(),
        }

    return app


app = create_app()
