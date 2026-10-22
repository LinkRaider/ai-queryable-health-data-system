#!/usr/bin/env bash
set -euo pipefail

baseline_project="ai-health-baseline-test"
recovery_project="ai-health-recovery-test"
shared_source_volume="ai-health-interruption-source"
target_event_count="${HEALTH_DATA_TEST_TARGET_EVENT_COUNT:-5000}"
progress_timeout_seconds="${HEALTH_DATA_TEST_PROGRESS_TIMEOUT_SECONDS:-120}"

compose() {
  local project="$1"
  shift
  HEALTH_DATA_SHARED_SOURCE_VOLUME="$shared_source_volume" \
    docker compose \
      --project-name "$project" \
      -f compose.yaml \
      -f compose.shared-source.yaml \
      "$@"
}

compose_recovery() {
  HEALTH_DATA_SHARED_SOURCE_VOLUME="$shared_source_volume" \
    docker compose \
      --project-name "$recovery_project" \
      -f compose.yaml \
      -f compose.shared-source.yaml \
      -f compose.interruption.yaml \
      "$@"
}

cleanup() {
  compose "$baseline_project" down --volumes --remove-orphans >/dev/null 2>&1 || true
  compose "$recovery_project" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker volume rm "$shared_source_volume" >/dev/null 2>&1 || true
}

trap cleanup EXIT

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
docker volume rm "$shared_source_volume" >/dev/null 2>&1 || true
docker volume create "$shared_source_volume" >/dev/null

docker compose -f compose.yaml build bootstrap >/dev/null

compose "$baseline_project" up --detach --wait postgres >/dev/null
baseline_output="$(compose "$baseline_project" run --rm bootstrap)"
baseline_digest="$(database_digest "$baseline_project")"
baseline_source_digest="$(
  database_scalar "$baseline_project" \
    "SELECT configuration->>'source_sha256' FROM ingestion_runs;"
)"

compose "$recovery_project" up --detach --wait postgres >/dev/null
compose_recovery up --detach bootstrap >/dev/null

persisted_count=0
deadline=$((SECONDS + progress_timeout_seconds))
bootstrap_container="$(compose_recovery ps --quiet bootstrap)"
while (( persisted_count < target_event_count )); do
  bootstrap_status="$(
    docker inspect --format '{{.State.Status}}' "$bootstrap_container"
  )"
  if [[ "$bootstrap_status" != "running" ]]; then
    printf 'Bootstrap stopped before %s events; status=%s\n' \
      "$target_event_count" "$bootstrap_status" >&2
    compose_recovery logs --no-color bootstrap >&2
    exit 1
  fi
  persisted_count="$(
    database_scalar "$recovery_project" \
      "SELECT count(*) FROM heart_rate_observations;" \
      2>/dev/null || printf '0'
  )"
  if (( SECONDS >= deadline )); then
    printf 'Timed out after %s seconds waiting for %s events; observed=%s\n' \
      "$progress_timeout_seconds" "$target_event_count" "$persisted_count" >&2
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
      inserted_event_count::text || '|' ||
      (SELECT count(*) FROM heart_rate_observations)::text
    FROM ingestion_runs;
  "
)"
IFS='|' read -r interrupted_status inserted_count persisted_count \
  <<<"$interrupted_state"

if [[ "$interrupted_status" != "in_progress" ]]; then
  printf 'Expected in_progress after kill, got %s\n' "$interrupted_status" >&2
  exit 1
fi
if [[ "$inserted_count" != "$persisted_count" ]]; then
  printf 'Recorded inserted count %s differs from persisted count %s\n' \
    "$inserted_count" "$persisted_count" >&2
  exit 1
fi

restart_started_at="$(date +%s)"
compose "$recovery_project" up --detach --force-recreate bootstrap >/dev/null
restarted_container="$(compose "$recovery_project" ps --quiet bootstrap)"
restart_exit_code="$(docker wait "$restarted_container")"
if [[ "$restart_exit_code" != "0" ]]; then
  compose "$recovery_project" logs --no-color bootstrap >&2
  exit 1
fi
restart_seconds="$(( $(date +%s) - restart_started_at ))"

restart_output="$(compose "$recovery_project" logs --no-color --no-log-prefix bootstrap)"
recovered_digest="$(database_digest "$recovery_project")"
recovered_source_digest="$(
  database_scalar "$recovery_project" \
    "SELECT configuration->>'source_sha256' FROM ingestion_runs;"
)"
if [[ "$baseline_digest" != "$recovered_digest" ]]; then
  printf 'Baseline and recovered digests differ\n' >&2
  printf 'baseline=%s\nrecovered=%s\n' "$baseline_digest" "$recovered_digest" >&2
  exit 1
fi
if [[ "$baseline_source_digest" != "$recovered_source_digest" ]]; then
  printf 'Baseline and recovery did not ingest the same source artifact\n' >&2
  exit 1
fi

printf 'baseline_bootstrap=%s\n' "$baseline_output"
printf 'interruption_signal=SIGKILL\n'
printf 'interrupted_status=%s\n' "$interrupted_status"
printf 'interrupted_persisted_event_count=%s\n' "$persisted_count"
printf 'restart_bootstrap=%s\n' "$restart_output"
printf 'restart_replay_seconds=%s\n' "$restart_seconds"
printf 'shared_source_sha256=%s\n' "$baseline_source_digest"
printf 'baseline_count_and_digest=%s\n' "$baseline_digest"
printf 'recovered_count_and_digest=%s\n' "$recovered_digest"
printf 'comparison=identical\n'
