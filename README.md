# AI-Queryable Health Data System

A small end-to-end system that generates a fixed, deterministic synthetic
heart-rate snapshot for 50 users over seven days, atomically publishes it as
JSONL, replays that persisted source into PostgreSQL, and exposes bounded
structured and natural-language analytics.
It is a reproducible historical demo, not a continuously updating or real-time
health service.

The generated patterns are for an engineering demonstration. They are not
clinically validated measurements and must not be used for health decisions.

## Quick start

Prerequisites:

- Docker Engine with Docker Compose v2
- Approximately 6 GB of free disk space
- Internet access on the first start for container images, Python packages,
  and the local model
- An ARM64 or AMD64 machine supported by the pinned images

Start the complete system:

```bash
docker compose up --build
```

No model command, schema command, or data-loading command is required.
Compose starts PostgreSQL and Ollama, downloads `qwen2.5:1.5b` through a
one-shot model preparation service, publishes and ingests the fixed dataset,
then starts FastAPI at <http://127.0.0.1:8000>. OpenAPI documentation is at
<http://127.0.0.1:8000/docs>.

The first start is download-heavy. Wait for `api` to report healthy:

```bash
docker compose ps --all
curl --fail http://127.0.0.1:8000/health/ready
```

Stop services while preserving data:

```bash
docker compose down
```

Remove all persisted demo data and the downloaded model:

```bash
docker compose down --volumes
```

## Architecture

```mermaid
flowchart LR
    Client[HTTP client] --> API[FastAPI]
    API --> Validator[Allowlisted query validator]
    Validator --> PostgreSQL[(PostgreSQL 16)]
    API --> Ollama[Ollama qwen2.5 1.5B]
    ModelInit[One-shot model init] --> Ollama
    Bootstrap[One-shot bootstrap] --> Generator[Deterministic generator]
    Generator -->|temporary file + atomic rename| JSONL[(JSONL source volume)]
    JSONL -->|full replay| Bootstrap
    Bootstrap -->|idempotent batches| PostgreSQL
```

Compose contains five purposeful services:

- `postgres` stores user profiles, observations, constraints, indexes, and the
  identity and outcome of the one accepted source.
- `bootstrap` atomically publishes the fixed event sequence to the source
  volume when absent, then replays it in retry-safe batches. It exits only
  after all source records have been checked against durable rows.
- `ollama` hosts the small local interpretation model.
- `model-init` downloads the pinned model when the model volume is empty and
  exits after preparation succeeds.
- `api` starts only after bootstrap and model preparation finish. It serves
  health, structured analytics, natural-language analytics, and OpenAPI.

There is no broker. Redpanda and Kafka are intentionally rejected alternatives,
not running components. For a deterministic seven-day batch, a broker adds
deployment and recovery paths without improving the demonstrated guarantees.
PostgreSQL is both storage and analytical engine because this volume and the
three access patterns fit indexed relational aggregation.

## Data contract and synthetic patterns

The full contract and acceptance criteria are in
[`docs/specification.md`](docs/specification.md). Each v1 observation contains:

- schema version `health.heart_rate.observation.v1`;
- stable UUIDv5 event ID;
- stable user and device IDs;
- an offset-aware observation timestamp;
- metric `heart_rate`, a decimal value, and unit `bpm`;
- one of `sleep`, `rest`, `ordinary_activity`, or `workout`.

The fixed default configuration is seed `20261019`, 50 users, and
`2026-01-05T00:00:00Z` through `2026-01-12T00:00:00Z`. Profiles vary by time
zone, resting baseline, schedule, activity response, workout frequency, and
device-off tendency. State changes affect values and sampling cadence;
deterministic jitter avoids clockwork intervals; device-off intervals emit no
observations.

Determinism makes the synthetic fixture reproducible for review and tests. It
does not imply that missing measurements from real people can be regenerated.

The observed sample contains 271,620 events. Representative profiles,
state-level means, a full local-day timeline, and an explicit 74.7-minute
no-reading gap are in
[`docs/synthetic-sample.md`](docs/synthetic-sample.md).

## Idempotent ingestion and recovery from persisted source

This is the first area of deliberate depth.

Bootstrap writes one event per JSONL line to a temporary file on the source
volume, flushes it, and publishes it with an atomic rename only after generation
finishes. A small sidecar records the generation configuration, line count,
unique-event count, and source digest. A compatible published source is
validated and reused without calling the event generator. An incompatible or
corrupt published source fails explicitly; a lone file from an interrupted
initial publication is not considered complete and may be regenerated.

