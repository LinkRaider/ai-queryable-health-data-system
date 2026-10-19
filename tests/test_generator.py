from __future__ import annotations

import hashlib
import sys
import statistics
import unittest
from collections import Counter, defaultdict
from datetime import UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from health_data.generator import (
    CADENCE_SECONDS,
    ActivityState,
    GenerationConfig,
    canonical_timestamp,
    device_off_windows,
    generate_events,
    generate_profiles,
)


def event_digest(events: list) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(
            (
                f"{event.event_id}|{event.user_id}|"
                f"{canonical_timestamp(event.observed_at)}|{event.value}|"
                f"{event.activity_state.value}\n"
            ).encode()
        )
    return digest.hexdigest()


class SyntheticHeartRateGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = GenerationConfig()
        cls.profiles = generate_profiles(cls.config)
        cls.events = list(generate_events(cls.config))
        cls.events_by_user = defaultdict(list)
        for event in cls.events:
            cls.events_by_user[event.user_id].append(event)

    def test_default_population_and_volume_are_defensible(self) -> None:
        self.assertEqual(50, len(self.profiles))
        self.assertGreater(len(self.events), 150_000)
        self.assertLess(len(self.events), 450_000)
        self.assertEqual(50, len(self.events_by_user))

    def test_repeated_generation_has_identical_payloads_and_ids(self) -> None:
        replay = list(generate_events(self.config))
        self.assertEqual(len(self.events), len(replay))
        self.assertEqual(event_digest(self.events), event_digest(replay))
        self.assertEqual(
            [event.event_id for event in self.events],
            [event.event_id for event in replay],
        )

    def test_seed_changes_event_identity(self) -> None:
        changed = GenerationConfig(seed=self.config.seed + 1, user_count=1, days=1)
        original = GenerationConfig(seed=self.config.seed, user_count=1, days=1)
        self.assertNotEqual(
            next(generate_events(original)).event_id,
            next(generate_events(changed)).event_id,
        )

    def test_profiles_are_distinguishable(self) -> None:
        baselines = {profile.resting_baseline_bpm for profile in self.profiles}
        sleep_schedules = {
            (profile.wake_minute, profile.bedtime_minute) for profile in self.profiles
        }
        activity_boosts = {
            profile.ordinary_activity_boost_bpm for profile in self.profiles
        }
        self.assertGreaterEqual(len(baselines), 40)
        self.assertGreaterEqual(len(sleep_schedules), 40)
        self.assertGreaterEqual(len(activity_boosts), 40)
        self.assertEqual(5, len({profile.timezone_name for profile in self.profiles}))

    def test_events_satisfy_contract_and_ids_are_unique(self) -> None:
        event_ids = set()
        natural_keys = set()
        offsets = set()
        for event in self.events:
            self.assertEqual("health.heart_rate.observation.v1", event.schema_version)
            self.assertEqual("heart_rate", event.metric)
            self.assertEqual("bpm", event.unit)
            self.assertGreaterEqual(event.value, 30.0)
            self.assertLessEqual(event.value, 220.0)
            self.assertIsNotNone(event.observed_at.utcoffset())
            event_ids.add(event.event_id)
            natural_keys.add(
                (event.user_id, event.device_id, event.observed_at, event.metric)
            )
            offsets.add(event.observed_at.utcoffset())
        self.assertEqual(len(self.events), len(event_ids))
        self.assertEqual(len(self.events), len(natural_keys))
        self.assertGreaterEqual(len(offsets), 4)

    def test_day_night_and_activity_patterns_are_present(self) -> None:
        states = Counter(event.activity_state for event in self.events)
        self.assertEqual(set(ActivityState), set(states))
        for user_events in self.events_by_user.values():
            user_states = {event.activity_state for event in user_events}
            self.assertIn(ActivityState.SLEEP, user_states)
            self.assertIn(ActivityState.REST, user_states)
            self.assertIn(ActivityState.ORDINARY_ACTIVITY, user_states)

        average_by_state = {
            state: statistics.fmean(
                event.value for event in self.events if event.activity_state is state
            )
            for state in ActivityState
        }
        self.assertLess(
            average_by_state[ActivityState.SLEEP],
            average_by_state[ActivityState.REST],
        )
        self.assertLess(
            average_by_state[ActivityState.REST],
            average_by_state[ActivityState.ORDINARY_ACTIVITY],
        )
        self.assertLess(
            average_by_state[ActivityState.ORDINARY_ACTIVITY],
            average_by_state[ActivityState.WORKOUT],
        )

    def test_sampling_is_irregular_and_state_dependent(self) -> None:
        intervals_by_state: dict[ActivityState, list[float]] = defaultdict(list)
        for events in self.events_by_user.values():
            for previous, current in zip(events, events[1:]):
                if previous.activity_state is not current.activity_state:
                    continue
                delta = (
                    current.observed_at.astimezone(UTC)
                    - previous.observed_at.astimezone(UTC)
                ).total_seconds()
                cadence, jitter = CADENCE_SECONDS[current.activity_state]
                if cadence - jitter <= delta <= cadence + jitter:
                    intervals_by_state[current.activity_state].append(delta)

        medians = {
            state: statistics.median(intervals) for state, intervals in intervals_by_state.items()
        }
        for intervals in intervals_by_state.values():
            self.assertGreater(len(set(intervals)), 10)
        self.assertLess(medians[ActivityState.WORKOUT], medians[ActivityState.ORDINARY_ACTIVITY])
        self.assertLess(medians[ActivityState.ORDINARY_ACTIVITY], medians[ActivityState.REST])
        self.assertLess(medians[ActivityState.REST], medians[ActivityState.SLEEP])

    def test_device_off_windows_contain_no_readings_and_create_long_gaps(self) -> None:
        observed_windows = 0
        for profile in self.profiles:
            events = self.events_by_user[profile.user_id]
            utc_instants = [event.observed_at.astimezone(UTC) for event in events]
            for off_window in device_off_windows(profile, self.config):
                off_start = off_window.start.astimezone(UTC)
                off_end = off_window.end.astimezone(UTC)
                self.assertFalse(
                    any(off_start <= instant < off_end for instant in utc_instants),
                    f"event found in device-off window for {profile.user_id}",
                )
                before = [instant for instant in utc_instants if instant < off_start]
                after = [instant for instant in utc_instants if instant >= off_end]
                if before and after:
                    self.assertGreater(
                        (min(after) - max(before)).total_seconds(),
                        30 * 60,
                    )
                    observed_windows += 1
        self.assertGreaterEqual(observed_windows, 50)


if __name__ == "__main__":
    unittest.main()
