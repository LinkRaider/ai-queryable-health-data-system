"""Container entry points for bootstrap and deterministic demo queries."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from health_data.config import load_settings
from health_data.db import apply_schema, connect, ingest
from health_data.query import QueryOperation, StructuredQuery, execute_query
from health_data.source import ensure_source


def bootstrap() -> None:
    settings = load_settings()
    source = ensure_source(settings.source_path, settings.generation)
    with connect(settings.database_url) as connection:
        apply_schema(connection)
        result = ingest(
            connection,
            source,
            settings.generation,
            batch_size=settings.batch_size,
            batch_delay_seconds=settings.batch_delay_seconds,
        )
    print(
        json.dumps(
            {
                "event": "bootstrap_complete",
                "source_path": str(source.path),
                "source_reused": source.reused,
                "source_sha256": source.sha256,
                **asdict(result),
            },
            sort_keys=True,
        )
    )


def demo_queries() -> None:
    settings = load_settings()
    start = settings.generation.window_start
    end = settings.generation.window_end
    requests = (
        StructuredQuery(
            operation=QueryOperation.USER_PERIOD_AVERAGE,
            user_id="user-0001", start=start, end=end,
        ),
        StructuredQuery(
            operation=QueryOperation.USER_PERIOD_RESTING_AVERAGE,
            user_id="user-0001", start=start, end=end,
        ),
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
            result = execute_query(
                connection,
                request,
                max_hold_seconds=settings.max_hold_seconds,
            )
            print(json.dumps(result.as_dict(), sort_keys=True))


def serve_api() -> None:
    import uvicorn

    uvicorn.run(
        "health_data.api:app",
        host="0.0.0.0",
        port=8000,
        access_log=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("api", "bootstrap", "demo-queries"))
    arguments = parser.parse_args()
    if arguments.command == "bootstrap":
        bootstrap()
    elif arguments.command == "demo-queries":
        demo_queries()
    else:
        serve_api()


if __name__ == "__main__":
    main()
