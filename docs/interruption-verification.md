# Interrupted ingestion verification

This is a historical run against the earlier seven-day January 2026 default.
Its counts and timings are retained as observed evidence from that revision,
not as measurements of the current October 2026 source.

Command executed:

```bash
./scripts/test_interrupted_ingestion.sh
```

The script created two isolated Compose projects with empty PostgreSQL volumes
and one shared source volume. The baseline project generated and atomically
published the JSONL source, then completed normally. The recovery project
reused that exact source artifact with the same image and 1,000-event batch
size. A 100-millisecond post-commit delay widened the observation window. The
progress poll sleeps between reads, checks that bootstrap is still running, and
fails with container logs after 120 seconds.

## Observed interruption

- Signal sent to the running bootstrap container: `SIGKILL`
- Durable run status immediately after termination: `in_progress`
- Rows committed at interruption: `6,000`
- Durable `inserted_event_count`: `6,000`

The persisted row count and recorded inserted count were equal before the
signal. This establishes that earlier batches were durable. Polling does not
identify whether bootstrap had opened its next transaction at the exact signal
instant.

This experiment establishes recovery by replaying the persisted source after
earlier committed batches and final parity with an uninterrupted load. It does
not claim that the forced interruption happened inside or outside another open
transaction, and does not empirically establish rollback behavior inside one.

## Observed restart

The bootstrap container was force-recreated without the artificial delay. It
validated and reread the same 271,620-line JSONL file from the beginning,
accepted the first 6,000 durable events as exact duplicates, inserted the
remaining 265,620 rows, and reported:

```json
{
  "batches_processed": 272,
  "event": "bootstrap_complete",
  "expected_event_count": 271620,
  "inserted_event_count": 271620,
  "inserted_this_attempt": 265620,
  "processed_this_attempt": 271620,
  "source_key": "a2ee71c85a4c158a7dc60c9093573e3600f5aea416206029463cf9eb0d7e878a",
  "source_reused": true,
  "source_sha256": "8a96006e1229c3fbb394603aa01581ac564879115fc1f4234c07a6375e1e7f09",
  "status": "complete"
}
```

`265,620` new rows plus the `6,000` rows committed before termination equals
the expected `271,620`. Full source replay and recovery took 24 seconds.

## Final parity

The script calculated an ordered PostgreSQL digest over event ID, user, device,
observation timestamp, value, and activity state.

```text
uninterrupted: 271620|2791d4a9a5996490ff6191256d36ca5a
recovered:     271620|2791d4a9a5996490ff6191256d36ca5a
comparison:    identical
```

This is direct container/PostgreSQL evidence for this post-commit recovery run.
The source digest recorded by both databases was
`8a96006e1229c3fbb394603aa01581ac564879115fc1f4234c07a6375e1e7f09`,
confirming that baseline and recovery consumed the same artifact.

The isolated
`test_failure_after_committed_batch_resumes_without_duplicates` raises after
batch 2 commits, verifies 200 persisted rows, rereads the same test file from
line one, and compares final event IDs with that source. Neither test claims to
interrupt an open transaction.
