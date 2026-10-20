"""Environment-backed runtime configuration with reproducible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from health_data.generator import GenerationConfig


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    generation: GenerationConfig
    batch_size: int
    batch_delay_seconds: float
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    query_reference_time: datetime
    relative_date_timezone: str


def load_settings() -> Settings:
    window_start = datetime.fromisoformat(
        os.getenv("HEALTH_DATA_WINDOW_START", "2026-01-05T00:00:00+00:00")
    )
    generation = GenerationConfig(
        seed=int(os.getenv("HEALTH_DATA_SEED", "20261019")),
        window_start=window_start,
        days=int(os.getenv("HEALTH_DATA_DAYS", "7")),
        user_count=int(os.getenv("HEALTH_DATA_USER_COUNT", "50")),
    )
    query_reference_time = datetime.fromisoformat(
        os.getenv(
            "HEALTH_DATA_QUERY_REFERENCE_TIME",
            generation.window_end.isoformat(),
        ).replace("Z", "+00:00")
    )
    if (
        query_reference_time.tzinfo is None
        or query_reference_time.utcoffset() is None
    ):
        raise ValueError("HEALTH_DATA_QUERY_REFERENCE_TIME must include a UTC offset")
    return Settings(
        database_url=os.getenv(
            "HEALTH_DATA_DATABASE_URL",
            "postgresql://health_data:health_data@postgres:5432/health_data",
        ),
        generation=generation,
        batch_size=int(os.getenv("HEALTH_DATA_BATCH_SIZE", "1000")),
        batch_delay_seconds=float(
            os.getenv("HEALTH_DATA_BATCH_DELAY_SECONDS", "0")
        ),
        ollama_url=os.getenv("HEALTH_DATA_OLLAMA_URL", "http://ollama:11434"),
        ollama_model=os.getenv("HEALTH_DATA_OLLAMA_MODEL", "qwen2.5:1.5b"),
        ollama_timeout_seconds=float(
            os.getenv("HEALTH_DATA_OLLAMA_TIMEOUT_SECONDS", "30")
        ),
        query_reference_time=query_reference_time,
        relative_date_timezone=os.getenv(
            "HEALTH_DATA_RELATIVE_DATE_TIMEZONE",
            "UTC",
        ),
    )
