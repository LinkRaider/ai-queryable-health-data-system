# Interrupted ingestion verification

Command executed:

```bash
./scripts/test_interrupted_ingestion.sh
```

The script created two isolated Compose projects with empty PostgreSQL volumes.
The baseline project completed normally. The recovery project used the same
image, fixed generation configuration, and 1,000-event batch size, with a
100-millisecond post-commit delay so the process could be interrupted at a
known durable checkpoint. The checkpoint poll sleeps between reads, checks that
the bootstrap container is still running, and fails with container logs after
120 seconds.

## Observed interruption

- Signal sent to the running bootstrap container: `SIGKILL`
- Durable run status immediately after termination: `in_progress`
- Durable `next_batch_index`: `6` of `272`
- Rows committed at interruption: `6,000`
- Durable `inserted_event_count`: `6,000`

The persisted row count and checkpoint-aligned inserted count were equal. The
termination happened during the post-commit delay after batch 6, not inside an
uncommitted transaction.

This experiment establishes recovery from a durable checkpoint and final
parity with an uninterrupted load. It does not establish, by forced
interruption, rollback behavior inside an open transaction. That behavior
follows from PostgreSQL transaction atomicity and the code placing event
inserts and checkpoint advancement in the same transaction.

## Observed restart

The bootstrap container was force-recreated without the artificial delay. It
read checkpoint 6, regenerated the deterministic sequence, skipped the first
six batches, and reported:

```json
{
  "event": "bootstrap_complete",
  "expected_event_count": 271620,
  "inserted_event_count": 271620,
  "inserted_this_attempt": 265620,
  "next_batch_index": 272,
  "run_key": "a2ee71c85a4c158a7dc60c9093573e3600f5aea416206029463cf9eb0d7e878a",
  "status": "complete",
  "total_batches": 272
}
```

`265,620` new rows plus the `6,000` rows committed before termination equals
the expected `271,620`.

## Final parity

The script calculated an ordered PostgreSQL digest over event ID, user, device,
observation timestamp, value, and activity state.

```text
uninterrupted: 271620|2791d4a9a5996490ff6191256d36ca5a
recovered:     271620|2791d4a9a5996490ff6191256d36ca5a
comparison:    identical
```

This is direct container/PostgreSQL evidence for this post-commit recovery run.
The fast isolated test
`test_failure_after_committed_batch_resumes_without_duplicates` also raises an
exception after batch 2 commits, checks the two-batch checkpoint and 200
persisted rows, resumes, and compares all final event IDs with generator
output. Neither test claims to interrupt an open transaction.

A negative-control run set target checkpoint `999` and the timeout to one
second. The harness exited non-zero and emitted `Timed out after 1 seconds
waiting for checkpoint 999`, confirming that a failed checkpoint wait
terminates with a diagnostic instead of polling indefinitely.
