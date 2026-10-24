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
MAX_HOLD_SECONDS = 360
USER_ID_PATTERN = re.compile(r"^user-[0-9]{4}$")


class QueryOperation(StrEnum):
    USER_PERIOD_AVERAGE = "user_period_average"
    USER_PERIOD_RESTING_AVERAGE = "user_period_resting_average"
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
    max_hold_seconds: int
    quality: dict[str, Any]
    rows: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "metric": self.metric,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "aggregation_basis": "time_weighted_observation_hold",
            "max_hold_seconds": self.max_hold_seconds,
            "quality": self.quality,
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
        QueryOperation.USER_PERIOD_AVERAGE,
        QueryOperation.USER_PERIOD_RESTING_AVERAGE,
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


INDIVIDUAL_INTERVALS_CTE = """
    WITH request AS (
        SELECT
            %s::timestamptz AS start_at,
            %s::timestamptz AS end_at,
            %s::double precision AS max_hold_seconds,
            %s::text AS user_id,
            %s::text AS metric
    ),
    source_readings AS (
        SELECT
            o.event_id,
            o.user_id,
            o.device_id,
            o.metric,
            o.observed_at,
            o.value,
            o.activity_state
        FROM heart_rate_observations AS o
        CROSS JOIN request AS r
        WHERE
            o.user_id = r.user_id
            AND o.metric = r.metric
            AND o.observed_at >= r.start_at
            AND o.observed_at < r.end_at
        UNION ALL
        SELECT
            carry_in.event_id,
            carry_in.user_id,
            carry_in.device_id,
            carry_in.metric,
            carry_in.observed_at,
            carry_in.value,
            carry_in.activity_state
        FROM (
            SELECT o.*
            FROM heart_rate_observations AS o
            CROSS JOIN request AS r
            WHERE
                o.user_id = r.user_id
                AND o.metric = r.metric
                AND o.observed_at < r.start_at
            ORDER BY o.observed_at DESC
            LIMIT 1
        ) AS carry_in
    ),
    ordered_readings AS (
        SELECT
            source_readings.*,
            lead(observed_at) OVER (
                PARTITION BY user_id, device_id, metric
                ORDER BY observed_at
            ) AS next_observed_at
        FROM source_readings
    ),
    clipped_intervals AS (
        SELECT
            ordered_readings.*,
            greatest(ordered_readings.observed_at, r.start_at) AS interval_start,
            least(
                coalesce(ordered_readings.next_observed_at, r.end_at),
                ordered_readings.observed_at
                    + r.max_hold_seconds * interval '1 second',
                r.end_at
            ) AS interval_end
        FROM ordered_readings
        CROSS JOIN request AS r
    ),
    intervals AS (
        SELECT *
        FROM clipped_intervals
        WHERE interval_end > interval_start
    )
"""

POPULATION_INTERVALS_CTE = """
    WITH request AS (
        SELECT
            %s::timestamptz AS start_at,
            %s::timestamptz AS end_at,
            %s::double precision AS max_hold_seconds,
            %s::text AS metric
    ),
    source_readings AS (
        SELECT
            o.event_id,
            o.user_id,
            o.device_id,
            o.metric,
            o.observed_at,
            o.value,
            o.activity_state
        FROM heart_rate_observations AS o
        CROSS JOIN request AS r
        WHERE
            o.metric = r.metric
            AND o.observed_at >= r.start_at
            AND o.observed_at < r.end_at
        UNION ALL
        SELECT
            carry_in.event_id,
            carry_in.user_id,
            carry_in.device_id,
            carry_in.metric,
            carry_in.observed_at,
            carry_in.value,
            carry_in.activity_state
        FROM (
            SELECT DISTINCT ON (o.user_id, o.device_id, o.metric)
                o.*
            FROM heart_rate_observations AS o
            CROSS JOIN request AS r
            WHERE
                o.metric = r.metric
                AND o.observed_at < r.start_at
            ORDER BY
                o.user_id,
                o.device_id,
                o.metric,
                o.observed_at DESC
        ) AS carry_in
    ),
    ordered_readings AS (
        SELECT
            source_readings.*,
            lead(observed_at) OVER (
                PARTITION BY user_id, device_id, metric
                ORDER BY observed_at
            ) AS next_observed_at
        FROM source_readings
    ),
    clipped_intervals AS (
        SELECT
            ordered_readings.*,
            greatest(ordered_readings.observed_at, r.start_at) AS interval_start,
            least(
                coalesce(ordered_readings.next_observed_at, r.end_at),
                ordered_readings.observed_at
                    + r.max_hold_seconds * interval '1 second',
                r.end_at
            ) AS interval_end
        FROM ordered_readings
        CROSS JOIN request AS r
    ),
    intervals AS (
        SELECT *
        FROM clipped_intervals
        WHERE interval_end > interval_start
    )
"""

