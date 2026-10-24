# AI-Queryable Health Data System Specification

Status: implementation contract  
Event schema: `health.heart_rate.observation.v1`  
Demo configuration: 50 users, 21 complete days, fixed seed `20261019`

This document describes the implementation-selected contract and acceptance
criteria. These choices are not additional requirements imposed by the brief. Heart-rate values and behavioural schedules are synthetic
patterns for demonstrating an engineering system. They are not clinically
validated measurements and must not be used for health decisions.

## Scope

The system generates deterministic synthetic heart-rate observations,
atomically publishes them as a JSONL source, ingests that persisted source
into PostgreSQL in retry-safe batches, and exposes bounded analytical queries
through FastAPI. A small local model interprets supported
natural-language questions as structured query plans. Application code
validates those plans and builds parameterised SQL; model-generated SQL is
never accepted or executed.

The clean-start path consists only of Docker Compose services:

1. PostgreSQL stores profiles, observations, and accepted source identity.
2. A one-shot bootstrap service publishes the configured history to a source
   volume when absent, then replays that source into PostgreSQL.
3. Ollama runs the local model, and a one-shot preparation service downloads
   the pinned model before the API starts.
4. FastAPI serves health, structured-query, and natural-language routes.

No broker (including Kafka), positional ingestion checkpoint, hosted model,
paid API, arbitrary-SQL route, or clinical interpretation is in scope.

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

The default window is fixed at `[2026-10-05T00:00:00Z,
2026-10-26T00:00:00Z)` rather than calculated from startup time. This prevents
a restart from silently generating a
shifted second history. Given the same schema version, seed, window, and user
count, generation must produce byte-for-byte equivalent ordered events and
identical IDs.
All dates and values are synthetic, including any day that is still in the
future when the demonstration is prepared.

The generator will produce distinguishable patterns:

- Profiles vary in resting baseline, sleep/wake schedule, time zone, ordinary
  activity intensity, workout frequency, and device-off tendency.
- Sleep dominates each local night; rest and ordinary activity occur during
  waking hours; workouts occur as occasional bounded bursts.
- State changes alter both value distribution and sampling cadence.
- Nominal sampling intervals are approximately five minutes for sleep, three
  minutes for rest, 60–90 seconds for ordinary activity, and 15–25 seconds for
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
include an offset. The single-user daily operation groups by the profile time zone; the population
daily operation groups by UTC. The state comparison groups by state, not day.

1. **Daily resting average for one user**  
   "What was `user-0001`'s average resting heart rate each day from
   2026-10-05T00:00:00Z to 2026-10-26T00:00:00Z?"
   Operation: `daily_user_average`; required user; fixed metric `heart_rate`;
   fixed state `rest`; grouped by local calendar day.

2. **Comparison across activity states**  
   "How did `user-0001`'s time-weighted heart rate compare
   across activity states in that range?"  
   Operation: `activity_state_comparison`; required user; grouped only by the
   four allowlisted activity states.

3. **Aggregate across users**  
   "What was the population's average heart rate and contributing-user count
   each UTC day in that range?"  
   Operation: `population_daily_average`; no user filter; grouped by UTC day.

4. **Whole-period user mean, all readings**
   Operation: `user_period_average`; required user; no grouping or activity
   filter; time-weighted mean over valid held intervals in `[start,end)`.
5. **Whole-period user mean, resting readings**
   Operation: `user_period_resting_average`; required user; fixed `rest` filter;
   no grouping. Explicit resting requests never select the all-state operation.

Both period operations return zero or one row:
`time_weighted_mean_bpm` and `contributing_duration_seconds`. An aggregate with
no valid contributing duration is suppressed, leaving `rows: []`; top-level
quality metadata remains present. These operations reuse the existing
observations table; no migration or source change is required.

Natural language without daily/state grouping selects a period operation;
“rest”/“resting” selects the rest-only variant. Explicit dates and the real
previous ISO week are supported for both. Daily rest semantics stay unchanged.
A candidate plan with mismatched grouping or rest scope is rejected rather
than silently answering another question. Maximum, median, other state-only
filters, and extra grouping dimensions remain outside this bounded contract.

Every reading defines a constant-value interval ending at the earliest of the
next reading for the same user/device/metric, its original timestamp plus
`MAX_HOLD_SECONDS`, or the request end. The default is 360 seconds through
`HEALTH_DATA_MAX_HOLD_SECONDS`. The immediately preceding reading can
contribute after `start` only while that original validity remains. The next
reading is determined before state filtering. There is no interpolation,
unlimited extrapolation, or zero fill.

