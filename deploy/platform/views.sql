-- Read models for the operator: exactly two views, six columns.
-- Run through apply.sh, which supplies :airflow_schema (default: airflow).
--
-- lifecycle_projection derives platform state from canonical facts only. The
-- scheduling rows retain the full Airflow key (dag_id, task_id, run_id,
-- map_index) and LEFT JOIN task_instance; a missing or partial key remains
-- unknown. dag_run rows carry the real Airflow state for the same association.

CREATE OR REPLACE VIEW :"platform_schema".lifecycle_projection AS
WITH
effective_effects AS (
    SELECT
        i.effect_id,
        c.run_id,
        CASE
            WHEN COALESCE(bool_or(f.fact_type = 'conflict'), false) THEN 'conflict'
            WHEN COALESCE(
                bool_or(
                    (f.fact_type = 'observation' AND f.state_class = 'known')
                    OR (f.fact_type = 'reconciled' AND f.state_class = 'known')
                ),
                false
            ) AND COALESCE(
                bool_or(f.fact_type = 'reconciled' AND f.state_class = 'absent'),
                false
            ) THEN 'conflict'
            WHEN COALESCE(
                bool_or(
                    (f.fact_type = 'observation' AND f.state_class = 'known')
                    OR (f.fact_type = 'reconciled' AND f.state_class = 'known')
                ),
                false
            ) THEN 'satisfied'
            WHEN COALESCE(
                bool_or(f.fact_type = 'reconciled' AND f.state_class = 'absent'),
                false
            ) THEN 'definitely_failed'
            WHEN COALESCE(bool_or(f.fact_type = 'dispatch_authorized'), false) THEN 'unknown'
            ELSE 'prepared'
        END AS state
    FROM :"platform_schema".effect_intent i
    JOIN :"platform_schema".command c ON c.command_id = i.command_id
    LEFT JOIN :"platform_schema".effect_fact f ON f.effect_id = i.effect_id
    GROUP BY i.effect_id, c.run_id
),
effect_projection AS (
    SELECT
        e.effect_id,
        e.run_id,
        e.state,
        CASE
            WHEN e.state IN ('conflict', 'unknown') THEN 'unresolved'
            WHEN e.state IN ('satisfied', 'definitely_failed') THEN 'terminal'
            WHEN d.outcome IS NOT NULL THEN 'terminal'
            ELSE 'unknown'
        END AS bucket,
        COALESCE(d.decided_at, a.created_at) AS updated_at
    FROM effective_effects e
    JOIN :"platform_schema".run_admission a ON a.run_id = e.run_id
    LEFT JOIN :"platform_schema".effect_intent i ON i.effect_id = e.effect_id
    LEFT JOIN :"platform_schema".command c ON c.command_id = i.command_id
    LEFT JOIN :"platform_schema".stage_decision d
           ON d.command_id = c.command_id
          AND c.kind = 'stage'
),
stage_projection AS (
    SELECT
        rows.run_id,
        rows.stage,
        CASE
            WHEN COALESCE(effects.has_unknown, false) THEN 'unresolved'
            WHEN decision.outcome IS NOT NULL THEN decision.outcome
            WHEN COALESCE(claim.open_claim, false) THEN 'running'
            WHEN COALESCE(cancel.requested, false) THEN 'cancellation_requested'
            ELSE 'waiting'
        END AS state,
        CASE
            WHEN COALESCE(effects.has_unknown, false) THEN 'unresolved'
            WHEN decision.outcome IN ('completed', 'failed', 'cancelled') THEN 'terminal'
            WHEN COALESCE(claim.open_claim, false) THEN 'in_flight'
            WHEN COALESCE(cancel.requested, false) THEN 'unresolved'
            ELSE 'unknown'
        END AS bucket,
        COALESCE(
            decision.decided_at,
            claim.updated_at,
            cancel.requested_at,
            rows.created_at
        ) AS updated_at
    FROM (
        SELECT
            a.run_id,
            a.created_at,
            stage_rows.stage
        FROM :"platform_schema".run_admission a
        CROSS JOIN (
            VALUES
                ('freeze', 0),
                ('discover', 1),
                ('capture', 2),
                ('generate', 3),
                ('verify', 4),
                ('publish', 5)
        ) AS stage_rows(stage, position)
    ) AS rows
    LEFT JOIN :"platform_schema".command command
           ON command.run_id = rows.run_id
          AND command.kind = 'stage'
          AND command.stage = rows.stage
    LEFT JOIN :"platform_schema".stage_decision decision
           ON decision.command_id = command.command_id
    LEFT JOIN LATERAL (
        SELECT
            count(*) > 0 AS open_claim,
            max(c.last_seen_at) AS updated_at
        FROM :"platform_schema".execution_claim c
        WHERE c.command_id = command.command_id
          AND c.closed_at IS NULL
    ) AS claim ON true
    LEFT JOIN LATERAL (
        SELECT
            count(*) > 0 AS requested,
            max(cancel_command.created_at) AS requested_at
        FROM :"platform_schema".command cancel_command
        WHERE cancel_command.kind = 'cancel'
          AND cancel_command.request->>'target_command_id' = command.command_id
    ) AS cancel ON true
    LEFT JOIN LATERAL (
        SELECT
            bool_or(e.state IN ('unknown', 'conflict')) AS has_unknown
        FROM effective_effects e
        JOIN :"platform_schema".effect_intent own_intent
          ON own_intent.effect_id = e.effect_id
        WHERE own_intent.command_id = command.command_id
    ) AS effects ON true
),
run_projection AS (
    SELECT
        a.run_id,
        CASE
            WHEN bool_or(s.state = 'unresolved') THEN 'unresolved'
            WHEN bool_or(s.state = 'running') THEN 'running'
            WHEN bool_or(s.state = 'failed') THEN 'failed'
            WHEN bool_or(s.state = 'cancelled') THEN 'cancelled'
            WHEN bool_and(s.state = 'completed') THEN 'succeeded'
            ELSE 'waiting'
        END AS state,
        CASE
            WHEN bool_or(s.state = 'unresolved') THEN 'unresolved'
            WHEN bool_or(s.state = 'running') THEN 'in_flight'
            WHEN bool_or(s.state = 'failed') THEN 'terminal'
            WHEN bool_or(s.state = 'cancelled') THEN 'terminal'
            WHEN bool_and(s.state = 'completed') THEN 'terminal'
            ELSE 'unknown'
        END AS bucket,
        max(s.updated_at) AS updated_at
    FROM :"platform_schema".run_admission a
    LEFT JOIN stage_projection s ON s.run_id = a.run_id
    GROUP BY a.run_id
),
bound_claims AS (
    SELECT
        c.claim_id,
        command.run_id,
        c.dag_id,
        c.dag_run_id,
        c.task_id,
        c.map_index,
        c.last_seen_at,
        ti.state AS task_state,
        COALESCE(ti.end_date, ti.start_date, c.last_seen_at) AS updated_at
    FROM :"platform_schema".execution_claim c
    JOIN :"platform_schema".command command ON command.command_id = c.command_id
    LEFT JOIN :"airflow_schema".task_instance ti
           ON ti.dag_id = c.dag_id
          AND ti.task_id = c.task_id
          AND ti.run_id = c.dag_run_id
          AND ti.map_index = c.map_index
    WHERE c.dag_id IS NOT NULL
       OR c.dag_run_id IS NOT NULL
       OR c.task_id IS NOT NULL
       OR c.map_index IS NOT NULL
),
scheduling_projection AS (
    SELECT
        b.claim_id,
        b.run_id,
        b.task_state AS state,
        CASE
            WHEN b.task_state IN (
                'queued', 'running', 'scheduled', 'deferred',
                'up_for_retry', 'up_for_reschedule', 'restarting'
            ) THEN 'in_flight'
            WHEN b.task_state IN ('success', 'failed', 'skipped', 'upstream_failed', 'removed')
                THEN 'terminal'
            ELSE 'unknown'
        END AS bucket,
        b.updated_at
    FROM bound_claims b
),
dag_run_projection AS (
    SELECT
        dr.dag_id || '/' || dr.run_id AS object_id,
        command.run_id,
        dr.state,
        CASE
            WHEN dr.state IN ('success', 'failed') THEN 'terminal'
            WHEN dr.state IN ('queued', 'running') THEN 'in_flight'
            ELSE 'unknown'
        END AS bucket,
        COALESCE(dr.end_date, dr.start_date) AS updated_at
    FROM :"airflow_schema".dag_run dr
    JOIN :"platform_schema".execution_claim c
      ON c.dag_id = dr.dag_id
     AND c.dag_run_id = dr.run_id
    JOIN :"platform_schema".command command ON command.command_id = c.command_id
    GROUP BY dr.dag_id, dr.run_id, dr.state, dr.start_date, dr.end_date, command.run_id
)
SELECT object_kind, object_id, run_id, state, bucket, updated_at
FROM (
    SELECT 'run'::text AS object_kind, run_id AS object_id, run_id, state, bucket, updated_at
    FROM run_projection
    UNION ALL
    SELECT 'stage'::text, run_id || ':' || stage, run_id, state, bucket, updated_at
    FROM stage_projection
    UNION ALL
    SELECT 'effect'::text, effect_id, run_id, state, bucket, updated_at
    FROM effect_projection
    UNION ALL
    SELECT 'scheduling'::text, claim_id, run_id, state, bucket, updated_at
    FROM scheduling_projection
    UNION ALL
    SELECT 'dag_run'::text, object_id, run_id, state, bucket, updated_at
    FROM dag_run_projection
) AS rows;

CREATE OR REPLACE VIEW :"platform_schema".admission_projection AS
SELECT state, reason, actor, changed_at, revision
FROM :"platform_schema".admission
WHERE id = 1;
