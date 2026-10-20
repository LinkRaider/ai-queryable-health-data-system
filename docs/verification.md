# Verification record

Verification environment:

- Host architecture: Apple/ARM64
- Docker Engine: `29.1.2`, Linux/ARM64
- Docker Compose: `2.40.3-desktop.1`
- Verification date: 2026-10-20

Observed facts are separated below from isolated-test evidence and checks that
were not performed.

## Empty-persisted-state Compose run

Command:

```bash
./scripts/clean_start_e2e.sh
```

The script first ran `docker compose down --volumes --remove-orphans`, removing
both PostgreSQL and Ollama named volumes. It then ran `docker compose up
--build --detach --remove-orphans`, waited for the API health check, exercised
both query routes, submitted a rejected question, and replayed bootstrap.

Observed:

- Time from starting Compose until the API health check passed: **286 seconds**
- Generated and stored observations: **271,620**
- Users: **50**
- History: `2026-01-05T00:00:00Z` through `2026-01-12T00:00:00Z`
- PostgreSQL: healthy
- Ollama: healthy
- Model preparation: exited 0 after downloading `qwen2.5:1.5b` into the newly
  created model volume
- Bootstrap: exited 0 after 272 committed batches
- FastAPI: healthy on `127.0.0.1:8000`

The 286-second measurement used empty persistent volumes but retained Docker's
previously downloaded image layers. It includes rebuilding the application
image, downloading the model into an empty Ollama volume, generation,
ingestion, and API readiness. It is not presented as a no-cache, new-VM
measurement. Earlier empty-volume runs took 99, 144, 149, 150, 158, 208, 236,
and 450 seconds, and the first Ollama image/model setup earlier in development
took 1,881.738 seconds on the same network, demonstrating the effect of network
and registry caching.

Health response:

```json
{"status":"ready","database":"ready","ingestion":"complete","model":"ready"}
```

Structured activity comparison for `user-0001`:

```json
{
  "operation": "activity_state_comparison",
  "metric": "heart_rate",
  "start": "2026-01-05T00:00:00+00:00",
  "end": "2026-01-12T00:00:00+00:00",
  "aggregation_basis": "recorded_observations",
  "rows": [
    {"activity_state": "sleep", "mean_recorded_bpm": 55.9, "observation_count": 707},
    {"activity_state": "rest", "mean_recorded_bpm": 63.3, "observation_count": 472},
    {"activity_state":"ordinary_activity","mean_recorded_bpm":95.2,"observation_count":3587},
    {"activity_state": "workout", "mean_recorded_bpm": 154.4, "observation_count": 929}
  ]
}
```

The main natural-language demonstration used explicit snapshot dates:
`What was user-0001's average resting heart rate each day from
2026-01-05T00:00:00Z to 2026-01-12T00:00:00Z?`. It returned HTTP 200 and was
interpreted as:

```json
{
  "operation": "daily_user_average",
  "user_id": "user-0001",
  "metric": "heart_rate",
  "start": "2026-01-05T00:00:00Z",
  "end": "2026-01-12T00:00:00Z",
  "row_limit": 1000
}
```

The response also exposed the application-owned temporal interpretation as an
explicit range:

```json
{
  "kind": "explicit_range",
  "start": "2026-01-05T00:00:00Z",
  "end": "2026-01-12T00:00:00Z",
  "timezone": null,
  "reference_time": null
}
```

It returned eight local-day groups. The first was `2026-01-04`, a recorded
observation mean of 63.7 BPM from 25 readings, and the last was `2026-01-11`,
a recorded observation mean of 63.1 BPM from 36 readings. The UTC range
crosses eight calendar dates in the user's `America/Los_Angeles` time zone.

The ambiguous question `What was the average heart rate from
2026-01-05T00:00:00Z to 2026-01-12T00:00:00Z?` returned HTTP 422:

```json
{
  "error": {
    "code": "ambiguous_question",
    "detail": "question must include an explicit user ID or population scope"
  }
}
```

Bootstrap replay reported `inserted_this_attempt: 0`; the observation count
remained **271,620**.

## Live local-model evaluation

Command:

```bash
python3 scripts/evaluate_natural_language.py
```

Observed on the rebuilt stack: **20/20 live endpoint cases passed**. The manifest also
contains invalid model output and model unavailability as deterministic
fault-injection cases covered by the isolated suite.

- Three explicit-range questions produced the expected structured operations,
  user scope, and ranges.
- The `last week` question used the real request time, exposed the previous ISO
  week `2026-10-12T00:00:00Z` through `2026-10-19T00:00:00Z` in UTC from
  request reference `2026-10-20T16:50:00.128731Z`, and returned an empty result
  because it did not overlap the January snapshot.
- The evaluator derives whether rows are expected from overlap between the
  resolved week and the fixed snapshot; it does not always require an empty
  result.
