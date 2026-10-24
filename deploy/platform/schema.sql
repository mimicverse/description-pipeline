-- Platform schema: the admission gate and canonical immutable pipeline facts.
-- The schema itself is created by bootstrap.sql (cluster identity); this file is applied by
-- the platform migration identity (platform_migrate) via apply.sh; idempotent.  The read
-- models live in views.sql, the consumer grants in roles.sql.  Scheduling facts are not stored here:
-- the lifecycle view reads the real Airflow metadata in the same consistent snapshot
-- (no cache, no TTL, no third view).

CREATE TABLE IF NOT EXISTS :"platform_schema".admission (
    id smallint PRIMARY KEY CHECK (id = 1),
    state text NOT NULL CHECK (state IN ('open', 'closed')),
    reason text NOT NULL,
    actor text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    revision bigint NOT NULL DEFAULT 1
);

-- Installation initializes the single admission row CLOSED; the start verb opens it through
-- the canonical admission-control API, which owns every later transition.
INSERT INTO :"platform_schema".admission (id, state, reason, actor)
VALUES (1, 'closed', 'install', 'apply')
ON CONFLICT (id) DO NOTHING;

-- Canonical six-stage facts. Product state is projected from immutable commands,
-- claims, decisions, candidates, effects and artifacts.

CREATE TABLE IF NOT EXISTS :"platform_schema".run_admission (
    run_id text PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    parent_run_id text REFERENCES :"platform_schema".run_admission (run_id),
    from_stage text CHECK (
        from_stage IS NULL
        OR from_stage IN ('freeze', 'discover', 'capture', 'generate', 'verify', 'publish')
    ),
    request jsonb NOT NULL,
    request_digest char(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_run_id IS NULL OR parent_run_id <> run_id)
);