Intervals are clipped to `[start,end)` and split at the relevant group
boundary: the user's local day, UTC day for population, or originating state.
Timezone conversion determines local boundaries, so elapsed durations remain
correct across daylight-saving changes rather than assuming a 24-hour day.
Rows contain `time_weighted_mean_bpm` and explicit contributing seconds;
population rows use contributing user-seconds and contributing-user count.

A separate quality object reports missing time even when no row can be
aggregated. Individual quality uses availability of any reading before a rest
filter and reports requested, observed and unobserved seconds, gap count and
largest gap. Population quality derives expected users from `users`, calculates
gaps independently per user, and reports expected/observed users and
requested/observed/unobserved user-seconds plus largest individual gap. These
values measure availability under the hold assumption, not physiological
coverage. Empty groups remain absent and no zero mean is invented.

## Query and error boundaries

- Only the five operations above, the `heart_rate` metric, and the four
  activity states are accepted.
- All values are bound parameters. Operation and grouping SQL fragments come
  from application-owned allowlists.
- `start` must precede `end`; both timestamps must be offset-aware; ranges may
  not exceed 31 days. This does not extend the 21-day dataset or fill
  dates without observations.
- A single-user operation requires a syntactically valid, existing `user_id`.
  Population aggregation rejects a user filter.
- row_limit accepts 1–1,000 aggregated output rows (default 1,000).
  The query fetches row_limit + 1 and returns HTTP 413 on overflow, not
  a truncated result. Four state groups with row_limit=2 overflow;
  raw observation count does not trigger this error. The default ceiling
  is not normally reachable with these operations and the 31-day range.
- Malformed input, unsupported metrics/dimensions/aggregations, medical-advice
  requests, ambiguous natural language, and invalid model output return a
  structured 422 error.
- Model unavailability or timeout returns a structured 503 error.
- Database unavailability returns a structured 503 error without exposing
  credentials or internal implementation details.
- Neither endpoint accepts SQL text.

For natural language, the response includes the validated interpreted request
and temporal interpretation beside the same result shape used by the
structured endpoint. Questions may contain either two explicit offset-aware
RFC 3339 timestamps, with optional fractional seconds, or `last week`. The
latter is resolved by application code at request time as the previous ISO
week in time zone `UTC`; the model receives the resolved bounds and cannot
choose them. Other relative expressions, and relative expressions mixed with
explicit timestamps, are rejected. If the real `last week` range does not
overlap the fixed synthetic snapshot, the valid result is empty. The
application never shifts the range merely to find data. Unsupported metrics,
aggregations, grouping intent, medical advice, missing users, conflicting date
expressions, invalid JSON, extra fields, and out-of-bounds plans are rejected.
First-person identity is rejected because authentication is not implemented.
The application requires daily wording for both daily operations, requires
activity-state wording only for the state comparison, and rejects combined or
unsupported dimensions such as hourly, daily-plus-state, device, or user
grouping. It also rejects state filters unsupported by an operation, UTC
grouping for the local-day user operation, local grouping for the UTC
population operation, malformed user tokens, and invalid calendar timestamps.
It validates these recognized lexical cues before and after planning and
rejects detected mismatches. This is a bounded contract, not a claim of general
natural-language understanding.

The period average is calculated directly from valid held intervals, not from
previously calculated daily averages. The two user-period operations return one
value for the requested interval; daily operations split those intervals and
return a value for each contributing day.

Core availability means being able to run validated structured queries against PostgreSQL; inference availability means being able to turn a supported English question into a plan within the timeout. Separate checks for these capabilities are proposed, not implemented. Today /health/ready combines completed ingestion, existing observations, and model listing; it does not exercise inference. Once the API is running, /v1/query does not call the model.

## Persisted source, idempotent ingestion, and recovery

The source consists of one event per JSONL line plus a minimal sidecar with the
source format, generation configuration, line and unique-event counts, and
SHA-256 digest. Initial generation writes a temporary file in the same volume,
flushes it, and renames it to the final path only after every event is written.
The sidecar is published last. A JSONL file without its sidecar is not a
published source and may be replaced after an interrupted initial generation.

A complete compatible source is verified and reused without calling the event
generator. A different configuration or a digest/count mismatch fails instead
of silently replacing the source. Determinism supports reproducible fixture
creation; ingestion recovery depends on the published file, not regeneration.

Every attempt reads the source from its first line and writes transactional
batches. Stable event and natural identities make that full replay idempotent:

