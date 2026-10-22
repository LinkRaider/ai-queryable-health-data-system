from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from health_data.generator import GenerationConfig, generate_events
from health_data.source import (
    SourceError,
    ensure_source,
    metadata_path_for,
    read_events,
)


class PersistedEventSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "events.jsonl"
        self.config = GenerationConfig(days=1, user_count=1)

    def test_jsonl_round_trip_preserves_every_event_field(self) -> None:
        expected = list(generate_events(self.config))
        source = ensure_source(self.path, self.config)

        self.assertEqual(
            [event.as_dict() for event in expected],
            [event.as_dict() for event in read_events(source)],
        )
        self.assertEqual(len(expected), source.event_count)

    def test_failed_generation_does_not_publish_a_source(self) -> None:
        first = next(generate_events(self.config))

        def interrupted_generator(_config):
            yield first
            raise RuntimeError("generation interrupted")

        with self.assertRaisesRegex(RuntimeError, "generation interrupted"):
            ensure_source(
                self.path,
                self.config,
                event_generator=interrupted_generator,
            )

        self.assertFalse(self.path.exists())
        self.assertFalse(metadata_path_for(self.path).exists())

    def test_complete_source_is_reused_without_calling_generator(self) -> None:
        created = ensure_source(self.path, self.config)
        generator = Mock(side_effect=AssertionError("generator must not be called"))

        reused = ensure_source(
            self.path,
            self.config,
            event_generator=generator,
        )

        self.assertTrue(reused.reused)
        self.assertEqual(created.sha256, reused.sha256)
        generator.assert_not_called()

    def test_incompatible_configuration_is_rejected_without_replacement(self) -> None:
        created = ensure_source(self.path, self.config)
        original_bytes = self.path.read_bytes()

        with self.assertRaisesRegex(SourceError, "incompatible"):
            ensure_source(
                self.path,
                replace(self.config, seed=self.config.seed + 1),
            )

        self.assertEqual(original_bytes, self.path.read_bytes())
        self.assertEqual(created.sha256, ensure_source(self.path, self.config).sha256)

    def test_unpublished_lone_file_is_replaced(self) -> None:
        self.path.write_text('{"incomplete":true}\n', encoding="utf-8")

        source = ensure_source(self.path, self.config)

        self.assertFalse(source.reused)
        self.assertGreater(source.event_count, 0)
        self.assertTrue(metadata_path_for(self.path).exists())

    def test_invalid_event_id_reports_its_line_number(self) -> None:
        source = ensure_source(self.path, self.config)
        payload = next(read_events(source)).as_dict()
        payload["event_id"] = 42
        self.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(SourceError, "line 1.*event_id"):
            list(read_events(source))

    def test_invalid_utf8_reports_its_line_number(self) -> None:
        source = ensure_source(self.path, self.config)
        first_line = self.path.read_bytes().splitlines(keepends=True)[0]
        self.path.write_bytes(first_line + b"\xff\n")

        with self.assertRaisesRegex(SourceError, "line 2.*UTF-8"):
            list(read_events(source))

    def test_noncanonical_uuid_reports_its_line_number(self) -> None:
        source = ensure_source(self.path, self.config)
        payload = next(read_events(source)).as_dict()
        payload["event_id"] = str(uuid.uuid4())
        self.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(SourceError, "line 1.*canonical"):
            list(read_events(source))

    def test_invalid_profile_identity_reports_its_line_number(self) -> None:
        source = ensure_source(self.path, self.config)
        payload = next(read_events(source)).as_dict()
        payload["device_id"] = "device-0002"
        self.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(SourceError, "line 1.*source profile"):
            list(read_events(source))

    def test_excess_value_precision_reports_its_line_number(self) -> None:
        source = ensure_source(self.path, self.config)
        payload = next(read_events(source)).as_dict()
        payload["value"] = 60.01
        self.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(SourceError, "line 1.*decimal place"):
            list(read_events(source))


if __name__ == "__main__":
    unittest.main()
