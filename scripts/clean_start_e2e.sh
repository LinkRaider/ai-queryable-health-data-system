#!/usr/bin/env bash
set -euo pipefail

temporary_directory="$(mktemp -d)"
trap 'rm -rf "$temporary_directory"' EXIT

docker compose down --volumes --remove-orphans

started_at="$(date +%s)"
docker compose up --build --detach --remove-orphans

api_container="$(docker compose ps --quiet api)"
for _attempt in $(seq 1 300); do
  health="$(
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
      "$api_container"
  )"
  if [[ "$health" == "healthy" ]]; then
    break
  fi
  if [[ "$health" == "unhealthy" ]]; then
    docker compose logs --no-color api ollama model-init bootstrap postgres >&2
    exit 1
  fi
  sleep 1
done
if [[ "${health:-}" != "healthy" ]]; then
  printf 'API did not become healthy within 300 seconds after container start\n' >&2
  exit 1
fi
startup_seconds="$(( $(date +%s) - started_at ))"

event_count="$(
  docker compose exec -T postgres \
    psql -U health_data -d health_data -Atqc \
    'SELECT count(*) FROM heart_rate_observations;'
)"

health_response="$(
  curl --fail --silent http://127.0.0.1:8000/health/ready
)"
structured_response="$(
  curl --fail --silent \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{
      "operation": "activity_state_comparison",
      "user_id": "user-0001",
      "metric": "heart_rate",
      "start": "2026-01-05T00:00:00Z",
      "end": "2026-01-12T00:00:00Z"
    }' \
    http://127.0.0.1:8000/v1/query
)"
natural_language_response="$(
  curl --fail --silent \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{
      "question": "What was user-0001'\''s average resting heart rate last week?"
    }' \
    http://127.0.0.1:8000/v1/query/natural-language
)"
rejected_status="$(
  curl --silent \
    --output "$temporary_directory/rejected.json" \
    --write-out '%{http_code}' \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{"question": "What was the average heart rate last week?"}' \
    http://127.0.0.1:8000/v1/query/natural-language
)"
if [[ "$rejected_status" != "422" ]]; then
  printf 'Expected ambiguous question status 422, got %s\n' "$rejected_status" >&2
  exit 1
fi
rejected_response="$(cat "$temporary_directory/rejected.json")"

before_replay="$event_count"
replay_output="$(docker compose run --rm bootstrap)"
after_replay="$(
  docker compose exec -T postgres \
    psql -U health_data -d health_data -Atqc \
    'SELECT count(*) FROM heart_rate_observations;'
)"
if [[ "$before_replay" != "$after_replay" ]]; then
  printf 'Replay changed event count from %s to %s\n' \
    "$before_replay" "$after_replay" >&2
  exit 1
fi

printf 'startup_seconds=%s\n' "$startup_seconds"
printf 'event_count=%s\n' "$event_count"
printf 'health_response=%s\n' "$health_response"
printf 'structured_response=%s\n' "$structured_response"
printf 'natural_language_response=%s\n' "$natural_language_response"
printf 'rejected_status=%s\n' "$rejected_status"
printf 'rejected_response=%s\n' "$rejected_response"
printf 'replay_output=%s\n' "$replay_output"
printf 'event_count_after_replay=%s\n' "$after_replay"
