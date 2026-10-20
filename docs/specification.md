# AI-Queryable Health Data System Specification

Status: implementation contract  
Event schema: `health.heart_rate.observation.v1`  
Demo configuration: 50 users, seven complete days, fixed seed `20261019`

This document turns the challenge brief into testable acceptance criteria before
implementation. Heart-rate values and behavioural schedules are synthetic
patterns for demonstrating an engineering system. They are not clinically
validated measurements and must not be used for health decisions.

## Scope

The system generates deterministic synthetic heart-rate observations, ingests
them directly into PostgreSQL in retry-safe batches, and exposes bounded
analytical queries through FastAPI. A small local model interprets supported
natural-language questions as structured query plans. Application code
validates those plans and builds parameterised SQL; model-generated SQL is
never accepted or executed.

The clean-start path consists only of Docker Compose services:

1. PostgreSQL stores profiles, observations, and durable ingestion progress.
2. A one-shot bootstrap service generates and ingests the configured history.
3. Ollama runs the local model, and a one-shot preparation service downloads
   the pinned model before the API starts.
4. FastAPI serves health, structured-query, and natural-language routes.

No broker, hosted model, paid API, arbitrary-SQL route, or clinical
interpretation is in scope.

## Versioned event contract

Every generated record conforms to `health.heart_rate.observation.v1`.
Additional fields require a new schema version unless they are explicitly
documented as optional.

| Field | Type | Rules |
| --- | --- | --- |
| `schema_version` | string | Exactly `health.heart_rate.observation.v1`. |
| `event_id` | UUID | UUIDv5 derived from the schema version, fixed generation configuration, user, device, and observation timestamp. Stable across retries and restarts. |
| `user_id` | string | Stable demo identifier matching `user-[0-9]{4}`. |
| `device_id` | string | Stable identifier matching `device-[0-9]{4}`; one device per demo user. |
| `observed_at` | RFC 3339 timestamp | Offset-aware and second-precision. Generated in the user's IANA time zone and stored as PostgreSQL `timestamptz`. |
| `metric` | string | Exactly `heart_rate` in v1. |
| `value` | decimal | Beats per minute, one decimal place, finite, and constrained to the synthetic safety envelope 30–220. |
| `unit` | string | Exactly `bpm`. |
| `activity_state` | enum | One of `sleep`, `rest`, `ordinary_activity`, or `workout`. Device-off intervals emit no event rather than a state. |

Canonical event identity uses the UTF-8 string:

```text
health.heart_rate.observation.v1|seed|window_start|window_end|user_id|device_id|observed_at
```

`window_start`, `window_end`, and `observed_at` use canonical RFC 3339
representations. UUIDv5 under a repository-owned namespace converts this
string into `event_id`. The database primary key is `event_id`; a secondary
unique constraint on `(user_id, device_id, observed_at, metric)` protects
against an accidental identity-algorithm regression.

User profiles are also deterministic. They include a time zone, resting
baseline, sleep schedule, activity tendency, workout tendency, and device-off
tendency. Profiles are metadata, not health events.

## Synthetic-pattern contract

The default window is fixed in configuration rather than calculated as "the
last seven days" at startup. This prevents a restart from silently generating a
shifted second history. Given the same schema version, seed, window, and user
count, generation must produce byte-for-byte equivalent ordered events and
identical IDs.

The generator will produce distinguishable patterns:

- Profiles vary in resting baseline, sleep/wake schedule, time zone, ordinary
  activity intensity, workout frequency, and device-off tendency.
- Sleep dominates each local night; rest and ordinary activity occur during
  waking hours; workouts occur as occasional bounded bursts.
- State changes alter both value distribution and sampling cadence.
- Nominal sampling intervals are approximately five minutes for sleep, three
  minutes for rest, 60–90 seconds for ordinary activity, and 15–30 seconds for
  workouts.
- Deterministic jitter prevents perfectly regular timestamps.
- Explicit device-off windows contain no observations and are long enough to
  distinguish from normal sampling jitter.