- The ambiguous question returned HTTP 422 `ambiguous_question`.
- The step-count question returned HTTP 422 `unsupported_metric`.
- Maximum, minimum, median, and count-only questions returned HTTP 422
  `unsupported_operation` before model use.
- A population average without daily grouping, an activity-state comparison
  with daily grouping, and an hourly grouping request returned HTTP 422
  `unsupported_operation` before model use.
- A population resting filter, UTC grouping for the local-day user operation,
  local grouping for the UTC population operation, and an activity-state
  subset returned HTTP 422 `unsupported_operation` before model use.
- An invalid calendar date returned HTTP 422 `invalid_timestamp`; a suffixed
  user token returned HTTP 422 `ambiguous_question`. Neither reached the model.
- The medical-advice question returned HTTP 422 `unsupported_question` before
  model use.
- A separate live question with `.500Z` bounds returned HTTP 200 and exposed
  normalized `.500000Z` bounds in both the interpreted query and temporal
  interpretation.

The initial live evaluation passed 3/5. The 1.5B model shortened
`user-0001` to `0001` in the activity plan and emitted a placeholder user for
the population plan. Both outputs were correctly rejected by grounding
validation. The prompt was then tightened with explicit user-copy and JSON-null
rules plus examples; the repeated live evaluation passed 5/5. The
application-owned `last week` resolver now uses the real request time rather
than the snapshot end date.

## Automated test evidence

Command:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Observed on the updated worktree: **51 tests passed in 11.900 seconds**.
Coverage includes:

- fixed-seed payload and ID reproducibility;
- profile variation, all four states, cadence jitter, and device-off gaps;
- PostgreSQL constraints and replay idempotency;
- rejection of a different generation run key in the same demo database;
- committed-batch exception and resume;
- hand-calculated answers for all three query operations;
- explicit proof that population means weight recorded observations rather
  than users equally;
- empty, invalid timestamp, unsupported metric/operation, unknown user,
  excessive range, oversized response, and database-unavailable behavior;
- structured endpoint schema excluding arbitrary SQL;
- twenty live endpoint cases and two isolated fault-injection cases;
- fractional-second timestamps, conflicting temporal expressions, unsupported
  analytical intent, medical advice, and missing daily grouping;
- invalid JSON, extra SQL/model fields, invented user/range, and unavailable
  model behavior, both sourced from the explicit evaluation manifest.

`fastapi.testclient` emitted a Starlette deprecation warning recommending its
future `httpx2` transport. It did not fail a test.

The actual `SIGKILL` container recovery test is recorded separately in
`docs/interruption-verification.md`: both uninterrupted and recovered loads
ended at 271,620 rows with ordered digest
`2791d4a9a5996490ff6191256d36ca5a`.
The harness's negative-control run with an unreachable checkpoint and a
one-second deadline exited non-zero with the expected timeout diagnostic.

## Failures encountered and corrected

1. `psycopg-binary==3.2.3` had no local Python 3.14 wheel. It was replaced by
   pinned `3.3.6`; installation and all database tests then passed.
2. The first Compose project name collided with a pre-existing local project
   named `health-data-system`. Its containers and PostgreSQL volume were
   removed by the attempted clean start; source files were untouched. This
   repository now uses the unique project name `ai-health-data-solution`.
3. Two services initially tried to export the same application image in
   parallel. Image ownership was reduced to the bootstrap build and dependent
   services reuse it.
4. The first bootstrap image could not locate `sql/schema.sql` from the
   installed wheel path. The image now sets an explicit in-image schema path.
5. The first API test run resolved a function-local dependency alias as a
   forward reference and returned 422. The dependency was moved to module
   scope; all endpoint and OpenAPI tests passed.
6. The first local-model evaluation exposed the two invalid user fields
   described above. Validation rejected them, and the revised constrained
   prompt produced 5/5 valid final interpretations.
7. The first fractional-second test expected a shortened `.5Z` serialization;
   Pydantic correctly emitted `.500000Z`. The assertion was corrected and all
   49 tests passed.
8. The first final Compose attempt hit a missing internal Docker extraction
   snapshot while exporting the application image. Repeating the same
   empty-volume build succeeded; this was a Docker cache failure before any
   service started.

No failure occurred in the successful final empty-persisted-state retry.

## Not verified

- A brand-new VM with no Docker image cache was not available. Compose image
  references, builds, and model preparation were exercised, but the 286-second
  timing must not be extrapolated to an uncached network.
- AMD64 execution was not run; verification used ARM64.
- Synthetic patterns were reviewed for demo plausibility. They were not
  clinically validated.
- Production load, multi-instance concurrency, authentication, TLS, backup,
  restore, and disaster-recovery procedures are outside this challenge.
