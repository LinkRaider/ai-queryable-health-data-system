# Verification record

This is the historical ARM64 record for the original three operations. Later
period-average evidence is in [period-average-verification.md](period-average-verification.md);
its new test counts and model results do not replace the measurements below.
Both records predate the time-weighted query contract; fields such as
`mean_recorded_bpm` and `recorded_observations` below describe the code tested
at that time and are not the current API.

Verification environment:

- Host architecture: Apple/ARM64
- Docker Engine: `29.1.2`, Linux/ARM64
- Docker Compose: `2.40.3-desktop.1`
- Verification date: 2026-10-22

Observed facts are separated below from isolated-test evidence and checks that
were not performed.

## Empty-persisted-state Compose run

Command:

```bash
./scripts/clean_start_e2e.sh
```

The script first ran `docker compose down --volumes --remove-orphans`, removing
the PostgreSQL, JSONL source, and Ollama named volumes. It then ran `docker
compose up --build --detach --remove-orphans`, waited for the API health check,
exercised both query routes, submitted a rejected question, measured a complete
source replay, and repeated `down`/`up` with the volumes preserved.

Observed:

- Time from starting Compose until the API health check passed: **67 seconds**
- JSONL source records generated and stored observations: **271,620**
- Users: **50**
- History: `2026-01-05T00:00:00Z` through `2026-01-12T00:00:00Z`
- PostgreSQL: healthy
- Ollama: healthy
- Model preparation: exited 0 after downloading `qwen2.5:1.5b` into the newly
  created model volume
- Bootstrap: atomically published the source, then exited 0 after 272 batches
- FastAPI: healthy on `127.0.0.1:8000`

The 67-second measurement used empty persistent volumes but retained Docker's
previously downloaded image layers. It includes rebuilding the application
image, downloading the model into an empty Ollama volume, JSONL generation,
ingestion, and API readiness. It is not presented as a no-cache, new-VM
measurement.

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
observation mean of 63.7 BPM from 25 readings between
`2026-01-05T02:30:11Z` and `2026-01-05T07:27:42Z`. The last was
`2026-01-11`, a recorded observation mean of 63.1 BPM from 36 readings between
`2026-01-11T15:55:03Z` and `2026-01-11T20:58:17Z`. These bounds identify
contributing observations; they do not prove the interval between them has no
gaps. The UTC range crosses eight calendar dates in the user's
`America/Los_Angeles` time zone.

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

Bootstrap validated and processed all 271,620 source lines in **18 seconds**,
reported `source_reused: true` and `inserted_this_attempt: 0`, and left the
observation count at **271,620**. A subsequent Compose restart with all named
volumes preserved became healthy in **23 seconds** and retained the same count.
The persisted source SHA-256 was
`8a96006e1229c3fbb394603aa01581ac564879115fc1f4234c07a6375e1e7f09`.

## Live local-model evaluation

Command:

```bash
python3 scripts/evaluate_natural_language.py
```

Observed on the rebuilt stack: **20/20 live endpoint cases passed**. These were
four supported questions that called the real Ollama model and 16 questions
rejected by application validation before a model call. The manifest contains
two additional cases—invalid model output and model unavailability—covered by
deterministic isolated tests rather than live Ollama calls.

- Three explicit-range questions produced the expected structured operations,
  user scope, and ranges.
- The `last week` question used the real request time, exposed the previous ISO
  week `2026-10-12T00:00:00Z` through `2026-10-19T00:00:00Z` in UTC from
  request reference `2026-10-22T09:36:00.266064Z`, and returned an empty result
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

The model client timeout is 30 seconds. Separate executions of this revision
produced both first-request outcomes: one first inference exceeded that timeout
and returned structured HTTP 503, while another returned HTTP 200 before the
timeout; a later loaded-model request also succeeded. This records startup
variability without attributing an exclusive cause to one host or execution
environment. The 20/20 manifest result was measured with the model available
and is not a guarantee that the first inference after startup completes within
the timeout.

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

Observed on the updated worktree: **65 tests passed in 17.299 seconds**.
Coverage includes:

- fixed-seed payload and ID reproducibility;
- profile variation, all four states, cadence jitter, and device-off gaps;
- JSONL field-preserving round trip and failed-generation publication safety;
- source reuse without generator invocation and incompatible-source rejection;
- PostgreSQL constraints and full-replay idempotency;
- exact duplicates, conflicting duplicate identity, invalid source lines,
  out-of-order events, and a late older observation;
- rejection of a different source configuration in the same demo database;
- committed-batch exception and replay from the same source;
- hand-calculated answers for all three query operations;
- first and last contributing timestamps for both daily operations;
- explicit proof that population means weight recorded observations rather
  than users equally;
- empty, invalid timestamp, unsupported metric/operation, unknown user,
  excessive range, oversized response, and database-unavailable behavior;
- structured endpoint schema excluding arbitrary SQL;
- manifest-backed endpoint assertions for the 20 live-case definitions and two
  isolated fault-injection cases, using controlled planners rather than real
  Ollama calls;
- fractional-second timestamps, conflicting temporal expressions, unsupported
  analytical intent, medical advice, and missing daily grouping;
- invalid JSON, extra SQL/model fields, invented user/range, and unavailable
  model behavior, both sourced from the explicit evaluation manifest.

`fastapi.testclient` emitted a Starlette deprecation warning recommending its
future `httpx2` transport. It did not fail a test.

The actual `SIGKILL` container recovery test is recorded separately in
`docs/interruption-verification.md`. Recovery reused the same persisted source
digest, began with 6,000 committed rows, reread all 271,620 source records,
inserted the remaining 265,620 in 24 seconds, and ended with the same ordered
database digest as the uninterrupted load:
`271620|2791d4a9a5996490ff6191256d36ca5a`.

## Failures encountered and corrected

The first final test command started the disposable PostgreSQL container and
immediately launched the suite. The server closed the first API-test
connection while still starting, so that attempt ended with one setup error.
Repeating the suite after PostgreSQL was ready passed all 65 tests. No failure
occurred in the empty-volume Compose run, preserved-volume restart, live-model
evaluation, or interruption harness.

## Not verified

- A brand-new VM with no Docker image cache was not available. Compose image
  references, builds, and model preparation were exercised, but the 67-second
  timing must not be extrapolated to an uncached network.
- AMD64 execution was not run; verification used ARM64.
- Synthetic patterns were reviewed for demo plausibility. They were not
  clinically validated.
- Production load, multi-instance concurrency, authentication, TLS, backup,
  restore, and disaster-recovery procedures are outside this challenge.
- The source volume was tested across normal container replacement, not host
  loss, volume corruption, backup, or restore.
- The interruption harness killed bootstrap after earlier batches were observed
  committed. Polling did not establish whether a subsequent transaction was
  open at the exact signal instant.
