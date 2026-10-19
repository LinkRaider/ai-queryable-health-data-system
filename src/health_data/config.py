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


def load_settings() -> Settings:
    window_start = datetime.fromisoformat(
        os.getenv("HEALTH_DATA_WINDOW_START", "2026-01-05T00:00:00+00:00")
    )
    return Settings(
        database_url=os.getenv(
            "HEALTH_DATA_DATABASE_URL",
            "postgresql://health_data:health_data@postgres:5432/health_data",
        ),
        generation=GenerationConfig(
            seed=int(os.getenv("HEALTH_DATA_SEED", "20261019")),
            window_start=window_start,
            days=int(os.getenv("HEALTH_DATA_DAYS", "7")),
            user_count=int(os.getenv("HEALTH_DATA_USER_COUNT", "50")),
        ),
        batch_size=int(os.getenv("HEALTH_DATA_BATCH_SIZE", "1000")),
        batch_delay_seconds=float(
            os.getenv("HEALTH_DATA_BATCH_DELAY_SECONDS", "0")
        ),
    )
