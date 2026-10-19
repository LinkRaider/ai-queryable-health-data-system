# Interrupted ingestion verification

Command executed:

```bash
./scripts/test_interrupted_ingestion.sh
```

The script created two isolated Compose projects with empty PostgreSQL volumes.
The baseline project completed normally. The recovery project used the same
image, fixed generation configuration, and 1,000-event batch size, with a
100-millisecond post-commit delay so the process could be interrupted at a
known durable checkpoint.

## Observed interruption

- Signal sent to the running bootstrap container: `SIGKILL`
- Durable run status immediately after termination: `in_progress`
- Durable `next_batch_index`: `5` of `272`
- Rows committed at interruption: `5,000`
- Durable `inserted_event_count`: `5,000`

The persisted row count and checkpoint-aligned inserted count were equal. The
termination happened during the post-commit delay after batch 5, not inside an
uncommitted transaction.

## Observed restart

The bootstrap container was force-recreated without the artificial delay. It
read checkpoint 5, regenerated the deterministic sequence, skipped the first
five batches, and reported:

```json
{
  "event": "bootstrap_complete",
  "expected_event_count": 271620,
  "inserted_event_count": 271620,
  "inserted_this_attempt": 266620,
  "next_batch_index": 272,
  "run_key": "66f0603bdbe21c2df7903701b6af2c099ce5c9f89d2a7579aad6a13e887981cc",
  "status": "complete",
  "total_batches": 272
}
```

`266,620` new rows plus the `5,000` rows committed before termination equals
the expected `271,620`.

## Final parity

The script calculated an ordered PostgreSQL digest over event ID, user, device,
observation timestamp, value, and activity state.

```text
uninterrupted: 271620|2791d4a9a5996490ff6191256d36ca5a
recovered:     271620|2791d4a9a5996490ff6191256d36ca5a
comparison:    identical
```

This is direct container/PostgreSQL evidence for this run. The fast isolated
test `test_failure_after_committed_batch_resumes_without_duplicates` separately
raises an exception after batch 2 commits, checks the two-batch checkpoint and
200 persisted rows, resumes, and compares all final event IDs with generator
output.
