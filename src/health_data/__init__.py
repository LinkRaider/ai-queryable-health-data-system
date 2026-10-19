"""Synthetic heart-rate data system."""

from health_data.generator import (
    ActivityState,
    GenerationConfig,
    HeartRateEvent,
    UserProfile,
    generate_events,
    generate_profiles,
)

__all__ = [
    "ActivityState",
    "GenerationConfig",
    "HeartRateEvent",
    "UserProfile",
    "generate_events",
    "generate_profiles",
]
