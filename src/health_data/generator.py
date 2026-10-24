"""Deterministic synthetic heart-rate event generation.

The distributions in this module are engineering demo patterns. They are not
clinical models and must not be interpreted as medical guidance.
"""

from __future__ import annotations

import hashlib
import math
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Iterator
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "health.heart_rate.observation.v1"
EVENT_NAMESPACE = uuid.UUID("83d27a8c-e30d-5ca0-9280-40f7ca4be219")
GENERATOR_VERSION = "1"


class ActivityState(StrEnum):
    SLEEP = "sleep"
    REST = "rest"
    ORDINARY_ACTIVITY = "ordinary_activity"
    WORKOUT = "workout"


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    seed: int = 20261019
    window_start: datetime = datetime(2026, 10, 5, tzinfo=UTC)
    days: int = 21
    user_count: int = 50

    def __post_init__(self) -> None:
        if self.window_start.tzinfo is None or self.window_start.utcoffset() is None:
            raise ValueError("window_start must be offset-aware")
        if self.days < 1:
            raise ValueError("days must be positive")
        if self.user_count < 1:
            raise ValueError("user_count must be positive")

    @property
    def window_end(self) -> datetime:
        return self.window_start + timedelta(days=self.days)

    @property
    def identity(self) -> str:
        return "|".join(
            (
                SCHEMA_VERSION,
                GENERATOR_VERSION,
                str(self.seed),
                canonical_timestamp(self.window_start),
                canonical_timestamp(self.window_end),
                str(self.user_count),
            )
        )


@dataclass(frozen=True, slots=True)
class UserProfile:
    user_id: str
    device_id: str
    timezone_name: str
    resting_baseline_bpm: float
    wake_minute: int
    bedtime_minute: int
    ordinary_activity_boost_bpm: float
    workout_boost_bpm: float
    workout_probability: float
    device_off_probability: float

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


@dataclass(frozen=True, slots=True)
class HeartRateEvent:
    schema_version: str
    event_id: uuid.UUID
    user_id: str
    device_id: str
    observed_at: datetime
    metric: str
    value: float
    unit: str
    activity_state: ActivityState

    def as_dict(self) -> dict[str, str | float]:
        return {
            "schema_version": self.schema_version,
            "event_id": str(self.event_id),
            "user_id": self.user_id,
            "device_id": self.device_id,
            "observed_at": canonical_timestamp(self.observed_at),
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "activity_state": self.activity_state.value,
        }


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: datetime
    end: datetime

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


@dataclass(frozen=True, slots=True)
class DayPlan:
    local_date: date
    wake_minute: int
    bedtime_minute: int
    workout: TimeWindow | None
    device_off: TimeWindow | None


TIMEZONE_NAMES = (
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "Europe/London",
)

CADENCE_SECONDS = {
    ActivityState.SLEEP: (300, 40),
    ActivityState.REST: (180, 30),
    ActivityState.ORDINARY_ACTIVITY: (75, 15),
    ActivityState.WORKOUT: (20, 5),
}


def canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be offset-aware")
    rendered = value.isoformat(timespec="seconds")
    return rendered.replace("+00:00", "Z")