USER_PERIOD_AVERAGE_SQL = INDIVIDUAL_INTERVALS_CTE + """
    SELECT
        round(
            (
                sum(
                    i.value * extract(epoch FROM i.interval_end - i.interval_start)
                )
                / nullif(
                    sum(extract(epoch FROM i.interval_end - i.interval_start)),
                    0
                )
            )::numeric,
            1
        )::double precision AS time_weighted_mean_bpm,
        round(
            sum(extract(epoch FROM i.interval_end - i.interval_start))::numeric,
            6
        )::double precision AS contributing_duration_seconds
    FROM intervals AS i
    WHERE (%s::boolean = false OR i.activity_state = 'rest')
    HAVING sum(extract(epoch FROM i.interval_end - i.interval_start)) > 0
    LIMIT %s
"""

DAILY_USER_AVERAGE_SQL = INDIVIDUAL_INTERVALS_CTE + """
    , split_intervals AS (
        SELECT
            (
                (i.interval_start AT TIME ZONE u.timezone_name)::date
                + day_offsets.day_offset
            ) AS day,
            i.value,
            greatest(
                i.interval_start,
                (
                    (
                        (i.interval_start AT TIME ZONE u.timezone_name)::date
                        + day_offsets.day_offset
                    )::timestamp AT TIME ZONE u.timezone_name
                )
            ) AS segment_start,
            least(
                i.interval_end,
                (
                    (
                        (i.interval_start AT TIME ZONE u.timezone_name)::date
                        + day_offsets.day_offset
                        + 1
                    )::timestamp AT TIME ZONE u.timezone_name
                )
            ) AS segment_end
        FROM intervals AS i
        JOIN users AS u ON u.user_id = i.user_id
        CROSS JOIN LATERAL generate_series(
            0,
            (
                (
                    (i.interval_end - interval '1 microsecond')
                    AT TIME ZONE u.timezone_name
                )::date
                - (i.interval_start AT TIME ZONE u.timezone_name)::date
            )
        ) AS day_offsets(day_offset)
        WHERE i.activity_state = 'rest'
    )
    SELECT
        day,
        round(
            (
                sum(value * extract(epoch FROM segment_end - segment_start))
                / nullif(sum(extract(epoch FROM segment_end - segment_start)), 0)
            )::numeric,
            1
        )::double precision AS time_weighted_mean_bpm,
        round(
            sum(extract(epoch FROM segment_end - segment_start))::numeric,
            6
        )::double precision AS contributing_duration_seconds
    FROM split_intervals
    GROUP BY day
    ORDER BY day
    LIMIT %s
"""

ACTIVITY_STATE_COMPARISON_SQL = INDIVIDUAL_INTERVALS_CTE + """
    SELECT
        i.activity_state,
        round(
            (
                sum(
                    i.value * extract(epoch FROM i.interval_end - i.interval_start)
                )
                / nullif(
                    sum(extract(epoch FROM i.interval_end - i.interval_start)),
                    0
                )
            )::numeric,
            1
        )::double precision AS time_weighted_mean_bpm,
        round(
            sum(extract(epoch FROM i.interval_end - i.interval_start))::numeric,
            6
        )::double precision AS contributing_duration_seconds
    FROM intervals AS i
    GROUP BY i.activity_state
    ORDER BY CASE i.activity_state
        WHEN 'sleep' THEN 1
        WHEN 'rest' THEN 2
        WHEN 'ordinary_activity' THEN 3
        WHEN 'workout' THEN 4
    END
    LIMIT %s
"""

POPULATION_DAILY_AVERAGE_SQL = POPULATION_INTERVALS_CTE + """
    , split_intervals AS (
        SELECT
            (
                (i.interval_start AT TIME ZONE 'UTC')::date
                + day_offsets.day_offset
            ) AS day,
            i.user_id,
            i.value,
            greatest(
                i.interval_start,
                (
                    (
                        (i.interval_start AT TIME ZONE 'UTC')::date
                        + day_offsets.day_offset
                    )::timestamp AT TIME ZONE 'UTC'
                )
            ) AS segment_start,
            least(
                i.interval_end,
                (
                    (
                        (i.interval_start AT TIME ZONE 'UTC')::date
                        + day_offsets.day_offset
                        + 1
                    )::timestamp AT TIME ZONE 'UTC'
                )
            ) AS segment_end
        FROM intervals AS i
        CROSS JOIN LATERAL generate_series(
            0,
            (
                (
                    (i.interval_end - interval '1 microsecond')
                    AT TIME ZONE 'UTC'
                )::date
                - (i.interval_start AT TIME ZONE 'UTC')::date
            )
        ) AS day_offsets(day_offset)
    )
    SELECT
        day,
        round(
            (
                sum(value * extract(epoch FROM segment_end - segment_start))
                / nullif(sum(extract(epoch FROM segment_end - segment_start)), 0)
            )::numeric,
            1
        )::double precision AS time_weighted_mean_bpm,
        round(
            sum(extract(epoch FROM segment_end - segment_start))::numeric,
            6
        )::double precision AS contributing_user_seconds,
        count(DISTINCT user_id)::integer AS contributing_user_count
    FROM split_intervals
    GROUP BY day
    ORDER BY day
    LIMIT %s
"""

