"""Environment-backed runtime configuration with reproducible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from health_data.generator import GenerationConfig


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    generation: GenerationConfig
    source_path: Path
    batch_size: int
    batch_delay_seconds: float
    max_hold_seconds: int
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    relative_date_timezone: str


def load_settings() -> Settings:
    window_start = datetime.fromisoformat(
        os.getenv("HEALTH_DATA_WINDOW_START", "2026-10-05T00:00:00+00:00")
    )
    max_hold_seconds = int(os.getenv("HEALTH_DATA_MAX_HOLD_SECONDS", "360"))
    if max_hold_seconds <= 0:
        raise ValueError("HEALTH_DATA_MAX_HOLD_SECONDS must be positive")
    generation = GenerationConfig(
        seed=int(os.getenv("HEALTH_DATA_SEED", "20261019")),
        window_start=window_start,
        days=int(os.getenv("HEALTH_DATA_DAYS", "21")),
        user_count=int(os.getenv("HEALTH_DATA_USER_COUNT", "50")),
    )
    return Settings(
        database_url=os.getenv(
            "HEALTH_DATA_DATABASE_URL",
            "postgresql://health_data:health_data@postgres:5432/health_data",
        ),
        generation=generation,
        source_path=Path(
            os.getenv(
                "HEALTH_DATA_SOURCE_PATH",
                "/var/lib/health-data-source/heart-rate-events.jsonl",
            )
        ),
        batch_size=int(os.getenv("HEALTH_DATA_BATCH_SIZE", "1000")),
        batch_delay_seconds=float(
            os.getenv("HEALTH_DATA_BATCH_DELAY_SECONDS", "0")
        ),
        max_hold_seconds=max_hold_seconds,
        ollama_url=os.getenv("HEALTH_DATA_OLLAMA_URL", "http://ollama:11434"),
        ollama_model=os.getenv("HEALTH_DATA_OLLAMA_MODEL", "qwen2.5:1.5b"),
        ollama_timeout_seconds=float(
            os.getenv("HEALTH_DATA_OLLAMA_TIMEOUT_SECONDS", "30")
        ),
        relative_date_timezone=os.getenv(
            "HEALTH_DATA_RELATIVE_DATE_TIMEZONE",
            "UTC",
        ),
    )
