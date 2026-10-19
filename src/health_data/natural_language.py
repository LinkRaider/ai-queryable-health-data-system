"""Constrained natural-language interpretation through a local Ollama model."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from health_data.query import QueryOperation, StructuredQuery, validate_query

TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})"
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


class NaturalLanguageError(ValueError):
    code = "invalid_interpretation"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class AmbiguousQuestionError(NaturalLanguageError):
    code = "ambiguous_question"


class UnsupportedMetricError(NaturalLanguageError):
    code = "unsupported_metric"


class InvalidModelOutputError(NaturalLanguageError):
    code = "invalid_model_output"


class ModelUnavailableError(NaturalLanguageError):
    code = "model_unavailable"


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

Supported operation mapping:
- daily_user_average: one user's RESTING heart-rate average grouped by local day.
- activity_state_comparison: one user's heart rate compared across activity states.
- population_daily_average: heart-rate average across all users grouped by UTC day.

The only metric is heart_rate. Do not infer missing users or timestamps.

Examples:
Question: Compare user-0042's heart rate across activity states from
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


def _parse_question_timestamps(question: str) -> tuple[datetime, datetime]:
    matches = TIMESTAMP_PATTERN.findall(question)
    if len(matches) != 2:
        raise AmbiguousQuestionError(
            "question must contain exactly two offset-aware RFC 3339 timestamps"
        )
    timestamps = tuple(
        datetime.fromisoformat(value.replace("Z", "+00:00")) for value in matches
    )
    return timestamps[0], timestamps[1]


def validate_question(question: str) -> tuple[datetime, datetime]:
    normalised = question.lower()
    if "heart rate" not in normalised and "heart-rate" not in normalised:
        raise UnsupportedMetricError(
            "question must explicitly ask about heart rate"
        )
    if any(term in normalised for term in UNSUPPORTED_METRIC_TERMS):
        raise UnsupportedMetricError("question mentions an unsupported metric")
    if any(term in normalised for term in PROMPT_INJECTION_TERMS):
        raise AmbiguousQuestionError("question contains unsupported instructions")
    return _parse_question_timestamps(question)


def interpret_model_plan(
    question: str,
    raw_plan: dict[str, Any],
) -> StructuredQuery:
    question_start, question_end = validate_question(question)
    try:
        plan = ModelPlan.model_validate(raw_plan)
    except ValidationError as error:
        raise InvalidModelOutputError(
            "the local model returned a plan outside the allowed schema"
        ) from error

    if plan.start != question_start or plan.end != question_end:
        raise InvalidModelOutputError(
            "the model plan timestamps were not copied from the question"
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
        and "rest" not in normalised
    ):
        raise AmbiguousQuestionError(
            "daily_user_average requires an explicit resting-heart-rate question"
        )
    if (
        plan.operation is QueryOperation.ACTIVITY_STATE_COMPARISON
        and "activity" not in normalised
    ):
        raise AmbiguousQuestionError(
            "activity_state_comparison requires explicit activity-state wording"
        )
    if plan.operation is QueryOperation.POPULATION_DAILY_AVERAGE and not any(
        phrase in normalised for phrase in ("all users", "across users", "population")
    ):
        raise AmbiguousQuestionError(
            "population_daily_average requires explicit population wording"
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


def interpret_question(question: str, planner: QueryPlanner) -> StructuredQuery:
    validate_question(question)
    return interpret_model_plan(question, planner.plan(question))