CREATE TABLE IF NOT EXISTS :"platform_schema".artifact (
    artifact_id text PRIMARY KEY,
    kind text NOT NULL,
    locator text NOT NULL UNIQUE,
    manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    set_sha256 char(64) NOT NULL CHECK (set_sha256 ~ '^[0-9a-f]{64}$'),
    producer_run_id text,
    producer_stage text CHECK (
        producer_stage IS NULL
        OR producer_stage IN ('freeze', 'discover', 'capture', 'generate', 'verify', 'publish')
    ),
    producer_tool_sha256 char(64) NOT NULL CHECK (producer_tool_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS :"platform_schema".command (
    command_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES :"platform_schema".run_admission (run_id),
    kind text NOT NULL CHECK (kind IN ('stage', 'dispatch', 'cancel', 'reconcile')),
    stage text CHECK (
        stage IS NULL
        OR stage IN ('freeze', 'discover', 'capture', 'generate', 'verify', 'publish')
    ),
    idempotency_key text NOT NULL UNIQUE,
    request jsonb NOT NULL,
    request_digest char(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    config_snapshot_sha256 char(64) NOT NULL CHECK (config_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    contract_sha256 char(64) NOT NULL CHECK (contract_sha256 ~ '^[0-9a-f]{64}$'),
    policy_sha256 char(64) NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    expected_executor_sha256 char(64) NOT NULL CHECK (expected_executor_sha256 ~ '^[0-9a-f]{64}$'),
    predecessor_decision_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (kind <> 'stage' OR stage IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS command_stage_once
    ON :"platform_schema".command (run_id, stage)
    WHERE kind = 'stage';

CREATE UNIQUE INDEX IF NOT EXISTS command_dispatch_once
    ON :"platform_schema".command (run_id)
    WHERE kind = 'dispatch';

CREATE UNIQUE INDEX IF NOT EXISTS command_cancel_target_once
    ON :"platform_schema".command ((request->>'target_command_id'))
    WHERE kind = 'cancel';

CREATE TABLE IF NOT EXISTS :"platform_schema".command_input_artifact (
    command_id text NOT NULL REFERENCES :"platform_schema".command (command_id),
    artifact_id text NOT NULL REFERENCES :"platform_schema".artifact (artifact_id),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (command_id, artifact_id),
    UNIQUE (command_id, position)
);

CREATE TABLE IF NOT EXISTS :"platform_schema".execution_claim (
    claim_id text PRIMARY KEY,
    command_id text NOT NULL REFERENCES :"platform_schema".command (command_id),
    fence bigint NOT NULL CHECK (fence > 0),
    owner_sha256 char(64) NOT NULL CHECK (owner_sha256 ~ '^[0-9a-f]{64}$'),
    executor_sha256 char(64) NOT NULL CHECK (executor_sha256 ~ '^[0-9a-f]{64}$'),
    lane_key text,
    dag_id text,
    dag_run_id text,
    task_id text,
    map_index integer,
    issued_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    closed_at timestamptz,
    closure_code text CHECK (
        closure_code IS NULL
        OR closure_code IN ('completed', 'definite_failure', 'abandoned_before_effect', 'reconciled', 'operator_closed')
    ),
    UNIQUE (command_id, fence),
    CHECK (
        (closed_at IS NULL AND closure_code IS NULL)
        OR (closed_at IS NOT NULL AND closure_code IS NOT NULL)
    ),
    CHECK (owner_sha256 <> executor_sha256),
    CHECK (expires_at >= issued_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS execution_claim_one_open_command
    ON :"platform_schema".execution_claim (command_id)
    WHERE closed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS execution_claim_one_open_lane
    ON :"platform_schema".execution_claim (lane_key)
    WHERE lane_key IS NOT NULL AND closed_at IS NULL;

CREATE TABLE IF NOT EXISTS :"platform_schema".stage_decision (
    decision_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES :"platform_schema".run_admission (run_id),
    stage text NOT NULL CHECK (
        stage IN ('freeze', 'discover', 'capture', 'generate', 'verify', 'publish')
    ),
    command_id text NOT NULL UNIQUE REFERENCES :"platform_schema".command (command_id),
    outcome text NOT NULL CHECK (outcome IN ('completed', 'failed', 'cancelled')),
    result_record_artifact_id text REFERENCES :"platform_schema".artifact (artifact_id),
    result_digest char(64) CHECK (result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$'),
    actual_executor_sha256 char(64) NOT NULL CHECK (actual_executor_sha256 ~ '^[0-9a-f]{64}$'),
    original_producer_decision_id text REFERENCES :"platform_schema".stage_decision (decision_id),
    reason_code text,
    failure_envelope jsonb,
    failure_digest char(64) CHECK (failure_digest IS NULL OR failure_digest ~ '^[0-9a-f]{64}$'),
    decided_at timestamptz NOT NULL,
    UNIQUE (run_id, stage),
    CHECK (
        outcome <> 'completed'
        OR (result_record_artifact_id IS NOT NULL AND result_digest IS NOT NULL AND failure_envelope IS NULL)
    ),
    CHECK (
        outcome = 'completed'
        OR (
            reason_code IS NOT NULL
            AND (
                (result_record_artifact_id IS NOT NULL AND result_digest IS NOT NULL AND failure_envelope IS NULL)
                OR (
                    result_record_artifact_id IS NULL
                    AND result_digest IS NULL
                    AND failure_envelope IS NOT NULL
                    AND failure_digest IS NOT NULL
                )
            )
        )
    ),
    CHECK ((failure_envelope IS NULL) = (failure_digest IS NULL))
);

CREATE TABLE IF NOT EXISTS :"platform_schema".stage_candidate (
    candidate_id text PRIMARY KEY,
    command_id text NOT NULL UNIQUE REFERENCES :"platform_schema".command (command_id),
    claim_id text NOT NULL REFERENCES :"platform_schema".execution_claim (claim_id),
    fence bigint NOT NULL CHECK (fence > 0),
    output_artifact_id text NOT NULL REFERENCES :"platform_schema".artifact (artifact_id),
    result_record_artifact_id text NOT NULL UNIQUE REFERENCES :"platform_schema".artifact (artifact_id),
    result_digest char(64) NOT NULL CHECK (result_digest ~ '^[0-9a-f]{64}$'),
    outcome text NOT NULL CHECK (outcome IN ('completed', 'failed', 'cancelled')),
    reason_code text,
    created_at timestamptz NOT NULL,
    CHECK (output_artifact_id <> result_record_artifact_id),
    CHECK (outcome = 'completed' OR reason_code IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS :"platform_schema".effect_intent (
    effect_id text PRIMARY KEY,
    command_id text NOT NULL REFERENCES :"platform_schema".command (command_id),
    effect_key text NOT NULL,
    effect_kind text NOT NULL CHECK (
        effect_kind IN ('airflow_dispatch', 'native_operation', 'git_ref_update', 'pr_upsert')
    ),
    target jsonb NOT NULL,
    expected_state jsonb NOT NULL,
    expected_state_digest char(64) NOT NULL CHECK (expected_state_digest ~ '^[0-9a-f]{64}$'),
    outbound_artifact_id text NOT NULL REFERENCES :"platform_schema".artifact (artifact_id),
    outbound_digest char(64) NOT NULL CHECK (outbound_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    UNIQUE (command_id, effect_key)
);

CREATE TABLE IF NOT EXISTS :"platform_schema".effect_fact (
    fact_id text PRIMARY KEY,
    effect_id text NOT NULL REFERENCES :"platform_schema".effect_intent (effect_id),
    seq bigint NOT NULL CHECK (seq > 0),
    claim_id text REFERENCES :"platform_schema".execution_claim (claim_id),
    fence bigint CHECK (fence IS NULL OR fence > 0),
    fact_type text NOT NULL CHECK (
        fact_type IN ('dispatch_authorized', 'response', 'timeout', 'observation', 'reconciled', 'conflict')
    ),
    source text NOT NULL,
    state_class text CHECK (
        state_class IS NULL OR state_class IN ('known', 'absent', 'unobservable')
    ),
    payload_artifact_id text REFERENCES :"platform_schema".artifact (artifact_id),
    payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    metadata_artifact_id text REFERENCES :"platform_schema".artifact (artifact_id),
    observed_at timestamptz NOT NULL,
    UNIQUE (effect_id, seq),
    CHECK (
        (fact_type IN ('observation', 'reconciled') AND state_class IS NOT NULL)
        OR (fact_type NOT IN ('observation', 'reconciled') AND state_class IS NULL)
    ),
    CHECK (
        fact_type NOT IN ('observation', 'reconciled')
        OR payload_artifact_id IS NOT NULL
    ),
    CHECK ((claim_id IS NULL) = (fence IS NULL))
);

CREATE TABLE IF NOT EXISTS :"platform_schema".run_display_metadata (
    run_id text PRIMARY KEY REFERENCES :"platform_schema".run_admission (run_id),
    display_name text,
    hidden_at timestamptz,
    hidden_reason text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((hidden_at IS NULL) = (hidden_reason IS NULL))
);

DO $$
BEGIN
    IF to_regclass('run') IS NOT NULL
       AND to_regclass('run_admission') IS NOT NULL
       AND to_regclass('run_display_metadata') IS NOT NULL THEN
        EXECUTE $migration$
            INSERT INTO run_display_metadata (
                run_id, display_name, hidden_at, hidden_reason, updated_at
            )
            SELECT r.run_id, r.display_name, r.hidden_at, r.hidden_reason, r.updated_at
            FROM run r
            WHERE (r.display_name IS NOT NULL OR r.hidden_at IS NOT NULL)
              AND EXISTS (
                  SELECT 1 FROM run_admission a WHERE a.run_id = r.run_id
              )
            ON CONFLICT (run_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                hidden_at = EXCLUDED.hidden_at,
                hidden_reason = EXCLUDED.hidden_reason,
                updated_at = EXCLUDED.updated_at
        $migration$;
    END IF;
END $$;

-- One-way legacy cutover. The maintenance operator must archive and verify the old
-- tables before applying this file. The legacy business rows are never copied into
-- a second runtime authority.
DROP VIEW IF EXISTS :"platform_schema".lifecycle_projection;
DROP TABLE IF EXISTS :"platform_schema".run_event CASCADE;
DROP TABLE IF EXISTS :"platform_schema".operation CASCADE;
DROP TABLE IF EXISTS :"platform_schema".stage CASCADE;
DROP TABLE IF EXISTS :"platform_schema".run CASCADE;
DROP TABLE IF EXISTS :"platform_schema".request CASCADE;
