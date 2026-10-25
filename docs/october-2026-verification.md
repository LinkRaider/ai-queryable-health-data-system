# October 2026 default-window verification

This record validates the uncommitted default-window revision based on commit
`34382e499d8cf1a2dae2e5edf0f18887ff392519`.
The run predates the time-weighted query contract. Its source, ingestion,
replay and window measurements remain applicable to that revision; its API
results and 69-test count are not current weighted-query verification.

## Environment and isolation

- Host: macOS 26.7.1 on Apple ARM64.
- Docker client 28.0.1, Docker Engine 29.1.2 on ARM64, and Docker Compose
  2.40.3-desktop.1.
- Application PostgreSQL and test PostgreSQL: `postgres:16.6-alpine`, with the
  server reporting PostgreSQL 16.6.
- Model: real `qwen2.5:1.5b` in Ollama 0.12.6; `ollama list` reported model ID
  `65ec06548149`.
- Compose project: `health-october-review-20261024`, API port 18049, with new
  PostgreSQL, source, and Ollama volumes. Existing projects and volumes were
  not changed.

## Automated tests

Against a separate PostgreSQL 16.6 container on port 55450:

```bash
HEALTH_DATA_TEST_DATABASE_URL='postgresql://postgres@127.0.0.1:55450/health_data' \
  .venv/bin/python -m unittest discover -s tests -v
```

Result: **69 tests passed in 34.089 seconds**. The fixed query and parser
fixtures were independent of the default window; default-dependent tests used
the October window.

## Empty-volume Compose start and source bounds

The isolated stack was built and started with:

```bash
docker compose \
  --project-name health-october-review-20261024 \
  --file compose.yaml \
  --file /tmp/health-october-review-20261024.override.yaml \
  up --build --detach --remove-orphans
```

The command completed in **151.01 seconds**. The application build reused some
Docker layers, so this is not a no-cache or fresh-machine measurement. All
three named volumes for this project were new. Model preparation completed,
the API became healthy, and `/health/ready` returned database, ingestion, and
model status `ready`.

Bootstrap reported `source_reused: false`, `processed_this_attempt: 814492`,
and `inserted_this_attempt: 814492`. Independent checks found:

- JSONL lines: **814,492**.
- Metadata event count and unique-event count: **814,492** each.
- PostgreSQL observations: **814,492**.
- Source SHA-256:
  `9467de64ab2e4fca07119cd79d27d0d54e6aa3504402ce066d6e042d079715cc`.
- Requested window: `[2026-10-05T00:00:00Z, 2026-10-26T00:00:00Z)`.
- Minimum observation: `2026-10-05T00:00:00Z`.
- Maximum observation: `2026-10-25T23:59:56Z`.
- Observations before the start or at/after the end: **0**.

All readings are synthetic. In particular, 25 October is not represented as
already-collected real-world data.

## Structured and natural-language queries

The full-range structured `user_period_average` for `user-0001` returned HTTP
200 with 16,351 observations and mean 93.3 BPM. The full-range local-day
resting query returned 22 local-date groups; the UTC population query returned
21 groups, from 5 through 25 October.

The first real-model request after startup returned HTTP 200 in **20.562
seconds**. Repeating the same request with the model loaded returned the same
response in **8.190 seconds**. Neither request exceeded the application's
unchanged 30-second timeout.

The evaluation command:

```bash
.venv/bin/python scripts/evaluate_natural_language.py \
  --base-url http://127.0.0.1:18049
```

passed **24/24 live requests** in **96.730 seconds**. Eight supported requests
called Ollama and their complete result objects matched equivalent structured
queries. Sixteen requests were rejected by application validation before a
model call. The two remaining manifest entries are isolated fault-injection
tests, not real-model calls.

At execution time, `last week` resolved in UTC to
`[2026-10-12T00:00:00Z, 2026-10-19T00:00:00Z)`. The three supported relative
questions returned non-empty rows and matched their structured equivalents.
The implementation still resolves this interval from the real request time;
it does not pin `last week` to the dataset.

## Replay, restart, and interruption

A complete replay of the persisted JSONL took **35.34 seconds**, reported
`source_reused: true`, processed all 814,492 source records, and inserted zero
rows. Count and ordered database digest were unchanged:

```text
814492|b1487525aa212fcdfea76537330f440d
```

After `docker compose down` without `--volumes`, starting the same isolated
project took **38.66 seconds**. Bootstrap again reported `source_reused: true`
and zero inserts; readiness, count, and digest remained unchanged.

The isolated `scripts/test_interrupted_ingestion.sh` run sent `SIGKILL` after
6,000 rows had committed. Recovery reused the same source, reread all 814,492
records, inserted the remaining 808,492, and completed in **50 seconds**.
Uninterrupted and recovered databases had the same count and digest above.
This is a post-commit progress test; it does not claim that the signal was
synchronized inside an open transaction.

## Boundaries

The runs used real PostgreSQL and Ollama; no mocks or native substitutes were
used for end-to-end evidence. The first-start measurement had cached image
layers, and no cache-free fresh-machine run was performed. The source volume
was retained after verification; this record does not test loss, backup, or
restore of that volume.
