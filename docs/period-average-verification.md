# Whole-period user averages: historical extension verification

This record used the earlier seven-day January 2026 default. Its test counts,
model results, and timings remain evidence for that revision; they are not new
measurements of the current October 2026 source.
It also predates the time-weighted query contract. Arithmetic means,
`mean_recorded_bpm`, observation counts, and contributing timestamp fields
below are preserved as historical evidence, not current interface claims.

The API now has **five operations**. `user_period_average` returns one
arithmetic mean over all of one user's matching readings;
`user_period_resting_average` returns one mean over that user's `rest` readings.
The original three operations retain their semantics. No schema migration,
source-format change, or new dependency is required.

## Contract exercised

- Values and the all/rest selector are bound parameters in application-owned
  SQL; no client SQL or arbitrary state filter is accepted.
- Both new operations require an existing `user-[0-9]{4}` identity, heart rate,
  offset-aware timestamps, `start < end`, at most 31 days, and the existing
  1–1,000 output-row limit.
- The interval is `[start,end)`. Zero matches returns `rows: []`; otherwise one
  row contains `mean_recorded_bpm`, `observation_count`, `first_observed_at`, and
  `last_observed_at`. The response also contains the requested interval and
  `aggregation_basis: recorded_observations`.
- Unequal daily counts distinguish the calculation from averaging daily means:
  three rest observations averaging 60 BPM and one at 70 yield 62.5 BPM overall,
  not 65 BPM. The same fixture's all-state mean is 85 BPM over six readings.
- Natural language supports explicit ranges and the actual previous ISO week
  for both variants. Daily wording and explicit resting intent are checked
  against the candidate operation. Wrong grouping or state substitutions are
  rejected; maximum, median and unsupported grouping/filter requests remain
  rejected before model use where recognized by the bounded vocabulary.

## Executed evidence

A full run of `python -m unittest discover -s tests -v` passed **69 tests**
in **41.434 seconds** against a separate disposable PostgreSQL **16.6**
container. The PostgreSQL server reported its actual version as 16.6. Tests
cover the generator and retained source, ingestion/replay, all five query
operations, HTTP/OpenAPI, controlled NL plans, and dependency faults. Added
checks exercise period all/rest known answers, counts and contributing bounds,
unequal daily counts, start inclusion/end exclusion, absent readings, an absent
rest subset, unknown users, invalid identity/metric/range/limits, and model
substitutions between period, daily and activity-state plans.

The application image was rebuilt and run under a fresh isolated Compose
project with separate PostgreSQL/source volumes and a different loopback API
port. Validation used an environment-only container supervision adaptation;
this is **not** a claim of unchanged clean-machine Compose deployment. Existing
stacks were not replaced. Cached model weights were reused; this is not a
new download or no-cache startup measurement. The CLI actually returned all
five operation results, and the running API's OpenAPI enumerated five
operations and documented the period row schema.

The current manifest contains **26 entries**: **8 supported model questions**
(5 explicit-range and 3 relative), **16 preflight rejections**, and **2 isolated
fault cases**. The final warm real-Ollama run passed **24/24 live NL requests**
with `qwen2.5:1.5b`. For every successful question the evaluator compared the
**entire result JSON** with the response from its validated structured query,
not just operation names. Real relative requests retained the application-
resolved previous-week range and returned no invented observations outside the
historical snapshot. The two fault cases remain deterministic isolated tests,
not real-model successes. The live run also exercises the original three
operations.

## Failures retained, not hidden

The initial prompt allowed the model to substitute daily resting plans for
period requests; validation rejected those substitutions instead of executing
them. Explicit examples and a no-grouping operation cue for recognized period
requests were added, while post-model checks still inspect the original
question. With the final implementation, an earlier real-model run returned
503 for its first two questions and passed 22/24 live cases; the subsequent
warm run passed 24/24. The application timeout remains **30 seconds**. Model
listing/readiness does not prove inference completes inside that timeout;
a passing warm rerun does not erase first-request reliability limitations.
The first full-suite run also exposed an obsolete no-model-call assertion for
a now-supported period request; it was updated to assert that an incorrect
daily model plan is rejected after one call.

These are extension-specific observations, not replacement timings for the
historical ARM64 record in [verification.md](verification.md). This feature run
is not a new interruption/SIGKILL benchmark, a complete production load test,
or proof of clinical validity, source-volume-loss recovery or public-service
security. The production Compose definition and database schema were not
changed by this feature.

## Reproduction

Use an isolated disposable PostgreSQL 16.6 database for tests; the suite
truncates fixture tables. Never point it at the running demo or personal data.

```bash
HEALTH_DATA_TEST_DATABASE_URL='postgresql://health_data:health_data@127.0.0.1:55432/health_data' \
  python -m unittest discover -s tests -v
python scripts/evaluate_natural_language.py --base-url http://127.0.0.1:8000
docker compose exec api python -m health_data.cli demo-queries
curl --fail http://127.0.0.1:8000/openapi.json
```
