"""Container entry points for bootstrap and deterministic demo queries."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from health_data.config import load_settings
from health_data.db import apply_schema, connect, ingest
from health_data.query import QueryOperation, StructuredQuery, execute_query


def bootstrap() -> None:
    settings = load_settings()
    with connect(settings.database_url) as connection:
        apply_schema(connection)
        result = ingest(
            connection,
            settings.generation,
            batch_size=settings.batch_size,
        )
    print(json.dumps({"event": "bootstrap_complete", **asdict(result)}, sort_keys=True))


def demo_queries() -> None:
    settings = load_settings()
    start = settings.generation.window_start
    end = settings.generation.window_end
    requests = (
        StructuredQuery(
            operation=QueryOperation.DAILY_USER_AVERAGE,
            user_id="user-0001",
            start=start,
            end=end,
        ),
        StructuredQuery(
            operation=QueryOperation.ACTIVITY_STATE_COMPARISON,
            user_id="user-0001",
            start=start,
            end=end,
        ),
        StructuredQuery(
            operation=QueryOperation.POPULATION_DAILY_AVERAGE,
            start=start,
            end=end,
        ),
    )
    with connect(settings.database_url) as connection:
        for request in requests:
            result = execute_query(connection, request)
            print(json.dumps(result.as_dict(), sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("bootstrap", "demo-queries"))
    arguments = parser.parse_args()
    if arguments.command == "bootstrap":
        bootstrap()
    else:
        demo_queries()


if __name__ == "__main__":
    main()
