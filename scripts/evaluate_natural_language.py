#!/usr/bin/env python3
"""Exercise the explicit NL evaluation set against the running local model."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "evaluation" / "natural_language_cases.json"


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

    for case in cases:
        status, payload = post_question(args.base_url, case["question"])
        if case["category"] == "supported":
            passed = (
                status == 200
                and payload.get("interpreted_query", {}).get("operation")
                == case["plan"]["operation"]
                and payload.get("interpreted_query", {}).get("user_id")
                == case["plan"]["user_id"]
            )
            observed = payload.get("interpreted_query")
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
                "failures": failures,
                "passed": len(cases) - failures,
            },
            sort_keys=True,
        )
    )
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
