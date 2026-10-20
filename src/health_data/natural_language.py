"""Constrained natural-language interpretation through a local Ollama model."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from health_data.query import QueryOperation, StructuredQuery, validate_query

TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
USER_PATTERN = re.compile(r"\buser-[0-9]{4}\b")
UNSUPPORTED_METRIC_TERMS = (
    "steps",
    "step count",
    "blood oxygen",
    "spo2",
    "weight",
    "sleep stage",
)
PROMPT_INJECTION_TERMS = (
    "ignore previous",
    "system prompt",
    "return sql",
    "write sql",
    "drop table",
    "select *",
)
RELATIVE_LAST_WEEK_PATTERN = re.compile(r"\b(?:last|previous) week\b", re.IGNORECASE)
UNSUPPORTED_RELATIVE_DATE_PATTERN = re.compile(
    r"\b(?:today|yesterday|tomorrow|this week|current week|next week|"
    r"(?:this|current|last|previous|next) "
    r"(?:day|month|year|weekend|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)|"
    r"(?:past|last|next) \d+ (?:hours?|days?|weeks?|months?|years?)|"
    r"\d+ (?:hours?|days?|weeks?|months?|years?) ago)\b",
    re.IGNORECASE,
)
UNSUPPORTED_AGGREGATION_PATTERN = re.compile(
    r"\b(?:max(?:imum)?|min(?:imum)?|median|percentile|"
    r"p\d{2}|sum|highest|lowest|standard deviation|how many)\b",
    re.IGNORECASE,
)
MEDICAL_ADVICE_PATTERN = re.compile(
    r"\b(?:diagnos(?:e|is)|medical advice|treatment|medication|"
    r"should i|is (?:this|my|user-[0-9]{4}'?s) (?:heart rate )?"
    r"(?:healthy|normal|dangerous))\b",
    re.IGNORECASE,
)
AVERAGE_INTENT_PATTERN = re.compile(r"\b(?:average|mean)\b", re.IGNORECASE)
DAILY_GROUPING_PATTERN = re.compile(
    r"\b(?:each day|per day|daily|by day)\b",
    re.IGNORECASE,
)
ACTIVITY_COMPARISON_PATTERN = re.compile(
    r"\b(?:compare|comparison|across|by)\b",
    re.IGNORECASE,
)
DEFAULT_RELATIVE_DATE_TIMEZONE = "UTC"


class NaturalLanguageError(ValueError):
    code = "invalid_interpretation"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class AmbiguousQuestionError(NaturalLanguageError):
    code = "ambiguous_question"


class UnsupportedMetricError(NaturalLanguageError):
    code = "unsupported_metric"


class UnsupportedOperationError(NaturalLanguageError):
    code = "unsupported_operation"


class UnsupportedQuestionError(NaturalLanguageError):
    code = "unsupported_question"


class InvalidModelOutputError(NaturalLanguageError):
    code = "invalid_model_output"


class ModelUnavailableError(NaturalLanguageError):
    code = "model_unavailable"


@dataclass(frozen=True, slots=True)
class TemporalInterpretation:
    kind: Literal["explicit_range", "previous_iso_week"]
    start: datetime
    end: datetime
    timezone_name: str | None
    reference_time: datetime | None


@dataclass(frozen=True, slots=True)
class NaturalLanguageInterpretation:
    query: StructuredQuery
    temporal: TemporalInterpretation


class ModelPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: QueryOperation
    user_id: str | None
    metric: Literal["heart_rate"]
    start: datetime
    end: datetime


class QueryPlanner(Protocol):
    def plan(self, question: str) -> dict[str, Any]: ...

    def available(self) -> bool: ...


MODEL_PLAN_SCHEMA = ModelPlan.model_json_schema()

SYSTEM_PROMPT = """You convert one supported heart-rate question into a JSON plan.
Return JSON only and conform exactly to the supplied schema. Never return SQL.
Copy the user ID and both offset-aware timestamps exactly from the question.
User IDs always include the literal "user-" prefix. Never shorten them.
For a population query, user_id must be the JSON value null. Never emit a
placeholder such as "<USER_ID>".
When the application appends a "Resolved time range" line, copy those
timestamps exactly. The application, not the model, resolves relative dates.

Supported operation mapping:
- daily_user_average: mean of one user's recorded RESTING heart-rate observations,
  grouped by local day.
- activity_state_comparison: mean of one user's recorded heart-rate observations,
  compared across activity states.
- population_daily_average: mean of recorded heart-rate observations across all
  users, grouped by UTC day. This is not a per-user or time-weighted mean.