INDIVIDUAL_QUALITY_SQL = INDIVIDUAL_INTERVALS_CTE + """
    , ordered_intervals AS (
        SELECT
            i.*,
            lag(i.interval_end) OVER (ORDER BY i.interval_start) AS previous_end
        FROM intervals AS i
    ),
    gaps AS (
        SELECT
            extract(
                epoch FROM
                oi.interval_start
                - greatest(coalesce(oi.previous_end, r.start_at), r.start_at)
            ) AS gap_seconds
        FROM ordered_intervals AS oi
        CROSS JOIN request AS r
        UNION ALL
        SELECT
            extract(
                epoch FROM
                r.end_at - coalesce(max(i.interval_end), r.start_at)
            ) AS gap_seconds
        FROM request AS r
        LEFT JOIN intervals AS i ON true
        GROUP BY r.start_at, r.end_at
    ),
    duration_summary AS (
        SELECT
            extract(epoch FROM r.end_at - r.start_at) AS requested_seconds,
            coalesce(
                sum(extract(epoch FROM i.interval_end - i.interval_start)),
                0
            ) AS observed_seconds
        FROM request AS r
        LEFT JOIN intervals AS i ON true
        GROUP BY r.start_at, r.end_at
    )
    SELECT
        'user'::text AS scope,
        round(ds.requested_seconds::numeric, 6)::double precision
            AS requested_duration_seconds,
        round(ds.observed_seconds::numeric, 6)::double precision
            AS observed_duration_seconds,
        round(
            (ds.requested_seconds - ds.observed_seconds)::numeric,
            6
        )::double precision AS unobserved_duration_seconds,
        count(*) FILTER (WHERE gaps.gap_seconds > 0)::integer AS gap_count,
        round(
            coalesce(max(gaps.gap_seconds), 0)::numeric,
            6
        )::double precision AS largest_gap_seconds
    FROM duration_summary AS ds
    CROSS JOIN gaps
    GROUP BY ds.requested_seconds, ds.observed_seconds
"""

POPULATION_QUALITY_SQL = POPULATION_INTERVALS_CTE + """
    , expected_users AS (
        SELECT user_id
        FROM users
    ),
    ordered_intervals AS (
        SELECT
            i.*,
            lag(i.interval_end) OVER (
                PARTITION BY i.user_id
                ORDER BY i.interval_start
            ) AS previous_end
        FROM intervals AS i
    ),
    gaps AS (
        SELECT
            oi.user_id,
            extract(
                epoch FROM
                    oi.interval_start
                    - greatest(coalesce(oi.previous_end, r.start_at), r.start_at)
            ) AS gap_seconds
        FROM ordered_intervals AS oi
        CROSS JOIN request AS r
        UNION ALL
        SELECT
            eu.user_id,
            extract(
                epoch FROM
                    r.end_at - coalesce(max(i.interval_end), r.start_at)
            ) AS gap_seconds
        FROM expected_users AS eu
        CROSS JOIN request AS r
        LEFT JOIN intervals AS i ON i.user_id = eu.user_id
        GROUP BY eu.user_id, r.start_at, r.end_at
    ),
    duration_by_user AS (
        SELECT
            eu.user_id,
            coalesce(
                sum(extract(epoch FROM i.interval_end - i.interval_start)),
                0
            ) AS observed_seconds
        FROM expected_users AS eu
        LEFT JOIN intervals AS i ON i.user_id = eu.user_id
        GROUP BY eu.user_id
    ),
    summary AS (
        SELECT
            extract(epoch FROM r.end_at - r.start_at) AS requested_seconds,
            count(duration_by_user.user_id)::integer AS expected_user_count,
            count(duration_by_user.user_id) FILTER (
                WHERE duration_by_user.observed_seconds > 0
            )::integer AS observed_user_count,
            coalesce(sum(duration_by_user.observed_seconds), 0)
                AS observed_user_seconds
        FROM request AS r
        LEFT JOIN duration_by_user ON true
        GROUP BY r.start_at, r.end_at
    ),
    gap_summary AS (
        SELECT
            count(*) FILTER (WHERE gap_seconds > 0)::integer AS gap_count,
            coalesce(max(gap_seconds), 0) AS largest_gap_seconds
        FROM gaps
    )
    SELECT
        'population'::text AS scope,
        round(summary.requested_seconds::numeric, 6)::double precision
            AS requested_duration_seconds,
        round(
            (
                summary.requested_seconds * summary.expected_user_count
            )::numeric,
            6
        )::double precision AS requested_user_seconds,
        round(summary.observed_user_seconds::numeric, 6)::double precision
            AS observed_user_seconds,
        round(
            (
                summary.requested_seconds * summary.expected_user_count
                - summary.observed_user_seconds
            )::numeric,
            6
        )::double precision AS unobserved_user_seconds,
        summary.expected_user_count,
        summary.observed_user_count,
        gap_summary.gap_count,
        round(
            gap_summary.largest_gap_seconds::numeric,
            6
        )::double precision AS largest_individual_gap_seconds
    FROM summary
    CROSS JOIN gap_summary
"""


