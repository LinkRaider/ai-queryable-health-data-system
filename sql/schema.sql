CREATE TABLE IF NOT EXISTS users (
    user_id text PRIMARY KEY CHECK (user_id ~ '^user-[0-9]{4}$'),
    device_id text NOT NULL UNIQUE CHECK (device_id ~ '^device-[0-9]{4}$'),
    timezone_name text NOT NULL,
    resting_baseline_bpm numeric(4, 1) NOT NULL
        CHECK (resting_baseline_bpm BETWEEN 30 AND 220),
    profile jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_user_device_key UNIQUE (user_id, device_id)
);

CREATE TABLE IF NOT EXISTS heart_rate_observations (
    event_id uuid PRIMARY KEY,
    schema_version text NOT NULL
        CHECK (schema_version = 'health.heart_rate.observation.v1'),
    user_id text NOT NULL,
    device_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    metric text NOT NULL CHECK (metric = 'heart_rate'),
    value numeric(4, 1) NOT NULL CHECK (value BETWEEN 30 AND 220),
    unit text NOT NULL CHECK (unit = 'bpm'),
    activity_state text NOT NULL
        CHECK (activity_state IN ('sleep', 'rest', 'ordinary_activity', 'workout')),
    ingested_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT heart_rate_observations_user_device_fk
        FOREIGN KEY (user_id, device_id) REFERENCES users(user_id, device_id),
    CONSTRAINT heart_rate_observations_natural_key
        UNIQUE (user_id, device_id, observed_at, metric)
);

CREATE INDEX IF NOT EXISTS heart_rate_user_time_activity_idx
    ON heart_rate_observations (user_id, observed_at, activity_state)
    INCLUDE (value);

CREATE INDEX IF NOT EXISTS heart_rate_time_activity_user_idx
    ON heart_rate_observations (observed_at, activity_state, user_id)
    INCLUDE (value);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    source_key text PRIMARY KEY CHECK (source_key ~ '^[a-f0-9]{64}$'),
    schema_version text NOT NULL,
    generator_version text NOT NULL,
    configuration jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('in_progress', 'complete')),
    expected_event_count integer NOT NULL CHECK (expected_event_count >= 0),
    inserted_event_count integer NOT NULL DEFAULT 0
        CHECK (inserted_event_count >= 0 AND inserted_event_count <= expected_event_count),
    started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (
        (status = 'in_progress' AND completed_at IS NULL)
        OR
        (
            status = 'complete'
            AND completed_at IS NOT NULL
        )
    )
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'ingestion_runs' AND column_name = 'run_key'
    ) THEN
        ALTER TABLE ingestion_runs RENAME COLUMN run_key TO source_key;
    END IF;
END
$$;

ALTER TABLE ingestion_runs
    DROP COLUMN IF EXISTS next_batch_index,
    DROP COLUMN IF EXISTS total_batches;

DO $$
BEGIN
    ALTER TABLE ingestion_runs
        ADD CONSTRAINT ingestion_runs_complete_count_check
        CHECK (
            status <> 'complete'
            OR inserted_event_count = expected_event_count
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;