- Values remain inside the synthetic envelope while workout values are
  materially higher, in aggregate, than rest and sleep values.

Tests will check these properties statistically and structurally. They do not
assert that the data is medically realistic.

## Demonstration questions

All ranges are half-open: `start <= observed_at < end`. Input timestamps must
include an offset. Day grouping uses the selected user's profile time zone for
single-user queries and UTC for population queries.

1. **Daily resting average for one user**  
   "What was `user-0001`'s average resting heart rate each day from
   2026-01-05T00:00:00Z to 2026-01-12T00:00:00Z?"  
   Operation: `daily_user_average`; required user; fixed metric `heart_rate`;
   fixed state `rest`; grouped by local calendar day.

2. **Comparison across activity states**  
   "How did `user-0001`'s average heart rate and observation count compare
   across activity states in that range?"  
   Operation: `activity_state_comparison`; required user; grouped only by the
   four allowlisted activity states.

3. **Aggregate across users**  
   "What was the population's average heart rate and contributing-user count
   each UTC day in that range?"  
   Operation: `population_daily_average`; no user filter; grouped by UTC day.

Results contain explicit grouping dimensions, rounded
`mean_recorded_bpm`, observation count, and—where relevant—distinct user
count. Each recorded observation has equal weight; these are not time-weighted
or equal-user-weighted means. An empty match returns HTTP 200 with `rows: []`,
not an invented zero.

## Query and error boundaries

- Only the three operations above, the `heart_rate` metric, and the four
  activity states are accepted.
- All values are bound parameters. Operation and grouping SQL fragments come
  from application-owned allowlists.
- `start` must precede `end`; both timestamps must be offset-aware; ranges may
  not exceed 31 days.
- A single-user operation requires a syntactically valid, existing `user_id`.
  Population aggregation rejects a user filter.
- Responses are capped at 1,000 rows. The query fetches at most 1,001 rows and
  returns a documented HTTP 413 error instead of truncating silently.
- Malformed input, unsupported metrics/dimensions/aggregations, medical-advice
  requests, ambiguous natural language, and invalid model output return a
  structured 422 error.
- Model unavailability or timeout returns a structured 503 error.
- Database unavailability returns a structured 503 error without exposing
  credentials or internal SQL.
- Neither endpoint accepts SQL text.

For natural language, the response includes the validated interpreted request
and temporal interpretation beside the same result shape used by the
structured endpoint. Questions may contain either two explicit offset-aware
RFC 3339 timestamps, with optional fractional seconds, or `last week`. The
latter is resolved by application code at request time as the previous ISO
week in time zone `UTC`; the model receives the resolved bounds and cannot
choose them. Other relative expressions, and relative expressions mixed with
explicit timestamps, are rejected. If the real `last week` range does not
overlap the fixed historical snapshot, the valid result is empty. The
application never shifts the range merely to find data. Unsupported metrics,
aggregations, grouping intent, medical advice, missing users, conflicting date
expressions, invalid JSON, extra fields, and out-of-bounds plans are rejected.
First-person identity is rejected because authentication is not implemented.
The application requires daily wording for both daily operations, requires
activity-state wording only for the state comparison, and rejects combined or
unsupported dimensions such as hourly, daily-plus-state, device, or user
grouping. It validates these lexical operation cues before and after planning
and never substitutes another metric, user, range, aggregation, grouping, or
operation.

## Durable ingestion and recovery

An ingestion run key is a digest of schema version, seed, fixed window, user
count, and generator version. PostgreSQL stores the run key, status, total
planned batches, next batch index, inserted-event count, and timestamps.

Events are generated in a stable order and divided into fixed-size batches.
For each batch, observation inserts using `ON CONFLICT DO NOTHING` and the
checkpoint advance commit in one database transaction:

- A failure before commit leaves neither events nor checkpoint.
- A failure after commit leaves both events and the advanced checkpoint.
- Restart regenerates the identical sequence and starts at the durable next
  batch index.