Every ingestion attempt reads that JSONL from the beginning. There is no
position checkpoint: the measured full replay is small enough that stable
event identity and idempotent inserts are simpler. Each batch commits
independently, so an interruption can leave earlier batches durable. On retry,
those rows are verified and skipped while missing rows are inserted:

- the same `event_id` and same content is accepted without another row;
- the same `event_id` with different content is an explicit error;
- a natural-key collision under another ID is an explicit error;
- malformed JSONL reports its line number instead of being ignored;
- file order is irrelevant because analytics use `observed_at`.

The demo database accepts one source configuration. Another seed, window,
population, generator version, or source content requires an isolated
database; dataset migration and coexistence are outside scope.

The recovery harness configures a post-commit delay, observes 6,000 durable
rows, then sends `SIGKILL`. It then replays the same persisted file from line
one:

```bash
./scripts/test_interrupted_ingestion.sh
```

This proves recovery with earlier batches already committed. Polling does not
identify whether the process had opened its next transaction at the exact
signal instant, so no claim is made about interruption inside or outside that
next transaction. The latest run reused source
digest `8a96006e1229c3fbb394603aa01581ac564879115fc1f4234c07a6375e1e7f09`,
processed all 271,620 lines, and inserted the remaining 265,620 rows in 24
seconds. Recovered and uninterrupted databases both contained 271,620 events
with digest `2791d4a9a5996490ff6191256d36ca5a`. Full evidence is in
[`docs/interruption-verification.md`](docs/interruption-verification.md).

The named volume survives normal container replacement; it is not a backup,
replica, or protection against deleting or losing the volume. Real
measurements that were never persisted cannot be reconstructed by a
deterministic generator.

## Analytical API

This is the second area of deliberate depth: explicit semantics when sampling
is irregular and data can be absent.

Every range is half-open: `start <= observed_at < end`. Timestamps require an
offset, ranges are limited to 31 days, and responses are limited to 1,000 rows.
An empty match returns HTTP 200 with an empty `rows` array. Oversized results
return HTTP 413 rather than silent truncation. Unsupported operations, metrics,
dimensions, users, and malformed timestamps return structured errors.

Only three operations exist. Every reported mean weights each recorded
observation equally. It is not a time-weighted mean and does not give each user
equal weight; state-dependent sampling cadence therefore affects it. Responses
make this explicit with `aggregation_basis: "recorded_observations"` and
`mean_recorded_bpm`.

- `daily_user_average`: mean of one user's recorded resting observations,
  grouped by local day, with the first and last contributing timestamps;
- `activity_state_comparison`: mean and count of one user's recorded
  observations by activity state;
- `population_daily_average`: mean of all recorded observations, observation
  count, contributing-user count, and first and last contributing timestamps
  by UTC day.

`observation_count`, `first_observed_at`, and `last_observed_at` describe only
the readings that contributed after all filters. More readings do not
necessarily mean better time coverage because cadence varies by state. The
span from first to last reading does not prove there were no gaps between
them. Missing groups stay absent: the API never invents a zero mean, and a day
without rows is not a day observed at zero BPM. No coverage percentage is
reported because the measurements alone provide no defensible denominator.

SQL operation and grouping fragments are application-owned constants. Every
request value is a bound parameter. No route accepts arbitrary SQL.

Daily resting average:

```bash
curl --fail --request POST http://127.0.0.1:8000/v1/query \
  --header 'Content-Type: application/json' \
  --data '{
    "operation": "daily_user_average",
    "user_id": "user-0001",
    "metric": "heart_rate",
    "start": "2026-01-05T00:00:00Z",
    "end": "2026-01-12T00:00:00Z"
  }'
```

Activity-state comparison:

```bash
curl --fail --request POST http://127.0.0.1:8000/v1/query \
  --header 'Content-Type: application/json' \
  --data '{
    "operation": "activity_state_comparison",
    "user_id": "user-0001",
    "metric": "heart_rate",
    "start": "2026-01-05T00:00:00Z",
    "end": "2026-01-12T00:00:00Z"
  }'
```

Population daily average:

```bash
curl --fail --request POST http://127.0.0.1:8000/v1/query \
  --header 'Content-Type: application/json' \
  --data '{
    "operation": "population_daily_average",
    "metric": "heart_rate",
    "start": "2026-01-05T00:00:00Z",
    "end": "2026-01-12T00:00:00Z"
  }'
```

