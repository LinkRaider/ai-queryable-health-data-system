#!/usr/bin/env python3
"""Exercise the explicit NL evaluation set against the running local model."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "evaluation" / "natural_language_cases.json"
SNAPSHOT_START = datetime(2026, 1, 5, tzinfo=UTC)
SNAPSHOT_END = datetime(2026, 1, 12, tzinfo=UTC)


def relative_result_matches_snapshot(
    start: datetime,
    end: datetime,
    rows: object,
) -> bool:
    if not isinstance(rows, list):
        return False
    overlaps_snapshot = start < SNAPSHOT_END and end > SNAPSHOT_START
    return bool(rows) if overlaps_snapshot else rows == []


def post_question(base_url: str, question: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/query/natural-language",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    failures = 0
    isolated_cases = 0

    for case in cases:
        if case["category"] in {"invalid_model_output", "model_unavailable"}:
            isolated_cases += 1
            print(
                json.dumps(
                    {
                        "name": case["name"],
                        "category": case["category"],
                        "live_status": "covered_by_isolated_test",
                    },
                    sort_keys=True,
                )
            )
            continue
        status, payload = post_question(args.base_url, case["question"])
        if case["category"] in {"supported", "supported_relative"}:
            interpreted = payload.get("interpreted_query", {})
            temporal = payload.get("temporal_interpretation", {})
            result = payload.get("result", {})
            if case["category"] == "supported":
                passed = (
                    status == 200
                    and result.get("aggregation_basis") == "recorded_observations"
                    and interpreted.get("operation") == case["plan"]["operation"]
                    and interpreted.get("user_id") == case["plan"]["user_id"]
                    and interpreted.get("start") == case["plan"]["start"]
                    and interpreted.get("end") == case["plan"]["end"]
                )
            else:
                try:
                    reference = datetime.fromisoformat(
                        temporal.get("reference_time", "").replace("Z", "+00:00")
                    )
                    start = datetime.fromisoformat(
                        temporal.get("start", "").replace("Z", "+00:00")
                    )
                    end = datetime.fromisoformat(
                        temporal.get("end", "").replace("Z", "+00:00")
                    )
                    passed = (
                        status == 200
                        and result.get("aggregation_basis") == "recorded_observations"
                        and interpreted.get("operation") == case["expected_operation"]
                        and interpreted.get("user_id") == case["expected_user_id"]
                        and interpreted.get("start") == temporal.get("start")
                        and interpreted.get("end") == temporal.get("end")
                        and temporal.get("kind") == "previous_iso_week"
                        and temporal.get("timezone") == "UTC"
                        and end - start == timedelta(days=7)
                        and end <= reference < end + timedelta(days=7)
                        and abs(datetime.now(UTC) - reference) < timedelta(minutes=2)
                        and relative_result_matches_snapshot(
                            start,
                            end,
                            result.get("rows"),
                        )
                    )
                except (TypeError, ValueError):
                    passed = False
            observed = {
                "interpreted_query": interpreted,
                "temporal_interpretation": temporal,
            }
        else:
            expected_code = {
                "ambiguous": "ambiguous_question",
                "unsupported_metric": "unsupported_metric",
            }[case["category"]]
            passed = (
                status == 422
                and payload.get("error", {}).get("code") == expected_code
            )
            observed = payload.get("error")
        failures += int(not passed)
        print(
            json.dumps(
                {
                    "name": case["name"],
                    "category": case["category"],
                    "passed": passed,
                    "status": status,
                    "observed": observed,
                },
                sort_keys=True,
            )
        )

    print(
        json.dumps(
            {
                "cases": len(cases),
                "isolated_cases": isolated_cases,
                "live_cases": len(cases) - isolated_cases,
                "failures": failures,
                "live_passed": len(cases) - isolated_cases - failures,
            },
            sort_keys=True,
        )
    )
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
