#!/usr/bin/env bash
set -euo pipefail

baseline_project="ai-health-baseline-test"
recovery_project="ai-health-recovery-test"
target_checkpoint="${HEALTH_DATA_TEST_TARGET_CHECKPOINT:-5}"
checkpoint_timeout_seconds="${HEALTH_DATA_TEST_CHECKPOINT_TIMEOUT_SECONDS:-120}"

compose() {
  local project="$1"
  shift
  docker compose --project-name "$project" -f compose.yaml "$@"
}

compose_recovery() {
  docker compose \
    --project-name "$recovery_project" \
    -f compose.yaml \
    -f compose.interruption.yaml \
    "$@"
}

database_scalar() {
  local project="$1"
  local sql="$2"
  compose "$project" exec -T postgres \
    psql -U health_data -d health_data -Atqc "$sql"
}

database_digest() {
  local project="$1"
  database_scalar "$project" "
    SELECT
      count(*)::text || '|' ||
      md5(
        string_agg(
          event_id::text || '|' ||
          user_id || '|' ||
          device_id || '|' ||
          observed_at::text || '|' ||
          value::text || '|' ||
          activity_state,
          E'\\n'
          ORDER BY event_id
        )
      )
    FROM heart_rate_observations;
  "
}

compose "$baseline_project" down --volumes --remove-orphans >/dev/null 2>&1 || true
compose "$recovery_project" down --volumes --remove-orphans >/dev/null 2>&1 || true

docker compose -f compose.yaml build bootstrap >/dev/null

compose "$baseline_project" up --detach --wait postgres >/dev/null
baseline_output="$(compose "$baseline_project" run --rm bootstrap)"
baseline_digest="$(database_digest "$baseline_project")"

compose "$recovery_project" up --detach --wait postgres >/dev/null
compose_recovery up --detach bootstrap >/dev/null

checkpoint=0
deadline=$((SECONDS + checkpoint_timeout_seconds))
bootstrap_container="$(compose_recovery ps --quiet bootstrap)"
while (( checkpoint < target_checkpoint )); do
  bootstrap_status="$(
    docker inspect --format '{{.State.Status}}' "$bootstrap_container"
  )"
  if [[ "$bootstrap_status" != "running" ]]; then
    printf 'Bootstrap stopped before checkpoint %s; status=%s\n' \
      "$target_checkpoint" "$bootstrap_status" >&2
    compose_recovery logs --no-color bootstrap >&2
    exit 1
  fi
  checkpoint="$(
    database_scalar "$recovery_project" \
      "SELECT COALESCE(max(next_batch_index), 0) FROM ingestion_runs;" \
      2>/dev/null || printf '0'
  )"
  if (( SECONDS >= deadline )); then
    printf 'Timed out after %s seconds waiting for checkpoint %s; observed=%s\n' \
      "$checkpoint_timeout_seconds" "$target_checkpoint" "$checkpoint" >&2
    compose_recovery logs --no-color bootstrap >&2
    exit 1
  fi
  sleep 0.05
done

compose_recovery kill --signal SIGKILL bootstrap >/dev/null

interrupted_state="$(
  database_scalar "$recovery_project" "
    SELECT
      status || '|' ||
      next_batch_index::text || '|' ||
      inserted_event_count::text || '|' ||
      (SELECT count(*) FROM heart_rate_observations)::text
    FROM ingestion_runs;
  "
)"
IFS='|' read -r interrupted_status interrupted_checkpoint inserted_count persisted_count \
  <<<"$interrupted_state"

if [[ "$interrupted_status" != "in_progress" ]]; then
  printf 'Expected in_progress after kill, got %s\n' "$interrupted_status" >&2
  exit 1
fi
if (( interrupted_checkpoint >= 272 )); then
  printf 'Bootstrap completed before interruption at batch %s\n' "$interrupted_checkpoint" >&2
  exit 1
fi
if [[ "$inserted_count" != "$persisted_count" ]]; then
  printf 'Checkpoint count %s differs from persisted count %s\n' \
    "$inserted_count" "$persisted_count" >&2
  exit 1
fi

compose "$recovery_project" up --detach --force-recreate bootstrap >/dev/null
restarted_container="$(compose "$recovery_project" ps --quiet bootstrap)"
restart_exit_code="$(docker wait "$restarted_container")"
if [[ "$restart_exit_code" != "0" ]]; then
  compose "$recovery_project" logs --no-color bootstrap >&2
  exit 1
fi

restart_output="$(compose "$recovery_project" logs --no-color --no-log-prefix bootstrap)"
recovered_digest="$(database_digest "$recovery_project")"
if [[ "$baseline_digest" != "$recovered_digest" ]]; then
  printf 'Baseline and recovered digests differ\n' >&2
  printf 'baseline=%s\nrecovered=%s\n' "$baseline_digest" "$recovered_digest" >&2
  exit 1
fi

printf 'baseline_bootstrap=%s\n' "$baseline_output"
printf 'interruption_signal=SIGKILL\n'
printf 'interrupted_status=%s\n' "$interrupted_status"
printf 'interrupted_next_batch_index=%s\n' "$interrupted_checkpoint"
printf 'interrupted_persisted_event_count=%s\n' "$persisted_count"
printf 'restart_bootstrap=%s\n' "$restart_output"
printf 'baseline_count_and_digest=%s\n' "$baseline_digest"
printf 'recovered_count_and_digest=%s\n' "$recovered_digest"
printf 'comparison=identical\n'