## Constrained natural-language layer

This is Bonus 2, implemented as a constrained interface over the same validated
structured-query path rather than a third claimed depth area.

Ollama receives a question and returns only a candidate JSON plan for the three
operations. The application then:

1. accepts either two offset-aware RFC 3339 timestamps, including fractional
   seconds, or the explicit phrase `last week`;
2. resolves `last week` at request time to the previous ISO week in application
   time zone `UTC`;
3. rejects missing user identity, unsupported metrics, unsupported
   aggregations, medical-advice requests, unsupported relative dates, and
   instruction-like input;
4. validates the model response with a strict, extra-field-forbidden schema;
5. checks exact user tokens and recognized timestamp, aggregation, grouping,
   state-filter, time-zone, and operation cues against a bounded lexical
   contract;
6. applies the same structured-query validation and limits;
7. builds parameterised SQL from application-owned statements.

Model output is never treated as SQL and SQL fields are rejected. Missing or
ambiguous scope is rejected instead of inferred. Model unavailability returns
HTTP 503. Every successful response includes `interpreted_query` and
`temporal_interpretation`, including the resolved range, reference time, and
time zone, beside the result. First-person questions such as “my heart rate”
remain rejected because authentication does not provide a user identity.
This validator deliberately recognizes a constrained vocabulary; it rejects
known incompatibilities and does not claim general understanding of arbitrary
natural language.

```bash
curl --fail --request POST \
  http://127.0.0.1:8000/v1/query/natural-language \
  --header 'Content-Type: application/json' \
  --data '{
    "question": "What was user-0001'\''s average resting heart rate each day from 2026-01-05T00:00:00Z to 2026-01-12T00:00:00Z?"
  }'
```

Only `last week` is supported as relative shorthand; expressions such as
`today`, `last month`, or `past 30 days` are rejected, including when mixed
with explicit timestamps. The main demo uses explicit dates because the dataset is a January 2026
snapshot. A `last week` request is relative to the real request time and
normally returns `rows: []` once that week no longer overlaps the snapshot. The
response exposes the resolved bounds, request reference time, and time zone; it
never shifts `last week` back to the data window merely to produce rows.

Run the evaluation manifest:

```bash
python3 scripts/evaluate_natural_language.py
```

The manifest contains 22 cases. Its live run sends 20 endpoint requests: four
supported questions reach the real Ollama model, while 16 ambiguous, invalid,
or unsupported questions are rejected by application validation before a
model call. The remaining two cases—invalid model output and model
unavailability—are deterministic fault-injection tests and are not live
Ollama calls. Further isolated checks cover fractional seconds, conflicting
date expressions, malformed JSON, and invented users and dates.

The model client has a 30-second timeout. The first inference after startup can
exceed it and return HTTP 503; a later request with the model loaded can
succeed. This is a startup limitation, not a result attributed exclusively to
one host or execution environment.

## Tests

The tests use Python 3.11 or newer and a disposable PostgreSQL instance:

```bash
docker run --name health-data-test-postgres \
  --detach \
  --publish 55432:5432 \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  --env POSTGRES_DB=health_data \
  postgres:16.6-alpine

python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v

docker rm --force health-data-test-postgres
```

The updated run passed 65 tests in 17.299 seconds. They include JSONL
round-tripping and atomic publication, source reuse and incompatibility,
duplicate/conflicting/out-of-order ingestion, post-commit retry, and
hand-calculated answers for all three operations including contributing
timestamp bounds. The empty- and preserved-state Compose flow is reproducible
with:

```bash
./scripts/clean_start_e2e.sh
```

That destructive test removes this Compose project's named volumes, starts the
complete system, checks both endpoints and a rejection, measures a full replay,
then restarts Compose with its volumes preserved and confirms the row count is
unchanged.

## Measured verification

The updated empty-persisted-state run completed in 67 seconds on ARM64 with
cached image layers but empty PostgreSQL, source, and Ollama volumes. It
downloaded the model, published and stored 271,620 observations, returned
checked structured and natural-language responses, and rejected an ambiguous
question with HTTP 422. A full 271,620-line replay took 18 seconds and inserted
zero rows. A subsequent `docker compose down`/`up` with all volumes preserved
was healthy in 23 seconds and retained 271,620 rows. The updated live endpoint
evaluation passed 20/20.

