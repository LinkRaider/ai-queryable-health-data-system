"""Bounded analytical queries over heart-rate observations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from psycopg import Connection

MAX_QUERY_RANGE = timedelta(days=31)
MAX_RESPONSE_ROWS = 1_000
USER_ID_PATTERN = re.compile(r"^user-[0-9]{4}$")


class QueryOperation(StrEnum):
    DAILY_USER_AVERAGE = "daily_user_average"
    ACTIVITY_STATE_COMPARISON = "activity_state_comparison"
    POPULATION_DAILY_AVERAGE = "population_daily_average"


@dataclass(frozen=True, slots=True)
class StructuredQuery:
    operation: QueryOperation | str
    start: datetime
    end: datetime
    metric: str = "heart_rate"
    user_id: str | None = None
    row_limit: int = MAX_RESPONSE_ROWS


@dataclass(frozen=True, slots=True)
class QueryResult:
    operation: QueryOperation
    metric: str
    start: datetime
    end: datetime
    rows: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "metric": self.metric,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "rows": list(self.rows),
        }


class AnalyticalQueryError(ValueError):
    code = "invalid_query"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnknownUserError(AnalyticalQueryError):
    code = "unknown_user"


class ResponseTooLargeError(AnalyticalQueryError):
    code = "response_too_large"


def validate_query(query: StructuredQuery) -> StructuredQuery:
    try:
        operation = QueryOperation(query.operation)
    except ValueError as error:
        raise AnalyticalQueryError(
            f"unsupported operation: {query.operation}"
        ) from error

    if query.metric != "heart_rate":
        raise AnalyticalQueryError(f"unsupported metric: {query.metric}")
    if query.start.tzinfo is None or query.start.utcoffset() is None:
        raise AnalyticalQueryError("start must include a UTC offset")
    if query.end.tzinfo is None or query.end.utcoffset() is None:
        raise AnalyticalQueryError("end must include a UTC offset")
    if query.start >= query.end:
        raise AnalyticalQueryError("start must be before end")
    if query.end - query.start > MAX_QUERY_RANGE:
        raise AnalyticalQueryError("query range may not exceed 31 days")
    if not 1 <= query.row_limit <= MAX_RESPONSE_ROWS:
        raise AnalyticalQueryError(
            f"row_limit must be between 1 and {MAX_RESPONSE_ROWS}"
        )

    single_user = operation in {
        QueryOperation.DAILY_USER_AVERAGE,
        QueryOperation.ACTIVITY_STATE_COMPARISON,
    }
    if single_user:
        if query.user_id is None or USER_ID_PATTERN.fullmatch(query.user_id) is None:
            raise AnalyticalQueryError(
                "this operation requires user_id matching user-[0-9]{4}"
            )
    elif query.user_id is not None:
        raise AnalyticalQueryError(
            "population_daily_average does not accept a user_id"
        )

    return StructuredQuery(
        operation=operation,
        start=query.start,
        end=query.end,
        metric=query.metric,
        user_id=query.user_id,
        row_limit=query.row_limit,
    )


DAILY_USER_AVERAGE_SQL = """
    SELECT
        (o.observed_at AT TIME ZONE u.timezone_name)::date AS day,
        round(avg(o.value), 1)::double precision AS average_bpm,
        count(*)::integer AS observation_count
    FROM heart_rate_observations AS o
    JOIN users AS u ON u.user_id = o.user_id
    WHERE
        o.user_id = %s
        AND o.metric = %s
        AND o.activity_state = 'rest'
        AND o.observed_at >= %s
        AND o.observed_at < %s
    GROUP BY day
    ORDER BY day
    LIMIT %s
"""

ACTIVITY_STATE_COMPARISON_SQL = """
    SELECT
        o.activity_state,
        round(avg(o.value), 1)::double precision AS average_bpm,
        count(*)::integer AS observation_count
    FROM heart_rate_observations AS o
    WHERE
        o.user_id = %s
        AND o.metric = %s
        AND o.observed_at >= %s
        AND o.observed_at < %s
    GROUP BY o.activity_state
    ORDER BY CASE o.activity_state
        WHEN 'sleep' THEN 1
        WHEN 'rest' THEN 2
        WHEN 'ordinary_activity' THEN 3
        WHEN 'workout' THEN 4
    END
    LIMIT %s
"""

POPULATION_DAILY_AVERAGE_SQL = """
    SELECT
        (o.observed_at AT TIME ZONE 'UTC')::date AS day,
        round(avg(o.value), 1)::double precision AS average_bpm,
        count(*)::integer AS observation_count,
        count(DISTINCT o.user_id)::integer AS user_count
    FROM heart_rate_observations AS o
    WHERE
        o.metric = %s
        AND o.observed_at >= %s
        AND o.observed_at < %s
    GROUP BY day
    ORDER BY day
    LIMIT %s
"""


def build_query(query: StructuredQuery) -> tuple[str, tuple[Any, ...]]:
    """Return allowlisted SQL and bound values for an already validated query."""

    operation = QueryOperation(query.operation)
    fetch_limit = query.row_limit + 1
    if operation is QueryOperation.DAILY_USER_AVERAGE:
        return DAILY_USER_AVERAGE_SQL, (
            query.user_id,
            query.metric,
            query.start,
            query.end,
            fetch_limit,
        )
    if operation is QueryOperation.ACTIVITY_STATE_COMPARISON:
        return ACTIVITY_STATE_COMPARISON_SQL, (
            query.user_id,
            query.metric,
            query.start,
            query.end,
            fetch_limit,
        )
    return POPULATION_DAILY_AVERAGE_SQL, (
        query.metric,
        query.start,
        query.end,
        fetch_limit,
    )


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, Decimal):
            result[key] = float(value)
        else:
            result[key] = value
    return result


def execute_query(connection: Connection, query: StructuredQuery) -> QueryResult:
    validated = validate_query(query)
    operation = QueryOperation(validated.operation)

    if validated.user_id is not None:
        user_exists = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM users WHERE user_id = %s) AS exists",
            (validated.user_id,),
        ).fetchone()["exists"]
        if not user_exists:
            connection.commit()
            raise UnknownUserError(f"unknown user: {validated.user_id}")

    sql, parameters = build_query(validated)
    rows = connection.execute(sql, parameters).fetchall()
    connection.commit()
    if len(rows) > validated.row_limit:
        raise ResponseTooLargeError(
            f"query produced more than {validated.row_limit} rows"
        )

    return QueryResult(
        operation=operation,
        metric=validated.metric,
        start=validated.start,
        end=validated.end,
        rows=tuple(_normalise_row(row) for row in rows),
    )