The only metric is heart_rate. Do not infer missing users or timestamps.

Examples:
Question: Compare user-0042's average heart rate across activity states from
2026-02-01T00:00:00Z to 2026-02-02T00:00:00Z.
Output: {"operation":"activity_state_comparison","user_id":"user-0042",
"metric":"heart_rate","start":"2026-02-01T00:00:00Z",
"end":"2026-02-02T00:00:00Z"}

Question: What was the population average heart rate across all users from
2026-02-01T00:00:00Z to 2026-02-02T00:00:00Z?
Output: {"operation":"population_daily_average","user_id":null,
"metric":"heart_rate","start":"2026-02-01T00:00:00Z",
"end":"2026-02-02T00:00:00Z"}
"""


class OllamaPlanner:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        try:
            response = httpx.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout_seconds, 3),
            )
            response.raise_for_status()
            return any(
                model.get("name", "").startswith(self.model)
                for model in response.json().get("models", [])
            )
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    def plan(self, question: str) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "system": SYSTEM_PROMPT,
                    "prompt": question,
                    "stream": False,
                    "format": MODEL_PLAN_SCHEMA,
                    "options": {"temperature": 0, "seed": 20261019},
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            model_text = response.json()["response"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise ModelUnavailableError(
                "the local interpretation model is unavailable"
            ) from error

        try:
            parsed = json.loads(model_text)
        except (json.JSONDecodeError, TypeError) as error:
            raise InvalidModelOutputError(
                "the local model returned invalid JSON"
            ) from error
        if not isinstance(parsed, dict):
            raise InvalidModelOutputError(
                "the local model response must be a JSON object"
            )
        return parsed


def _validate_question_scope(question: str) -> None:
    normalised = question.lower()
    if "heart rate" not in normalised and "heart-rate" not in normalised:
        raise UnsupportedMetricError(
            "question must explicitly ask about heart rate"
        )
    if any(term in normalised for term in UNSUPPORTED_METRIC_TERMS):
        raise UnsupportedMetricError("question mentions an unsupported metric")
    if any(term in normalised for term in PROMPT_INJECTION_TERMS):
        raise AmbiguousQuestionError("question contains unsupported instructions")
    if MEDICAL_ADVICE_PATTERN.search(question):
        raise UnsupportedQuestionError(
            "medical advice and clinical interpretation are not supported"
        )
    if UNSUPPORTED_AGGREGATION_PATTERN.search(question):
        raise UnsupportedOperationError(
            "question requests an unsupported analytical operation"
        )
    if re.search(r"\b(?:my|mine)\b", normalised) and not USER_PATTERN.search(question):
        raise AmbiguousQuestionError(
            "first-person questions require an explicit user ID because authentication "
            "is not implemented"
        )
    population_scope = any(
        phrase in normalised for phrase in ("all users", "across users", "population")
    )
    if USER_PATTERN.search(question) is None and not population_scope:
        raise AmbiguousQuestionError(
            "question must include an explicit user ID or population scope"
        )


def _render_timestamp(value: datetime) -> str:
    rendered = value.isoformat(timespec="seconds")
    return rendered.replace("+00:00", "Z")


def resolve_temporal_expression(
    question: str,
    *,
    reference_time: datetime | None = None,
    timezone_name: str = DEFAULT_RELATIVE_DATE_TIMEZONE,
) -> TemporalInterpretation:
    reference_time = reference_time or datetime.now(UTC)
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("query reference time must be offset-aware")

    matches = TIMESTAMP_PATTERN.findall(question)
    asks_for_last_week = RELATIVE_LAST_WEEK_PATTERN.search(question) is not None
    unsupported_relative_date = UNSUPPORTED_RELATIVE_DATE_PATTERN.search(question)
    if unsupported_relative_date is not None:
        raise AmbiguousQuestionError(
            "question contains an unsupported relative date expression: "
            f"{unsupported_relative_date.group(0)}"
        )
    if asks_for_last_week and matches:
        raise AmbiguousQuestionError(
            "question cannot combine last week with an explicit timestamp range"
        )
    if len(matches) == 2:
        timestamps = tuple(
            datetime.fromisoformat(value.replace("Z", "+00:00")) for value in matches
        )
        return TemporalInterpretation(
            kind="explicit_range",
            start=timestamps[0],
            end=timestamps[1],
            timezone_name=None,
            reference_time=None,
        )
    if matches:
        raise AmbiguousQuestionError(
            "question must contain exactly two offset-aware RFC 3339 timestamps"
        )
    if not asks_for_last_week:
        raise AmbiguousQuestionError(
            "question must contain an explicit timestamp range or the phrase last week"
        )

    try:
        timezone = ZoneInfo(timezone_name)
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid relative-date timezone: {timezone_name}") from error
    local_reference = reference_time.astimezone(timezone)
    current_week_start_date = local_reference.date() - timedelta(
        days=local_reference.weekday()
    )
    current_week_start = datetime.combine(
        current_week_start_date,
        time.min,
        tzinfo=timezone,
    )
    previous_week_start = current_week_start - timedelta(days=7)
    return TemporalInterpretation(
        kind="previous_iso_week",
        start=previous_week_start,
        end=current_week_start,
        timezone_name=timezone_name,
        reference_time=reference_time,
    )


def _planner_question(
    question: str,
    temporal: TemporalInterpretation,
) -> str:
    if temporal.kind == "explicit_range":
        return question
    return (
        f"{question}\nResolved time range: from "
        f"{_render_timestamp(temporal.start)} to {_render_timestamp(temporal.end)}."
    )


def validate_question(
    question: str,
    *,
    reference_time: datetime | None = None,
    timezone_name: str = DEFAULT_RELATIVE_DATE_TIMEZONE,
) -> TemporalInterpretation:
    _validate_question_scope(question)
    return resolve_temporal_expression(
        question,
        reference_time=reference_time,
        timezone_name=timezone_name,
    )


def interpret_model_plan(
    question: str,
    raw_plan: dict[str, Any],
    temporal: TemporalInterpretation | None = None,
) -> StructuredQuery:
    temporal = temporal or validate_question(question)
    try:
        plan = ModelPlan.model_validate(raw_plan)
    except ValidationError as error:
        raise InvalidModelOutputError(
            "the local model returned a plan outside the allowed schema"
        ) from error

    if plan.start != temporal.start or plan.end != temporal.end:
        raise InvalidModelOutputError(
            "the model plan timestamps do not match the application-resolved range"
        )

    mentioned_users = USER_PATTERN.findall(question)
    if plan.operation in {
        QueryOperation.DAILY_USER_AVERAGE,
        QueryOperation.ACTIVITY_STATE_COMPARISON,
    }:
        if len(mentioned_users) != 1 or plan.user_id != mentioned_users[0]:
            raise InvalidModelOutputError(
                "the model plan user was not copied from the question"
            )
    elif mentioned_users or plan.user_id is not None:
        raise InvalidModelOutputError(
            "population queries cannot contain a user"
        )

    normalised = question.lower()
    if (
        plan.operation is QueryOperation.DAILY_USER_AVERAGE
        and (
            "rest" not in normalised
            or AVERAGE_INTENT_PATTERN.search(question) is None
            or DAILY_GROUPING_PATTERN.search(question) is None
        )
    ):
        raise UnsupportedOperationError(
            "daily_user_average requires an average resting-heart-rate question "
            "with daily grouping"
        )
    if (
        plan.operation is QueryOperation.ACTIVITY_STATE_COMPARISON
        and (
            "activity" not in normalised
            or ACTIVITY_COMPARISON_PATTERN.search(question) is None
            or AVERAGE_INTENT_PATTERN.search(question) is None
        )
    ):
        raise UnsupportedOperationError(
            "activity_state_comparison requires an explicit average comparison "
            "across activity states"
        )
    if plan.operation is QueryOperation.POPULATION_DAILY_AVERAGE and (
        not any(
            phrase in normalised
            for phrase in ("all users", "across users", "population")
        )
        or AVERAGE_INTENT_PATTERN.search(question) is None
    ):
        raise UnsupportedOperationError(
            "population_daily_average requires an explicit population average"
        )

    return validate_query(
        StructuredQuery(
            operation=plan.operation,
            user_id=plan.user_id,
            metric=plan.metric,
            start=plan.start,
            end=plan.end,
        )
    )


def interpret_question(
    question: str,
    planner: QueryPlanner,
    *,
    reference_time: datetime | None = None,
    timezone_name: str = DEFAULT_RELATIVE_DATE_TIMEZONE,
) -> NaturalLanguageInterpretation:
    temporal = validate_question(
        question,
        reference_time=reference_time,
        timezone_name=timezone_name,
    )
    query = interpret_model_plan(
        question,
        planner.plan(_planner_question(question, temporal)),
        temporal,
    )
    return NaturalLanguageInterpretation(query=query, temporal=temporal)