The exact responses, environment, corrected failures, test evidence, and
measurement boundary are in
[`docs/verification.md`](docs/verification.md). In particular, 67 seconds is
not claimed as a no-cache fresh-VM time.

## Decisions and tradeoffs

- A fixed window makes restart behavior reproducible; it does not look
  perpetually current.
- Full JSONL replay instead of a broker or positional checkpoint keeps recovery
  inspectable. The measured 18-second replay is acceptable for this dataset;
  materially larger sources require new measurements before keeping this
  choice.
- One constrained heart-rate table provides stronger checks and clearer
  indexes than a generic entity-attribute-value schema; adding another metric
  requires a deliberate migration.
- PostgreSQL is sufficient at demo volume; high-rate longitudinal production
  data would require retention, partitioning, compression, and load evidence.
- UUIDv5 and a natural unique key spend extra index space to defend against
  both replay and identity-algorithm defects.
- Relative dates are intentionally narrow: `last week` means the previous ISO
  week relative to the real request time in UTC. Other shorthand is rejected
  rather than guessed, and an out-of-snapshot range returns an empty result.
- The 1.5B local model keeps data local and avoids paid credentials, but its
  image/model download dominates first startup and its interpretation quality
  is bounded by the explicit validator.

## Development process and review boundaries

Initial scaffolding, implementation drafts, test cases, Compose wiring, and
documentation structure were delegated. Runtime and test claims in this README
are limited to commands recorded in `docs/verification.md`. Corrections
included a Python-version dependency mismatch, a packaged schema path, Compose
image ownership, API dependency resolution, and two rejected local-model
plans.

During review, I challenged recovery based on regenerating synthetic
measurements and the checkpoint complexity supporting that path: real
measurements cannot be reconstructed if they were never retained, and the
brief did not require that mechanism. I chose a persisted JSONL source and
full idempotent replay, keeping transactional batch ingestion and PostgreSQL
without adding permanent services; implementation of that revision was
delegated. I would not delegate blindly the definition of the problem, the
failure model, or acceptance of recovery guarantees: those require human
judgment about what can actually be recovered and what the evidence proves.
Deterministic generation remains a reproducibility aid for tests, not a
recovery mechanism for real-world measurements.

Review checkpoints covered scope, synthetic-pattern plausibility, query and
error boundaries, recovery evidence, and final claims. This repository records
runtime evidence, not reviewer identities or approvals. Clinical validity,
production security, privacy policy, and operational readiness still require
domain-owner review and environment-specific evidence.

## Limitations

- A no-cache new VM and AMD64 were not available for execution; ARM64 and
  empty named volumes were tested.
- There is no authentication, authorization, TLS termination, rate limiting,
  per-user tenancy boundary, or audit log.
- There is no connection pool, horizontal API deployment, partition lifecycle,
  backup/restore automation, or production monitoring.
- The synthetic generator models recognizable schedules and gaps, not
  physiology.
- The data is a fixed historical snapshot. Normal restarts replay the same
  source and do not append current observations.
- The source named volume survives container restarts but is neither backed up
  nor replicated. Deleting it removes the persisted event source.
- Means weight recorded observations equally, so high-cadence states and users
  with more readings contribute more; no time weighting or equal-user
  weighting is claimed.
- First/last timestamps bound contributing observations but do not prove
  gap-free coverage. Missing days are omitted, never represented as zero BPM.
- The API supports three intentionally narrow operations and one metric.
- Image tags and Python direct dependencies are pinned, but image digests and
  the complete transitive Python dependency graph are not locked.
- The live model can still emit an invalid plan; that path is rejected and
  returned as an error.

## If you had two more weeks, what would you build next?

Before any public exposure, I would establish the security baseline with
authentication, per-user authorization, TLS, rate limiting, and audit logging,
then improve readiness and the reliability of the first model inference. I
would expand the user population, historical window, and seed variation to
measure actual limits, using those results and observed access patterns to
decide whether PostgreSQL partitioning and retention, Parquet, or a lakehouse
are justified. I would also explore continuous production ingestion and
streaming, considering Kafka only if retained replay, backpressure, and
independent consumers become concrete requirements; it is not needed for the
current batch. Within two weeks I would prioritize validation and tested
backup and restore paths rather than claim delivery of every proposal. These
are proposed extensions, not features claimed by the current implementation.