- Same `event_id` and same content is accepted without another row.
- Same `event_id` and different content is rejected explicitly.
- A natural identity already attached to another `event_id` is rejected.
- Invalid JSON or event shape reports the source line number.
- Earlier committed batches can remain after failure; retry verifies those
  rows and inserts only missing observations.
- Arrival order does not affect final analytics, which filter and group by
  `observed_at`.

Idempotence applies to repeated delivery of the same event, not to distinct
new measurements; it is not a general end-to-end exactly-once guarantee.
No positional checkpoint is maintained. In the recorded isolated run, full
replay of the current 814,492-line source took 35.34 seconds and inserted zero
rows; the environment and cache boundary are in
`october-2026-verification.md`. PostgreSQL records source identity, expected
unique count, inserted count, and completion status. The demo database accepts
one source configuration; migration and coexistence are not implemented.

The interruption harness gives baseline and recovery databases the same source
volume, observes previously committed rows while a post-commit delay is
configured, sends `SIGKILL`, then replays the file from line one. It compares
final count and a stable database digest with the uninterrupted load. This
tests recovery with earlier durable batches. Polling cannot establish whether
the next transaction was open at the exact signal instant, so the harness makes
no inside-transaction or outside-transaction claim. It checks container state
and fails with logs after a bounded wait.

The local named volume preserves the file across normal container replacement.
It is not backup, replication, or recovery from loss of that volume. Real
measurements that were never persisted cannot be reconstructed.

## Acceptance criteria

### Core and generation

- `docker compose up` from empty persisted state builds or pulls every required
  component, prepares the local model, ingests 50 users over the fixed
  21-day window, and starts a ready API without hidden host setup.
- A fixed-seed replay produces the same ordered payloads and event IDs.
- A complete JSONL source is published by same-volume rename and reused on
  later runs; partial publication and incompatible configuration are tested.
- Automated tests detect profile variation, all activity states,
  state-dependent irregular sampling, and genuine device-off gaps.
- The measured event count and clean-start elapsed time are recorded after the
  final empty-state run, not estimated here.

### Persistence and analytics

- PostgreSQL rejects duplicate event IDs and duplicate natural event keys.
- Replaying a complete source leaves the event count and digest unchanged.
- Exact duplicate events are accepted, conflicting duplicate IDs are rejected,
  and invalid lines identify their source position.
- Out-of-order and late older events produce the same analytical result as the
  same unique observations in time order.
- A different source configuration is rejected instead of mixing
  configurations in the same demo database.
- The interruption procedure resumes to the same count and digest as an
  uninterrupted load by rereading the same persisted file.
- Fixture tests with hand-calculated values verify all five operations,
  including unequal intervals, carry-in validity, state changes, gaps,
  local/UTC day boundaries, daylight-saving transitions, population
  user-seconds, out-of-order readings, and empty results.
- Tests cover invalid timestamps, unsupported metric and operation, excessive
  range, unknown user, and oversized response.

### APIs and local model

- Structured requests return documented JSON and execute only validated,
  parameterised queries.
- Natural-language requests invoke the Compose-managed local model, validate
  its structured plan through the same path, and return that interpretation,
  including request-time application-resolved relative-date context.
- The explicit evaluation manifest contains 32 cases: 30 live natural-language endpoint
  requests, of which eight supported questions call the real model and 22 are
  rejected before model use, plus two deterministic isolated fault-injection
  cases for invalid model output and model unavailability.
- Clean-start verification exercises health, one structured query, one
  supported natural-language query, one rejected natural-language query, and
  records the observed responses.

### Documentation and delivery

- The README architecture diagram matches running Compose services.
- Prerequisites and exact startup, test, replay, and interruption commands are
  documented.
- Observed end-to-end evidence, isolated-test evidence, limitations, and
  unverified claims are clearly separated.
- The README identifies idempotent ingestion from a persisted source and
  explicit analytical semantics under irregular/missing samples as the two
  depth areas.
- AI-usage notes state delegated work, human corrections, and work that
  requires human judgment without claiming reviews that did not occur.
- The README ends with the required "If you had two more weeks" paragraph.

## Chosen tradeoffs

- **Persisted JSONL and direct batches over a broker:** 21 days of demo
  history does not need asynchronous transport. An inspectable source plus
  PostgreSQL transactions provides the demonstrated replay behavior with less
  operational surface. Continuous ingestion and streaming are not implemented;
  Kafka would be reconsidered only for measured requirements such as retained
  replay, backpressure, and independent consumers.
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