def _stable_seed(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def generate_profiles(config: GenerationConfig) -> tuple[UserProfile, ...]:
    profiles: list[UserProfile] = []
    for index in range(1, config.user_count + 1):
        rng = random.Random(_stable_seed(config.seed, "profile", index))
        profiles.append(
            UserProfile(
                user_id=f"user-{index:04d}",
                device_id=f"device-{index:04d}",
                timezone_name=TIMEZONE_NAMES[(index - 1) % len(TIMEZONE_NAMES)],
                resting_baseline_bpm=round(rng.uniform(52.0, 78.0), 1),
                wake_minute=rng.randint(6 * 60, 8 * 60 + 30),
                bedtime_minute=rng.randint(21 * 60 + 30, 23 * 60 + 45),
                ordinary_activity_boost_bpm=round(rng.uniform(18.0, 38.0), 1),
                workout_boost_bpm=round(rng.uniform(62.0, 108.0), 1),
                workout_probability=rng.uniform(0.25, 0.72),
                device_off_probability=rng.uniform(0.18, 0.48),
            )
        )
    return tuple(profiles)


def _at_local_minute(day: date, minute: int, timezone: ZoneInfo) -> datetime:
    return datetime.combine(
        day,
        time(hour=minute // 60, minute=minute % 60),
        tzinfo=timezone,
    )


def build_day_plan(
    profile: UserProfile,
    day: date,
    config: GenerationConfig,
) -> DayPlan:
    rng = random.Random(_stable_seed(config.seed, "day-plan", profile.user_id, day.isoformat()))
    wake_minute = min(max(profile.wake_minute + rng.randint(-20, 20), 5 * 60 + 30), 9 * 60)
    bedtime_minute = min(
        max(profile.bedtime_minute + rng.randint(-20, 20), 21 * 60),
        23 * 60 + 55,
    )

    workout: TimeWindow | None = None
    if rng.random() < profile.workout_probability:
        workout_start_minute = rng.randint(wake_minute + 90, bedtime_minute - 90)
        workout_duration = rng.randint(25, 70)
        workout_start = _at_local_minute(day, workout_start_minute, profile.timezone)
        workout = TimeWindow(workout_start, workout_start + timedelta(minutes=workout_duration))

    device_off: TimeWindow | None = None
    if rng.random() < profile.device_off_probability:
        off_start_minute = rng.randint(wake_minute + 30, bedtime_minute - 45)
        off_duration = rng.randint(35, 125)
        off_start = _at_local_minute(day, off_start_minute, profile.timezone)
        device_off = TimeWindow(
            off_start,
            min(
                off_start + timedelta(minutes=off_duration),
                _at_local_minute(day, bedtime_minute, profile.timezone),
            ),
        )

    return DayPlan(
        local_date=day,
        wake_minute=wake_minute,
        bedtime_minute=bedtime_minute,
        workout=workout,
        device_off=device_off,
    )


def device_off_windows(
    profile: UserProfile,
    config: GenerationConfig,
) -> tuple[TimeWindow, ...]:
    first_local_day = config.window_start.astimezone(profile.timezone).date()
    last_local_day = (config.window_end - timedelta(microseconds=1)).astimezone(
        profile.timezone
    ).date()
    windows: list[TimeWindow] = []
    day = first_local_day
    while day <= last_local_day:
        device_off = build_day_plan(profile, day, config).device_off
        if device_off is not None:
            window_start_utc = config.window_start.astimezone(UTC)
            window_end_utc = config.window_end.astimezone(UTC)
            if device_off.end.astimezone(UTC) > window_start_utc and (
                device_off.start.astimezone(UTC) < window_end_utc
            ):
                windows.append(device_off)
        day += timedelta(days=1)
    return tuple(windows)


def _state_at(
    instant_utc: datetime,
    profile: UserProfile,
    config: GenerationConfig,
) -> tuple[ActivityState | None, DayPlan]:
    local = instant_utc.astimezone(profile.timezone)
    plan = build_day_plan(profile, local.date(), config)
    if plan.device_off is not None and plan.device_off.contains(local):
        return None, plan
    if plan.workout is not None and plan.workout.contains(local):
        return ActivityState.WORKOUT, plan

    minute = local.hour * 60 + local.minute
    if minute < plan.wake_minute or minute >= plan.bedtime_minute:
        return ActivityState.SLEEP, plan

    minutes_after_wake = minute - plan.wake_minute
    minutes_before_bed = plan.bedtime_minute - minute
    in_lunch_rest = 12 * 60 <= minute < 13 * 60
    in_evening_rest = 18 * 60 + 30 <= minute < 19 * 60 + 30
    if (
        minutes_after_wake < 45
        or minutes_before_bed <= 45
        or in_lunch_rest
        or in_evening_rest
    ):
        return ActivityState.REST, plan
    return ActivityState.ORDINARY_ACTIVITY, plan


def _heart_rate_value(
    state: ActivityState,
    profile: UserProfile,
    local_instant: datetime,
    plan: DayPlan,
    rng: random.Random,
) -> float:
    circadian = 1.8 * math.sin((local_instant.hour - 9) * math.pi / 12)
    if state is ActivityState.SLEEP:
        value = rng.normalvariate(profile.resting_baseline_bpm - 7.0, 2.2)
    elif state is ActivityState.REST:
        value = rng.normalvariate(profile.resting_baseline_bpm + circadian, 3.0)
    elif state is ActivityState.ORDINARY_ACTIVITY:
        value = rng.normalvariate(
            profile.resting_baseline_bpm + profile.ordinary_activity_boost_bpm + circadian,
            7.0,
        )
    else:
        progress = 0.5
        if plan.workout is not None:
            duration = (plan.workout.end - plan.workout.start).total_seconds()
            elapsed = (local_instant - plan.workout.start).total_seconds()
            progress = min(max(elapsed / duration, 0.0), 1.0)
        warmup_curve = 0.72 + 0.28 * math.sin(progress * math.pi)
        value = rng.normalvariate(
            profile.resting_baseline_bpm + profile.workout_boost_bpm * warmup_curve,
            7.5,
        )
    return round(min(max(value, 30.0), 220.0), 1)


def _event_id(
    config: GenerationConfig,
    profile: UserProfile,
    observed_at: datetime,
) -> uuid.UUID:
    canonical_name = "|".join(
        (
            SCHEMA_VERSION,
            str(config.seed),
            canonical_timestamp(config.window_start),
            canonical_timestamp(config.window_end),
            profile.user_id,
            profile.device_id,
            canonical_timestamp(observed_at),
        )
    )
    return uuid.uuid5(EVENT_NAMESPACE, canonical_name)


def generate_events(
    config: GenerationConfig = GenerationConfig(),
) -> Iterator[HeartRateEvent]:
    window_start_utc = config.window_start.astimezone(UTC)
    window_end_utc = config.window_end.astimezone(UTC)

    for profile in generate_profiles(config):
        rng = random.Random(
            _stable_seed(config.seed, "events", profile.user_id, config.identity)
        )
        cursor = window_start_utc
        while cursor < window_end_utc:
            state, plan = _state_at(cursor, profile, config)
            if state is None:
                assert plan.device_off is not None
                cursor = plan.device_off.end.astimezone(UTC) + timedelta(
                    seconds=rng.randint(1, 30)
                )
                continue

            observed_at = cursor.astimezone(profile.timezone)
            yield HeartRateEvent(
                schema_version=SCHEMA_VERSION,
                event_id=_event_id(config, profile, observed_at),
                user_id=profile.user_id,
                device_id=profile.device_id,
                observed_at=observed_at,
                metric="heart_rate",
                value=_heart_rate_value(state, profile, observed_at, plan, rng),
                unit="bpm",
                activity_state=state,
            )

            cadence, jitter = CADENCE_SECONDS[state]
            cursor += timedelta(seconds=cadence + rng.randint(-jitter, jitter))