def _individual_parameters(
    query: StructuredQuery,
    max_hold_seconds: int,
) -> tuple[Any, ...]:
    return (
        query.start,
        query.end,
        max_hold_seconds,
        query.user_id,
        query.metric,
    )


def _population_parameters(
    query: StructuredQuery,
    max_hold_seconds: int,
) -> tuple[Any, ...]:
    return (query.start, query.end, max_hold_seconds, query.metric)


def build_query(
    query: StructuredQuery,
    max_hold_seconds: int = MAX_HOLD_SECONDS,
) -> tuple[str, tuple[Any, ...]]:
    """Return allowlisted SQL and bound values for an already validated query."""

    operation = QueryOperation(query.operation)
    fetch_limit = query.row_limit + 1
    if operation in {
        QueryOperation.USER_PERIOD_AVERAGE,
        QueryOperation.USER_PERIOD_RESTING_AVERAGE,
    }:
        return USER_PERIOD_AVERAGE_SQL, _individual_parameters(
            query, max_hold_seconds
        ) + (
            operation is QueryOperation.USER_PERIOD_RESTING_AVERAGE, fetch_limit,
        )
    if operation is QueryOperation.DAILY_USER_AVERAGE:
        return DAILY_USER_AVERAGE_SQL, _individual_parameters(
            query, max_hold_seconds
        ) + (
            fetch_limit,
        )
    if operation is QueryOperation.ACTIVITY_STATE_COMPARISON:
        return ACTIVITY_STATE_COMPARISON_SQL, _individual_parameters(
            query, max_hold_seconds
        ) + (
            fetch_limit,
        )
    return POPULATION_DAILY_AVERAGE_SQL, _population_parameters(
        query, max_hold_seconds
    ) + (
        fetch_limit,
    )


def build_quality_query(
    query: StructuredQuery,
    max_hold_seconds: int = MAX_HOLD_SECONDS,
) -> tuple[str, tuple[Any, ...]]:
    operation = QueryOperation(query.operation)
    if operation is QueryOperation.POPULATION_DAILY_AVERAGE:
        return (
            POPULATION_QUALITY_SQL,
            _population_parameters(query, max_hold_seconds),
        )
    return INDIVIDUAL_QUALITY_SQL, _individual_parameters(
        query, max_hold_seconds
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


def execute_query(
    connection: Connection,
    query: StructuredQuery,
    *,
    max_hold_seconds: int = MAX_HOLD_SECONDS,
) -> QueryResult:
    validated = validate_query(query)
    operation = QueryOperation(validated.operation)
    if max_hold_seconds <= 0:
        raise AnalyticalQueryError("max_hold_seconds must be positive")

    if validated.user_id is not None:
        user_exists = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM users WHERE user_id = %s) AS exists",
            (validated.user_id,),
        ).fetchone()["exists"]
        if not user_exists:
            connection.commit()
            raise UnknownUserError(f"unknown user: {validated.user_id}")

    sql, parameters = build_query(validated, max_hold_seconds)
    rows = connection.execute(sql, parameters).fetchall()
    quality_sql, quality_parameters = build_quality_query(
        validated,
        max_hold_seconds,
    )
    quality = connection.execute(quality_sql, quality_parameters).fetchone()
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
        max_hold_seconds=max_hold_seconds,
        quality=_normalise_row(quality),
        rows=tuple(_normalise_row(row) for row in rows),
    )
