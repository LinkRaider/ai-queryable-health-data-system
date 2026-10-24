# Time-weighted analytics verification

This record covers the uncommitted time-weighted query revision based on
commit `34382e499d8cf1a2dae2e5edf0f18887ff392519`, including the preserved local
October-window changes.

## Environment and isolation

- Host: Apple ARM64.
- Test database and Compose database: PostgreSQL 16.6.
- Compose project: `health-weighted-review-20261024`.
- API port: 18050.
- New isolated PostgreSQL, JSONL source, and Ollama volumes; existing projects
  and volumes were not modified.
- Real Ollama `qwen2.5:1.5b`; no mock or native model substitute.

## Automated tests

```bash
HEALTH_DATA_TEST_DATABASE_URL='postgresql://postgres@127.0.0.1:55451/health_data' \
  .venv/bin/python -m unittest discover -s tests -v
```

Result: **79 tests passed in 36.054 seconds**.

Hand-calculated fixtures cover:

- unequal intervals producing 96.0 BPM instead of the 90.0 arithmetic sample
  mean;
- a rest interval stopped by a later workout reading before the rest filter;
- carry-in with remaining validity and a fully expired carry-in;
- last-reading validity, initial/interior/final gaps, and no-data requests;
- local and UTC midnight splitting;
- 23-hour and 25-hour local days across daylight-saving changes;
- population pooling with different observed user-seconds and expected users
  from `users`;
- insertion order independence, alongside the existing duplicate/conflict and
  out-of-order ingestion tests.

## Isolated Compose run

```bash
docker compose \
  --project-name health-weighted-review-20261024 \
  --file compose.yaml \
  --file /tmp/health-weighted-review-20261024.override.yaml \
  up --build --detach --remove-orphans
```

The command completed in **140.81 seconds**. Some application image layers
were cached, so this is not a no-cache or fresh-machine measurement. Bootstrap
generated and inserted **814,492** observations from the unchanged October
source, PostgreSQL reported version 16.6, and `/health/ready` reported the
database, ingestion, and model ready.

All five CLI operations completed in **6.28 seconds**. Every result used
`aggregation_basis: time_weighted_observation_hold` and
`max_hold_seconds: 360`.

Observed full-window examples:

- `user_period_average`: 77.5 BPM over 1,769,875 contributing seconds.
- `user_period_resting_average`: 63.4 BPM over 250,832 contributing seconds.
- `daily_user_average`: 22 local-date rows.
- `activity_state_comparison`: four state rows.
- `population_daily_average`: 21 UTC-date rows.

For `user-0001`, quality reported 1,814,400 requested seconds, 1,769,875
observed seconds, 44,525 unobserved seconds, 12 gaps, and a 6,357-second
largest gap. Population quality reported 50 expected and observed users,
90,720,000 requested user-seconds, 89,135,625 observed user-seconds,
1,584,375 unobserved user-seconds, 364 gaps, and a 7,253-second largest
individual gap.

## Real-model evaluation

The first supported request after startup returned HTTP 200 in **15.852
seconds**. Repeating the same request with the model loaded returned the same
response in **5.755 seconds**. The application timeout remained 30 seconds.

```bash
.venv/bin/python scripts/evaluate_natural_language.py \
  --base-url http://127.0.0.1:18050
```

Result: **30/30 live requests passed in 59.765 seconds**. Eight supported
requests called Ollama and their complete result objects matched the equivalent
structured responses. Twenty-two requests were rejected before model use,
including arithmetic/geometric/root-mean-square means and custom hold
overrides. The two
isolated fault entries remain tests rather than live model calls.

At execution time, `last week` resolved to
`[2026-10-12T00:00:00Z, 2026-10-19T00:00:00Z)`. The whole-period user result
was 77.3 BPM over 591,932 observed seconds; quality exposed 12,868 unobserved
seconds across three gaps. Relative natural-language results matched their
structured equivalents in full.

## Boundaries

This run validates query semantics, HTTP/OpenAPI through automated tests,
Compose startup, and real-model parity. It does not establish clinical
validity for the 360-second hold, production performance, equal-person
weighting, cache-free deployment, or physiological coverage. Ingestion code
was unchanged; the prior replay and SIGKILL records remain historical and were
not rerun as new time-weighting evidence.
