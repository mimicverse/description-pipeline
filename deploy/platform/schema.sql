-- Platform schema: the admission gate, the business rows and their controlled fields.
-- Frozen structure: A v3.3 (reviews/target-architecture-20261013/platform-admission-and-projection-A-final.md).
-- Applied by the maintenance role via apply.sh; idempotent.  The read models live in
-- views.sql, the grants in roles.sql.  Scheduling facts are deliberately NOT stored here:
-- the lifecycle view reads the real Airflow metadata in the same consistent snapshot
-- (no cache, no TTL, no third view).
--
-- Per-object state vocabularies (frozen; these CHECKs consume them):
--   run        queued / running / succeeded / failed / cancelled / unresolved
--   stage      pending / running / completed / failed / cancelled / unresolved / blocked
--   operation  queued / running / succeeded / failed / cancelled / unresolved
--   request    immutable record; the projection reports the accepted fact only

CREATE SCHEMA IF NOT EXISTS platform;

CREATE TABLE IF NOT EXISTS platform.admission (
    id smallint PRIMARY KEY CHECK (id = 1),
    state text NOT NULL CHECK (state IN ('open', 'closed')),
    reason text NOT NULL,
    actor text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    revision bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS platform.request (
    request_id text PRIMARY KEY,
    principal text NOT NULL,
    target_hardware text NOT NULL,
    main_assembly text,
    configuration jsonb,
    -- The explicit baseline/status references of the sealed submission (immutable once
    -- written); they add no projection columns.
    baseline jsonb,
    -- C-defined semantics: a request row is a sealed acceptance fact, so the seal instant is
    -- NOT NULL and defaults to the transaction clock; the API never updates the row.
    sealed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.run (
    run_id text PRIMARY KEY,
    request_id text NOT NULL REFERENCES platform.request (request_id),
    parent_run_id text REFERENCES platform.run (run_id),
    from_stage text,
    state text NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'unresolved')),
    tool_identity jsonb,
    -- Display metadata lives on the run (store-managed); it never enters the six-column view.
    display_name text,
    hidden_at timestamptz,
    hidden_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.stage (
    run_id text NOT NULL REFERENCES platform.run (run_id),
    stage text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'unresolved', 'blocked')),
    -- Sealed receipt / checkpoint reference, registered atomically with a confirmation.
    receipt jsonb,
    checkpoint jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, stage)
);

CREATE TABLE IF NOT EXISTS platform.operation (
    operation_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES platform.run (run_id),
    stage text,
    kind text NOT NULL CHECK (kind IN ('dispatch', 'native', 'publish', 'cancel', 'cache_release')),
    state text NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'unresolved')),
    -- Idempotent registration key: same key + same digest returns the existing record; the
    -- UNIQUE constraint backstops concurrent registrations.
    operation_key text NOT NULL UNIQUE,
    params_digest text NOT NULL,
    -- The dispatch intent's actual Airflow binding (the retained side of the scheduling rows).
    -- The join key is the full unique key (dag_id, task_id, run_id, map_index); try_number is
    -- attempt metadata for reports and is never a join key.  Missing keys must stay visible as
    -- unknown in the view - never silently dropped.
    dag_id text,
    dag_run_id text,
    task_id text,
    map_index integer,
    try_number integer,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The single admission row starts open; the store API owns every later transition.
INSERT INTO platform.admission (id, state, reason, actor)
VALUES (1, 'open', 'initial', 'install')
ON CONFLICT (id) DO NOTHING;