- Replaying a completed run performs no duplicate inserts.
- The demo database holds one declared generation configuration. A different
  run key is rejected and requires an isolated database. Coexistence or
  migration between configurations is outside the validated design because
  stable profile IDs and overlapping natural observation keys can conflict.

The interruption test will load an identical baseline database, start a delayed
load into a fresh database, kill the bootstrap container after at least one
batch commits and before completion, record the checkpoint and row count,
restart it, and compare final row count plus a stable digest of event identity
and values with the uninterrupted baseline. This directly tests restart from a
durable post-commit checkpoint. It does not send a signal inside an open
transaction; rollback before commit remains a PostgreSQL atomicity guarantee
supported by the transaction boundary in the code rather than direct
interruption evidence. The harness checks that bootstrap remains running,
pauses between checkpoint reads, and emits bootstrap logs if a 120-second
deadline expires.

## Acceptance criteria

### Core and generation

- `docker compose up` from empty persisted state builds or pulls every required
  component, prepares the local model, ingests 50 users over the fixed
  seven-day window, and starts a ready API without hidden host setup.
- A fixed-seed replay produces the same ordered payloads and event IDs.
- Automated tests detect profile variation, all activity states,
  state-dependent irregular sampling, and genuine device-off gaps.
- The measured event count and clean-start elapsed time are recorded after the
  final empty-state run, not estimated here.

### Persistence and analytics

- PostgreSQL rejects duplicate event IDs and duplicate natural event keys.
- Replaying a complete load leaves the event count and digest unchanged.
- A different generation run key is rejected instead of mixing configurations
  in the same demo database.
- The interruption procedure resumes to the same count and digest as an
  uninterrupted load.
- Fixture tests with hand-calculated values verify all three operations,
  including averages, counts, grouping boundaries, and empty results.
- Tests cover invalid timestamps, unsupported metric and operation, excessive
  range, unknown user, and oversized response.

### APIs and local model

- Structured requests return documented JSON and execute only validated,
  parameterised queries.
- Natural-language requests invoke the Compose-managed local model, validate
  its structured plan through the same path, and return that interpretation,
  including request-time application-resolved relative-date context.
- An explicit evaluation set covers all three supported question shapes,
  ambiguity, unsupported metrics, unsupported aggregations, missing,
  conflicting, and unsupported grouping dimensions, medical advice, invalid
  model output, and model unavailability.
- Clean-start verification exercises health, one structured query, one
  supported natural-language query, one rejected natural-language query, and
  records the observed responses.

### Documentation and delivery

- The README architecture diagram matches running Compose services.
- Prerequisites and exact startup, test, replay, and interruption commands are
  documented.
- Observed end-to-end evidence, isolated-test evidence, limitations, and
  unverified claims are clearly separated.
- The README identifies deterministic synthesis and interruption-safe
  ingestion as the two depth areas.
- AI-usage notes state delegated work, human corrections, and work that
  requires human judgment without claiming reviews that did not occur.
- The README ends with the required "If you had two more weeks" paragraph.

## Chosen tradeoffs

- **Direct batches over a broker:** seven days of deterministic demo history
  does not need asynchronous transport. PostgreSQL transactions provide the
  delivery and recovery guarantees being demonstrated with less operational
  surface.
- **PostgreSQL for storage and analytics:** the exposed dimensions and volume
  fit indexed relational queries. One engine avoids a second datastore and a
  synchronization path.
- **Fixed demo snapshot over continuous ingestion:** reproducibility and
  restart safety outweigh making the sample dates appear current. Normal
  restarts do not append new observations.
- **One heart-rate table over a generic EAV design:** the only supported metric
  gets strong constraints, clear indexes, and readable queries. A new metric
  can receive a deliberate schema and query contract later.
- **Ollama with a pinned small model over a hosted LLM:** Compose can prepare a
  complete offline-capable runtime after image/model downloads, with no paid
  key. Small models can misinterpret requests, so strict validation and
  rejection are part of the design.
- **Bounded analytical operations over arbitrary SQL:** a narrow API is easier
  to validate, test, secure, and explain. It intentionally does not offer a
  general-purpose query interface.
