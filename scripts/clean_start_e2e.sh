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
      "start": "2026-10-05T00:00:00Z",
      "end": "2026-10-26T00:00:00Z"
    }' \
    http://127.0.0.1:8000/v1/query
)"
natural_language_response="$(
  curl --fail --silent \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{
      "question": "What was user-0001'\''s average resting heart rate each day from 2026-10-05T00:00:00Z to 2026-10-26T00:00:00Z?"
    }' \
    http://127.0.0.1:8000/v1/query/natural-language
)"
validate_weighted_response() {
  local response="$1"
  local nested="${2:-false}"
  python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if sys.argv[1] == "true":
    payload = payload["result"]
assert payload["aggregation_basis"] == "time_weighted_observation_hold"
assert payload["max_hold_seconds"] == 360
assert isinstance(payload["quality"], dict)
assert all("time_weighted_mean_bpm" in row for row in payload["rows"])
assert all("mean_recorded_bpm" not in row for row in payload["rows"])
' "$nested" <<<"$response"
}
validate_weighted_response "$structured_response"
validate_weighted_response "$natural_language_response" true
rejected_status="$(
  curl --silent \
    --output "$temporary_directory/rejected.json" \
    --write-out '%{http_code}' \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{"question": "What was the average heart rate from 2026-10-05T00:00:00Z to 2026-10-26T00:00:00Z?"}' \
    http://127.0.0.1:8000/v1/query/natural-language
)"
if [[ "$rejected_status" != "422" ]]; then
  printf 'Expected ambiguous question status 422, got %s\n' "$rejected_status" >&2
  exit 1
fi
rejected_response="$(cat "$temporary_directory/rejected.json")"

before_replay="$event_count"
replay_started_at="$(date +%s)"
replay_output="$(docker compose run --rm bootstrap)"
replay_seconds="$(( $(date +%s) - replay_started_at ))"
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

docker compose down
preserved_restart_started_at="$(date +%s)"
docker compose up --detach --remove-orphans
api_container="$(docker compose ps --quiet api)"
health=""
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
if [[ "$health" != "healthy" ]]; then
  printf 'API did not become healthy within 300 seconds on preserved restart\n' >&2
  exit 1
fi
preserved_restart_seconds="$(( $(date +%s) - preserved_restart_started_at ))"
preserved_event_count="$(
  docker compose exec -T postgres \
    psql -U health_data -d health_data -Atqc \
    'SELECT count(*) FROM heart_rate_observations;'
)"
if [[ "$event_count" != "$preserved_event_count" ]]; then
  printf 'Preserved restart changed event count from %s to %s\n' \
    "$event_count" "$preserved_event_count" >&2
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
printf 'full_replay_seconds=%s\n' "$replay_seconds"
printf 'event_count_after_replay=%s\n' "$after_replay"
printf 'preserved_restart_seconds=%s\n' "$preserved_restart_seconds"
printf 'event_count_after_preserved_restart=%s\n' "$preserved_event_count"
